"""Zerodha Kite Connect (official SDK `kiteconnect`).

Kite has no option-chain endpoint: the chain is assembled from the instrument
master (`instruments("NFO"/"BFO"/"MCX")`) and batched `quote()` calls. The access
token is generated once per day through the OAuth request-token flow
(scripts/zerodha_login.py) and supplied as ZERODHA_ACCESS_TOKEN.
"""
from __future__ import annotations

import datetime as dt

from amrt.brokers.base import BrokerProfile, BrokerSession, CapabilityEntry, classify_place_error, normalise_status, num
from amrt.core.enums import DataLabel, FeatureStatus, OrderType, Product, Side
from amrt.core.errors import BrokerRejected, DataUnavailable
from amrt.execution.intents import BrokerOrderRequest
from amrt.execution.venue import BrokerAck, FundsReport, OrderStatusReport, PositionReport
from amrt.marketdata.models import ChainRow, ChainSnapshot, Instrument, OptionLeg, Quote

FS = FeatureStatus
EXCHANGE = {"NSE_FO": "NFO", "BSE_FO": "BFO", "MCX_FO": "MCX"}
INDEX_QUOTE = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "SENSEX": "BSE:SENSEX"}
STATUS = {"complete": "FILLED", "rejected": "REJECTED", "cancelled": "CANCELLED", "open": "OPEN", "trigger pending": "OPEN", "open pending": "OPEN",
          "validation pending": "OPEN", "put order req received": "OPEN", "modify pending": "OPEN", "modify validation pending": "OPEN",
          "cancel pending": "OPEN", "amo req received": "OPEN"}

PROFILE = BrokerProfile(
    name="zerodha", display="Zerodha Kite Connect", sdk_package="kiteconnect", sdk_version_tested="5.2.2", official_docs="https://kite.trade/docs/connect/v3/",
    api_version="Kite Connect v3", auth="API key + daily request_token → access_token (OAuth redirect)", instruments=["NFO", "BFO", "MCX", "NSE", "BSE"],
    order_types=["LIMIT", "MARKET", "SL", "SL-M"], market_data="REST quote()/ltp(); KiteTicker WebSocket", option_chain="assembled from instruments() + quote() (no chain endpoint)",
    historical_data="historical_data() (separate subscription)", websocket="KiteTicker — not used by AMRT v1", rate_limits="documented: ~10 req/s general, quote 1 req/s, orders 10/s",
    sandbox="none", tag_field="tag", tag_max_len=20,
    known_limitations=["access token expires daily", "quote() max 500 instruments per call", "no native option chain"],
    capabilities=[
        CapabilityEntry(feature="authentication", status=FS.PARTIAL, evidence="SDK signature contract test", notes="live BLOCKED: network policy"),
        CapabilityEntry(feature="option_chain", status=FS.PARTIAL, evidence="assembly + parser tests with documented shapes"),
        CapabilityEntry(feature="quotes", status=FS.PARTIAL, evidence="parser test (documented quote shape)"),
        CapabilityEntry(feature="orders", status=FS.BLOCKED, evidence="contract test (kwargs bind to SDK)"),
        CapabilityEntry(feature="order_book/positions/funds", status=FS.PARTIAL, evidence="parser tests with documented field names"),
        CapabilityEntry(feature="websocket", status=FS.UNAVAILABLE, evidence="not implemented in AMRT v1"),
    ],
    verification_date="2026-10-03", overall_status=FS.PARTIAL)


def order_kwargs(req: BrokerOrderRequest) -> dict:
    kw = {"variety": "regular", "exchange": req.broker_ref.get("exchange") or EXCHANGE[req.segment], "tradingsymbol": req.broker_ref.get("tradingsymbol") or req.trading_symbol,
          "transaction_type": "BUY" if req.side == Side.BUY else "SELL", "quantity": int(req.quantity), "product": "NRML" if req.product == Product.NRML else "MIS",
          "order_type": "LIMIT" if req.order_type == OrderType.LIMIT else "MARKET", "validity": req.validity.value, "tag": req.client_tag[:20]}
    if req.order_type == OrderType.LIMIT:
        kw["price"] = round(float(req.limit_price), 2)
    return kw


