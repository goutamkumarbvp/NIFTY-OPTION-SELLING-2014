"""Kotak Neo (official SDK `kotakneoapi` 3.0.6, module `neo_api_client`).

Verified offline against the installed SDK (method signatures, accepted segment /
product / order-type / validity values in `neo_api_client.settings`). Live
endpoints were NOT reachable from the build environment: live behaviour is BLOCKED
until verified with real credentials (see docs/BROKER_CAPABILITY_MATRIX.md).
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import pyotp

from amrt.brokers.base import BrokerProfile, BrokerSession, CapabilityEntry, classify_place_error, normalise_status, num, pick
from amrt.core.enums import DataLabel, Exchange, FeatureStatus, OrderType, Product, Side, Validity
from amrt.core.errors import BrokerRejected, DataUnavailable
from amrt.execution.intents import BrokerOrderRequest
from amrt.execution.venue import BrokerAck, FundsReport, OrderStatusReport, PositionReport
from amrt.marketdata.models import ChainRow, ChainSnapshot, Instrument, OptionLeg, Quote

log = logging.getLogger("amrt.brokers.kotak")
FS = FeatureStatus

SEGMENT = {"NSE_FO": "nse_fo", "BSE_FO": "bse_fo", "MCX_FO": "mcx_fo", "NSE_CM": "nse_cm", "BSE_CM": "bse_cm"}
CHAIN_SEGMENT = {Exchange.NSE: "nse_fo", Exchange.BSE: "bse_fo", Exchange.MCX: "mcx_fo"}
PRODUCT = {Product.NRML: "NRML", Product.MIS: "MIS"}
ORDER_TYPE = {OrderType.LIMIT: "L", OrderType.MARKET: "MKT"}
STATUS = {"complete": "FILLED", "traded": "FILLED", "rejected": "REJECTED", "cancelled": "CANCELLED", "canceled": "CANCELLED", "open": "OPEN",
          "trigger pending": "OPEN", "put order req received": "OPEN", "validation pending": "OPEN", "open pending": "OPEN", "modified": "OPEN",
          "after market order req received": "OPEN", "partially filled": "PARTIAL"}

PROFILE = BrokerProfile(
    name="kotak", display="Kotak Neo", sdk_package="kotakneoapi (neo_api_client)", sdk_version_tested="3.0.6",
    official_docs="https://github.com/Kotak-Neo/Kotak-neo-api-v2 (SDK docstrings); Kotak Neo Trade API documentation", api_version="Neo Trade API v2 (SDK 3.0.6)",
    auth="consumer key + TOTP login (mobile, UCC, TOTP) + MPIN validate; session per day", instruments=["NSE F&O", "BSE F&O", "MCX F&O", "NSE/BSE cash"],
    order_types=["L", "MKT", "SL", "SL-M"], market_data="REST quotes(); WebSocket subscribe()", option_chain="REST option_chain(exchange=<segment>, underlying, expiry)",
    historical_data="historical_data() (not used)", websocket="SDK subscribe() — not used by AMRT v1 (REST polling)", rate_limits="not published in SDK; AMRT throttles via policy",
    sandbox="environment='uat' supported by SDK; not verified", tag_field="tag (echoed as GuiOrdId)", tag_max_len=20,
    known_limitations=["market protection is fixed at 0 by the SDK", "MCX validity DAY only", "expiries()/option_chain() need the F&O segment, not the exchange name"],
    capabilities=[
        CapabilityEntry(feature="authentication", status=FS.PARTIAL, evidence="SDK signature contract test", notes="live login BLOCKED: network policy"),
        CapabilityEntry(feature="option_chain", status=FS.PARTIAL, evidence="contract test + documented-shape parser test", notes="live payload not verified"),
        CapabilityEntry(feature="quotes", status=FS.PARTIAL, evidence="contract test"),
        CapabilityEntry(feature="orders", status=FS.BLOCKED, evidence="contract test (kwargs bind to SDK)", notes="no live verification possible"),
        CapabilityEntry(feature="order_book/positions/funds", status=FS.PARTIAL, evidence="parser tests with documented field names"),
        CapabilityEntry(feature="websocket", status=FS.UNAVAILABLE, evidence="not implemented in AMRT v1"),
    ],
    verification_date="2026-10-03", overall_status=FS.PARTIAL)


# ------------------------------------------------------------------ pure mapping / parsing
def order_kwargs(req: BrokerOrderRequest) -> dict:
    seg = SEGMENT[req.segment]
    validity = req.validity.value if seg != "mcx_fo" else Validity.DAY.value
    return {"exchange_segment": seg, "product": PRODUCT[req.product], "price": f"{req.limit_price:.2f}" if req.order_type == OrderType.LIMIT else "0",
            "order_type": ORDER_TYPE[req.order_type], "quantity": str(req.quantity), "validity": validity,
            "trading_symbol": req.broker_ref.get("trading_symbol") or req.trading_symbol, "transaction_type": "B" if req.side == Side.BUY else "S",
            "amo": "NO", "disclosed_quantity": "0", "trigger_price": "0", "tag": req.client_tag}


def parse_place(resp) -> BrokerAck:
    if not isinstance(resp, dict):
        return BrokerAck(accepted=False, status="UNKNOWN", message=f"unexpected response type {type(resp).__name__}")
    stat = str(resp.get("stat", "")).lower()
    oid = resp.get("nOrdNo") or pick(resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}, "nOrdNo", "orderId")
    if stat == "ok" and oid:
        return BrokerAck(accepted=True, broker_order_id=str(oid), status="OPEN", message="accepted")
    if resp.get("errMsg") or resp.get("error") or stat == "not_ok":
        raise BrokerRejected(str(resp.get("errMsg") or resp.get("error") or resp)[:300])
    return BrokerAck(accepted=False, status="UNKNOWN", message=f"ambiguous response {str(resp)[:200]}")


def _rows(resp) -> list[dict]:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("data", "Data", "result"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def parse_order_book(resp) -> list[OrderStatusReport]:
    out = []
    for r in _rows(resp):
        qty = int(num(pick(r, "qty", "quantity"), 0) or 0)
        filled = int(num(pick(r, "fldQty", "filledQty"), 0) or 0)
        tt = str(pick(r, "trnsTp", "transactionType", default="")).upper()
        out.append(OrderStatusReport(broker_order_id=str(pick(r, "nOrdNo", "orderId", default="")), client_tag=pick(r, "GuiOrdId", "tag"),
                                     trading_symbol=str(pick(r, "trdSym", "tradingSymbol", default="")),
                                     side=Side.BUY if tt.startswith("B") else (Side.SELL if tt.startswith("S") else None), quantity=qty, filled_qty=filled,
                                     avg_price=num(pick(r, "avgPrc", "averagePrice"), 0.0) or 0.0,
                                     status=normalise_status(str(pick(r, "ordSt", "status", default="")), filled, qty, STATUS),
                                     message=str(pick(r, "rejRsn", "rejectionReason", default=""))[:200]))
    return out


def parse_positions(resp) -> list[PositionReport]:
    out = []
    for r in _rows(resp):
        buy = (num(pick(r, "flBuyQty"), 0) or 0) + (num(pick(r, "cfBuyQty"), 0) or 0)
        sell = (num(pick(r, "flSellQty"), 0) or 0) + (num(pick(r, "cfSellQty"), 0) or 0)
        net = int(buy - sell)
        if net == 0:
            continue
        buy_amt = (num(pick(r, "buyAmt"), 0) or 0) + (num(pick(r, "cfBuyAmt"), 0) or 0)
        sell_amt = (num(pick(r, "sellAmt"), 0) or 0) + (num(pick(r, "cfSellAmt"), 0) or 0)
        avg = buy_amt / buy if net > 0 and buy else (sell_amt / sell if sell else 0.0)
        out.append(PositionReport(trading_symbol=str(pick(r, "trdSym", default="")), net_qty=net, avg_price=round(avg, 4)))
    return out


def parse_funds(resp, ts: float) -> FundsReport:
    d = resp.get("data", resp) if isinstance(resp, dict) else {}
    if isinstance(d, list):
        d = d[0] if d else {}
    avail = num(pick(d, "Net", "net", "AvailableMargin", "availableMargin"))
    used = num(pick(d, "MarginUsed", "marginUsed", "UsedMargin"))
    total = (avail + used) if avail is not None and used is not None else None
    return FundsReport(available=avail, margin_used=used, total=total, ts=ts)


def _leg(r: dict, side: str, key: str) -> OptionLeg | None:
    pre = "ce" if side == "CE" else "pe"
    sub = r.get(side) or r.get(side.lower()) or r.get(f"{pre}_data") or {}
    src = sub if isinstance(sub, dict) and sub else {k[len(pre) + 1:]: v for k, v in r.items() if str(k).lower().startswith(pre + "_")}
    if not src:
        return None
    oi = num(pick(src, "oi", "openInterest", "open_interest"))
    prev = num(pick(src, "prev_oi", "prevOi", "previous_oi"))
    chg = num(pick(src, "oi_change", "oiChange", "changeinOpenInterest"))
    if chg is None and oi is not None and prev is not None:
        chg = oi - prev
    return OptionLeg(instrument_key=key, ltp=num(pick(src, "ltp", "last_price", "lastPrice")), bid=num(pick(src, "bid", "bid_price", "bp")),
                     ask=num(pick(src, "ask", "ask_price", "sp")), volume=int(num(pick(src, "volume", "vol"), 0) or 0) if pick(src, "volume", "vol") is not None else None,
                     oi=int(oi) if oi is not None else None, oi_change=int(chg) if chg is not None else None, iv=num(pick(src, "iv", "impliedVolatility")),
                     delta=num(pick(src, "delta")), gamma=num(pick(src, "gamma")), theta=num(pick(src, "theta")), vega=num(pick(src, "vega")))


def leg_refs(resp, underlying: str, exchange: Exchange, expiry: dt.date) -> dict[str, dict]:
    """Per-leg broker references (trading symbol, token) when the chain payload carries them."""
    refs: dict[str, dict] = {}
    for r in _rows(resp) or (_rows({"data": resp["data"].get("chain") or resp["data"].get("rows") or []})
                             if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else []):
        k = num(pick(r, "strike", "strike_price", "strikePrice", "stkPrc"))
        if k is None:
            continue
        for side in ("CE", "PE"):
            pre = side.lower()
            sub = r.get(side) or r.get(pre) or r.get(f"{pre}_data") or {}
            src = sub if isinstance(sub, dict) and sub else {kk[len(pre) + 1:]: v for kk, v in r.items() if str(kk).lower().startswith(pre + "_")}
            sym = pick(src, "trdSym", "pTrdSymbol", "trading_symbol", "tradingSymbol")
            tok = pick(src, "tk", "pSymbol", "token", "instrument_token")
            if sym or tok:
                refs[Instrument.option_key(exchange, underlying, expiry, k, side)] = {"trading_symbol": str(sym or ""), "token": str(tok or ""),
                                                                                      "segment": CHAIN_SEGMENT[exchange]}
    return refs


def parse_chain(resp, underlying: str, exchange: Exchange, expiry: dt.date, recv_ts: float, source: str = "kotak") -> ChainSnapshot:
    rows_raw = _rows(resp) or _rows(resp.get("data", {}) if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else {})
    if not rows_raw and isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        rows_raw = _rows({"data": resp["data"].get("chain") or resp["data"].get("rows") or []})
    rows = []
    for r in rows_raw:
        k = num(pick(r, "strike", "strike_price", "strikePrice", "stkPrc"))
        if k is None:
            continue
        rows.append(ChainRow(strike=k, ce=_leg(r, "CE", Instrument.option_key(exchange, underlying, expiry, k, "CE")),
                             pe=_leg(r, "PE", Instrument.option_key(exchange, underlying, expiry, k, "PE"))))
    spot = None
    meta = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else (resp if isinstance(resp, dict) else {})
    spot = num(pick(meta, "underlying_ltp", "spot", "underlyingValue", "underlying_spot_price", "ltp"))
    if not rows:
        raise DataUnavailable("DATA UNAVAILABLE", reason="kotak option_chain payload not understood", sample=json.dumps(resp, default=str)[:300])
    return ChainSnapshot(underlying=underlying, exchange=exchange, expiry=expiry, spot=spot, ts=recv_ts, source=source, label=DataLabel.LIVE_UNVERIFIED, rows=rows)


class KotakSession(BrokerSession):
    name = "kotak"
    profile = PROFILE

    def credentials_present(self) -> bool:
        s = self.settings
        return all([s.kotak_consumer_key, s.kotak_mobile, s.kotak_ucc, s.kotak_mpin, s.kotak_totp_secret])

    def _login_sync(self) -> dict:
        s = self.settings
        if self._sdk_factory is not None:
            client = self._sdk_factory()
        else:
            from neo_api_client import NeoAPI
            client = NeoAPI(consumer_key=s.kotak_consumer_key, environment="prod")
        client.totp_login(mobile_number=s.kotak_mobile, ucc=s.kotak_ucc, totp=pyotp.TOTP(s.kotak_totp_secret).now())
        res = client.totp_validate(mpin=s.kotak_mpin)
        self._set_client(client)
        return {"ok": True, "validated": bool(res)}

    async def fetch_expiries(self, underlying: str) -> list[str]:
        from amrt.marketdata.instruments import spec
        resp = await self.call("expiries", exchange=CHAIN_SEGMENT[spec(underlying).exchange], underlying=underlying)
        vals = _rows(resp) or (resp.get("data", {}).get("expiries", []) if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else [])
        out = []
        for v in vals:
            raw = v if not isinstance(v, dict) else pick(v, "expiry", "expiryDate", "date")
            for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y", "%d-%m-%Y"):
                try:
                    out.append(dt.datetime.strptime(str(raw), fmt).date().isoformat())
                    break
                except ValueError:
                    continue
        return sorted(set(out))

    async def fetch_chain(self, underlying: str, expiry_iso: str, instruments) -> tuple[ChainSnapshot, Quote | None]:
        from amrt.marketdata.instruments import spec
        sp = spec(underlying)
        resp = await self.call("option_chain", exchange=CHAIN_SEGMENT[sp.exchange], underlying=underlying, expiry=expiry_iso, count=40)
        exp = dt.date.fromisoformat(expiry_iso)
        snap = parse_chain(resp, underlying, sp.exchange, exp, self.clock.ts())
        for ikey, ref in leg_refs(resp, underlying, sp.exchange, exp).items():
            p = ikey.split(":")
            instruments.add(instruments.option(underlying, exp, float(p[3]), p[4]), source="kotak")
            instruments.set_broker_ref(ikey, "kotak", ref)
        spot_q = None
        if snap.spot:
            spot_q = Quote(instrument_key=Instrument.underlying_key(sp.exchange, underlying), ltp=snap.spot, recv_ts=snap.ts, source="kotak", label=DataLabel.LIVE_UNVERIFIED)
        return snap, spot_q

    async def fetch_quotes(self, instruments: list) -> list[Quote]:
        toks = [{"instrument_token": i.broker_refs["kotak"]["token"], "exchange_segment": i.broker_refs["kotak"].get("segment", SEGMENT.get(i.segment.value, "nse_fo"))}
                for i in instruments if "kotak" in i.broker_refs and i.broker_refs["kotak"].get("token")]
        if not toks:
            return []
        resp = await self.call("quotes", instrument_tokens=toks, quote_type="all")
        now = self.clock.ts()
        by_tok = {i.broker_refs["kotak"]["token"]: i for i in instruments if "kotak" in i.broker_refs}
        out = []
        for r in _rows(resp):
            inst = by_tok.get(str(pick(r, "exchange_token", "instrument_token", "tk", default="")))
            ltp = num(pick(r, "ltp", "last_traded_price"))
            if inst is None or not ltp:
                continue
            depth = r.get("depth") or {}
            bid = num(pick((depth.get("buy") or [{}])[0], "price")) if isinstance(depth, dict) and depth.get("buy") else None
            ask = num(pick((depth.get("sell") or [{}])[0], "price")) if isinstance(depth, dict) and depth.get("sell") else None
            out.append(Quote(instrument_key=inst.key, ltp=ltp, bid=bid, ask=ask, volume=int(num(pick(r, "last_volume", "volume"), 0) or 0),
                             oi=int(num(pick(r, "open_int", "oi"), 0) or 0), recv_ts=now, source="kotak", label=DataLabel.LIVE_UNVERIFIED))
        return out

    async def fetch_order_book(self) -> list[OrderStatusReport]:
        return parse_order_book(await self.call("order_report"))

    async def fetch_positions(self) -> list[PositionReport]:
        return parse_positions(await self.call("positions"))

    async def fetch_funds(self) -> FundsReport:
        return parse_funds(await self.call("limits"), self.clock.ts())

    async def _place(self, req: BrokerOrderRequest) -> BrokerAck:
        try:
            resp = await self.call("place_order", **order_kwargs(req), timeout=self.settings.order_ack_timeout_seconds)
        except BrokerRejected:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_place_error(exc) from exc
        return parse_place(resp)

    async def _cancel(self, broker_order_id: str) -> BrokerAck:
        resp = await self.call("cancel_order", order_id=broker_order_id)
        ok = isinstance(resp, dict) and str(resp.get("stat", "")).lower() == "ok"
        return BrokerAck(accepted=ok, broker_order_id=broker_order_id, status="CANCELLED" if ok else "UNKNOWN", message=str(resp)[:200])