def parse_order_book(rows) -> list[OrderStatusReport]:
    out = []
    for r in rows or []:
        qty, filled = int(r.get("quantity") or 0), int(r.get("filled_quantity") or 0)
        tt = str(r.get("transaction_type", "")).upper()
        out.append(OrderStatusReport(broker_order_id=str(r.get("order_id", "")), client_tag=r.get("tag"), trading_symbol=str(r.get("tradingsymbol", "")),
                                     side=Side.BUY if tt == "BUY" else (Side.SELL if tt == "SELL" else None), quantity=qty, filled_qty=filled,
                                     avg_price=float(r.get("average_price") or 0.0), status=normalise_status(str(r.get("status", "")), filled, qty, STATUS),
                                     message=str(r.get("status_message") or "")[:200]))
    return out


def parse_positions(resp) -> list[PositionReport]:
    rows = (resp or {}).get("net", []) if isinstance(resp, dict) else []
    return [PositionReport(trading_symbol=str(r.get("tradingsymbol", "")), net_qty=int(r.get("quantity") or 0), avg_price=float(r.get("average_price") or 0.0))
            for r in rows if int(r.get("quantity") or 0) != 0]


def parse_funds(resp, ts: float) -> FundsReport:
    avail = used = 0.0
    seen = False
    for seg in ("equity", "commodity"):
        d = (resp or {}).get(seg) or {}
        if not d:
            continue
        seen = True
        avail += float(d.get("net") or 0.0)
        used += float((d.get("utilised") or {}).get("debits") or 0.0)
    return FundsReport(available=avail if seen else None, margin_used=used if seen else None, total=(avail + used) if seen else None, ts=ts)


def parse_quote(sym_key: str, q: dict, instrument_key: str, recv_ts: float) -> Quote | None:
    ltp = num(q.get("last_price"))
    if not ltp:
        return None
    depth = q.get("depth") or {}
    bid = num((depth.get("buy") or [{}])[0].get("price")) if depth.get("buy") else None
    ask = num((depth.get("sell") or [{}])[0].get("price")) if depth.get("sell") else None
    ts = q.get("last_trade_time") or q.get("timestamp")
    ex_ts = ts.timestamp() if isinstance(ts, dt.datetime) else None
    return Quote(instrument_key=instrument_key, ltp=ltp, bid=bid or None, ask=ask or None, volume=int(q.get("volume") or 0), oi=int(q.get("oi") or 0),
                 exchange_ts=ex_ts, recv_ts=recv_ts, source="zerodha", label=DataLabel.LIVE_UNVERIFIED)


class ZerodhaSession(BrokerSession):
    name = "zerodha"
    profile = PROFILE

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._master: dict[str, list[dict]] = {}

    def credentials_present(self) -> bool:
        return bool(self.settings.zerodha_api_key and self.settings.zerodha_access_token)

    def _login_sync(self) -> dict:
        s = self.settings
        if self._sdk_factory is not None:
            client = self._sdk_factory()
        else:
            from kiteconnect import KiteConnect
            client = KiteConnect(api_key=s.zerodha_api_key)
        client.set_access_token(s.zerodha_access_token)
        prof = client.profile()
        self._set_client(client)
        return {"ok": True, "user": (prof or {}).get("user_id")}

    async def _contracts(self, underlying: str) -> list[dict]:
        from amrt.marketdata.instruments import spec
        ex = EXCHANGE[spec(underlying).option_segment.value]
        if ex not in self._master:
            self._master[ex] = await self.call("instruments", ex, timeout=30)
        return [r for r in self._master[ex] if r.get("name") == underlying and r.get("instrument_type") in ("CE", "PE")]

    async def fetch_expiries(self, underlying: str) -> list[str]:
        return sorted({(r["expiry"].isoformat() if isinstance(r["expiry"], dt.date) else str(r["expiry"])) for r in await self._contracts(underlying)})

    async def fetch_chain(self, underlying: str, expiry_iso: str, instruments) -> tuple[ChainSnapshot, Quote | None]:
        from amrt.marketdata.instruments import spec
        sp = spec(underlying)
        exp = dt.date.fromisoformat(expiry_iso)
        rows = [r for r in await self._contracts(underlying) if (r["expiry"].isoformat() if isinstance(r["expiry"], dt.date) else str(r["expiry"])) == expiry_iso]
        if not rows:
            raise DataUnavailable("DATA UNAVAILABLE", reason=f"no {underlying} contracts for {expiry_iso}")
        ex = EXCHANGE[sp.option_segment.value]
        keys = [f"{ex}:{r['tradingsymbol']}" for r in rows]
        quotes: dict = {}
        for i in range(0, len(keys), 450):
            quotes.update(await self.call("quote", *keys[i:i + 450], timeout=15))
        idx = INDEX_QUOTE.get(underlying)
        if idx:
            quotes.update(await self.call("quote", idx))
        now = self.clock.ts()
        by_strike: dict[float, dict] = {}
        for r in rows:
            k = float(r["strike"])
            ot = r["instrument_type"]
            ikey = Instrument.option_key(sp.exchange, underlying, exp, k, ot)
            q = quotes.get(f"{ex}:{r['tradingsymbol']}") or {}
            instruments.add(instruments.option(underlying, exp, k, ot), source="zerodha")
            instruments.set_broker_ref(ikey, "zerodha", {"tradingsymbol": r["tradingsymbol"], "exchange": ex, "instrument_token": str(r.get("instrument_token"))})
            depth = q.get("depth") or {}
            leg = OptionLeg(instrument_key=ikey, ltp=num(q.get("last_price")), bid=num((depth.get("buy") or [{}])[0].get("price")) if depth.get("buy") else None,
                            ask=num((depth.get("sell") or [{}])[0].get("price")) if depth.get("sell") else None, volume=q.get("volume"), oi=q.get("oi"),
                            oi_change=None)  # Kite quotes carry no previous-day OI; change-in-OI PCR is unavailable from this source
            by_strike.setdefault(k, {})[ot] = leg
        spot = num((quotes.get(idx) or {}).get("last_price")) if idx else None
        snap = ChainSnapshot(underlying=underlying, exchange=sp.exchange, expiry=exp, spot=spot, ts=now, source="zerodha", label=DataLabel.LIVE_UNVERIFIED,
                             rows=[ChainRow(strike=k, ce=v.get("CE"), pe=v.get("PE")) for k, v in sorted(by_strike.items())])
        spot_q = parse_quote(idx, quotes[idx], Instrument.underlying_key(sp.exchange, underlying), now) if idx and quotes.get(idx) else None
        return snap, spot_q

    async def fetch_quotes(self, instruments: list) -> list[Quote]:
        keys = {f"{i.broker_refs['zerodha']['exchange']}:{i.broker_refs['zerodha']['tradingsymbol']}": i for i in instruments if "zerodha" in i.broker_refs}
        if not keys:
            return []
        resp = await self.call("quote", *keys.keys())
        now = self.clock.ts()
        return [q for k, inst in keys.items() if (q := parse_quote(k, resp.get(k) or {}, inst.key, now)) is not None]

    async def fetch_order_book(self) -> list[OrderStatusReport]:
        return parse_order_book(await self.call("orders"))

    async def fetch_positions(self) -> list[PositionReport]:
        return parse_positions(await self.call("positions"))

    async def fetch_funds(self) -> FundsReport:
        return parse_funds(await self.call("margins"), self.clock.ts())

    async def _place(self, req: BrokerOrderRequest) -> BrokerAck:
        try:
            oid = await self.call("place_order", **order_kwargs(req), timeout=self.settings.order_ack_timeout_seconds)
        except BrokerRejected:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_place_error(exc) from exc
        if not oid:
            return BrokerAck(accepted=False, status="UNKNOWN", message="no order id returned")
        return BrokerAck(accepted=True, broker_order_id=str(oid), status="OPEN", message="accepted")

    async def _cancel(self, broker_order_id: str) -> BrokerAck:
        oid = await self.call("cancel_order", variety="regular", order_id=broker_order_id)
        return BrokerAck(accepted=bool(oid), broker_order_id=broker_order_id, status="CANCELLED" if oid else "UNKNOWN")
