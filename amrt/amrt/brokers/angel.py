"""Angel One SmartAPI (official SDK `smartapi-python`, module `SmartApi`).

SmartAPI has no option-chain endpoint: the chain is assembled from Angel's
published scrip master (OpenAPIScripMaster.json) and `getMarketData("FULL", …)`
in batches of 50 tokens. Login: client code + PIN + TOTP.
"""
from __future__ import annotations

import datetime as dt

import pyotp

from amrt.brokers.base import BrokerProfile, BrokerSession, CapabilityEntry, classify_place_error, normalise_status, num, pick
from amrt.core.enums import DataLabel, FeatureStatus, OrderType, Product, Side
from amrt.core.errors import BrokerRejected, DataUnavailable
from amrt.execution.intents import BrokerOrderRequest
from amrt.execution.venue import BrokerAck, FundsReport, OrderStatusReport, PositionReport
from amrt.marketdata.models import ChainRow, ChainSnapshot, Instrument, OptionLeg, Quote

FS = FeatureStatus
EXCHANGE = {"NSE_FO": "NFO", "BSE_FO": "BFO", "MCX_FO": "MCX"}
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
STATUS = {"complete": "FILLED", "rejected": "REJECTED", "cancelled": "CANCELLED", "open": "OPEN", "trigger pending": "OPEN", "open pending": "OPEN",
          "validation pending": "OPEN", "put order req received": "OPEN", "modified": "OPEN", "after market order req received": "OPEN"}

PROFILE = BrokerProfile(
    name="angel", display="Angel One SmartAPI", sdk_package="smartapi-python (SmartApi)", sdk_version_tested="1.5.5", official_docs="https://smartapi.angelbroking.com/docs",
    api_version="SmartAPI REST v1", auth="API key + client code + PIN + TOTP → JWT session", instruments=["NFO", "BFO", "MCX", "NSE", "BSE"],
    order_types=["LIMIT", "MARKET", "STOPLOSS_LIMIT", "STOPLOSS_MARKET"], market_data="REST getMarketData(LTP/OHLC/FULL); SmartWebSocketV2",
    option_chain="assembled from scrip master + getMarketData (no chain endpoint)", historical_data="getCandleData() (not used)", websocket="SmartWebSocketV2 — not used by AMRT v1",
    rate_limits="documented per-endpoint limits (e.g. orders 20/s, market data 10/s)", sandbox="none", tag_field="ordertag", tag_max_len=20,
    known_limitations=["prices in REST are rupees; WebSocket prices are paise", "scrip master must be downloaded daily", "getMarketData max 50 tokens/call"],
    capabilities=[
        CapabilityEntry(feature="authentication", status=FS.PARTIAL, evidence="SDK signature contract test", notes="live BLOCKED: network policy"),
        CapabilityEntry(feature="option_chain", status=FS.PARTIAL, evidence="assembly + parser tests with documented shapes"),
        CapabilityEntry(feature="orders", status=FS.BLOCKED, evidence="contract test (orderparams keys)"),
        CapabilityEntry(feature="order_book/positions/funds", status=FS.PARTIAL, evidence="parser tests with documented field names"),
        CapabilityEntry(feature="websocket", status=FS.UNAVAILABLE, evidence="not implemented in AMRT v1"),
    ],
    verification_date="2026-10-03", overall_status=FS.PARTIAL)


def order_params(req: BrokerOrderRequest) -> dict:
    p = {"variety": "NORMAL", "tradingsymbol": req.broker_ref.get("tradingsymbol") or req.trading_symbol, "symboltoken": req.broker_ref.get("symboltoken", ""),
         "transactiontype": "BUY" if req.side == Side.BUY else "SELL", "exchange": req.broker_ref.get("exchange") or EXCHANGE[req.segment],
         "ordertype": "LIMIT" if req.order_type == OrderType.LIMIT else "MARKET", "producttype": "CARRYFORWARD" if req.product == Product.NRML else "INTRADAY",
         "duration": req.validity.value, "price": f"{req.limit_price:.2f}" if req.order_type == OrderType.LIMIT else "0", "quantity": str(req.quantity),
         "ordertag": req.client_tag[:20]}
    return p


def _data(resp):
    if isinstance(resp, dict):
        if resp.get("status") is False or (resp.get("message") and str(resp.get("message")).upper() not in ("SUCCESS", "")):
            if resp.get("data") is None:
                raise DataUnavailable("DATA UNAVAILABLE", reason=str(resp.get("message"))[:200])
        return resp.get("data")
    return resp


def parse_order_book(resp) -> list[OrderStatusReport]:
    out = []
    for r in _data(resp) or []:
        qty, filled = int(num(r.get("quantity"), 0) or 0), int(num(r.get("filledshares"), 0) or 0)
        tt = str(r.get("transactiontype", "")).upper()
        out.append(OrderStatusReport(broker_order_id=str(r.get("orderid", "")), client_tag=r.get("ordertag"), trading_symbol=str(r.get("tradingsymbol", "")),
                                     side=Side.BUY if tt == "BUY" else (Side.SELL if tt == "SELL" else None), quantity=qty, filled_qty=filled,
                                     avg_price=num(r.get("averageprice"), 0.0) or 0.0, status=normalise_status(str(r.get("status", "")), filled, qty, STATUS),
                                     message=str(r.get("text") or "")[:200]))
    return out


def parse_positions(resp) -> list[PositionReport]:
    out = []
    for r in _data(resp) or []:
        net = int(num(r.get("netqty"), 0) or 0)
        if net:
            out.append(PositionReport(trading_symbol=str(r.get("tradingsymbol", "")), net_qty=net, avg_price=num(pick(r, "avgnetprice", "netprice"), 0.0) or 0.0))
    return out


def parse_funds(resp, ts: float) -> FundsReport:
    d = _data(resp) or {}
    avail = num(pick(d, "availablecash", "net"))
    used = num(pick(d, "utiliseddebits", "utilisedpayout"))
    return FundsReport(available=avail, margin_used=used, total=(avail + used) if avail is not None and used is not None else None, ts=ts)


class AngelSession(BrokerSession):
    name = "angel"
    profile = PROFILE

    def __init__(self, *a, master_loader=None, **k) -> None:
        super().__init__(*a, **k)
        self._master_loader = master_loader
        self._master: list[dict] | None = None

    def credentials_present(self) -> bool:
        s = self.settings
        return all([s.angel_api_key, s.angel_client_code, s.angel_pin, s.angel_totp_secret])

    def _login_sync(self) -> dict:
        s = self.settings
        if self._sdk_factory is not None:
            client = self._sdk_factory()
        else:
            from SmartApi import SmartConnect
            client = SmartConnect(api_key=s.angel_api_key)
        res = client.generateSession(s.angel_client_code, s.angel_pin, pyotp.TOTP(s.angel_totp_secret).now())
        if isinstance(res, dict) and res.get("status") is False:
            raise BrokerRejected(str(res.get("message"))[:200])
        self._set_client(client)
        return {"ok": True}

    async def _contracts(self, underlying: str) -> list[dict]:
        if self._master is None:
            if self._master_loader is not None:
                self._master = self._master_loader()
            else:
                import httpx
                async with httpx.AsyncClient(timeout=60) as c:
                    self._master = (await c.get(SCRIP_MASTER_URL)).json()
        return [r for r in self._master if r.get("name") == underlying and r.get("instrumenttype") in ("OPTIDX", "OPTFUT", "OPTSTK")]

    @staticmethod
    def _expiry(r: dict) -> str:
        return dt.datetime.strptime(r["expiry"], "%d%b%Y").date().isoformat()

    async def fetch_expiries(self, underlying: str) -> list[str]:
        return sorted({self._expiry(r) for r in await self._contracts(underlying)})

    async def fetch_chain(self, underlying: str, expiry_iso: str, instruments) -> tuple[ChainSnapshot, Quote | None]:
        from amrt.marketdata.instruments import spec
        sp = spec(underlying)
        exp = dt.date.fromisoformat(expiry_iso)
        rows = [r for r in await self._contracts(underlying) if self._expiry(r) == expiry_iso]
        if not rows:
            raise DataUnavailable("DATA UNAVAILABLE", reason=f"no {underlying} contracts for {expiry_iso}")
        ex = EXCHANGE[sp.option_segment.value]
        fetched: dict[str, dict] = {}
        toks = [r["token"] for r in rows]
        for i in range(0, len(toks), 50):
            resp = await self.call("getMarketData", "FULL", {ex: toks[i:i + 50]})
            for q in ((_data(resp) or {}).get("fetched") or []):
                fetched[str(q.get("symbolToken"))] = q
        now = self.clock.ts()
        by_strike: dict[float, dict] = {}
        for r in rows:
            k = num(r.get("strike")) / 100.0      # scrip master strikes are in paise
            ot = r["symbol"][-2:]
            ikey = Instrument.option_key(sp.exchange, underlying, exp, k, ot)
            instruments.add(instruments.option(underlying, exp, k, ot), source="angel")
            instruments.set_broker_ref(ikey, "angel", {"tradingsymbol": r["symbol"], "symboltoken": str(r["token"]), "exchange": ex})
            q = fetched.get(str(r["token"])) or {}
            depth = q.get("depth") or {}
            by_strike.setdefault(k, {})[ot] = OptionLeg(instrument_key=ikey, ltp=num(q.get("ltp")), bid=num((depth.get("buy") or [{}])[0].get("price")) if depth.get("buy") else None,
                                                        ask=num((depth.get("sell") or [{}])[0].get("price")) if depth.get("sell") else None,
                                                        volume=int(num(q.get("tradeVolume"), 0) or 0) if q else None, oi=int(num(q.get("opnInterest"), 0) or 0) if q else None)
        snap = ChainSnapshot(underlying=underlying, exchange=sp.exchange, expiry=exp, spot=None, ts=now, source="angel", label=DataLabel.LIVE_UNVERIFIED,
                             rows=[ChainRow(strike=k, ce=v.get("CE"), pe=v.get("PE")) for k, v in sorted(by_strike.items())])
        return snap, None

    async def fetch_quotes(self, instruments: list) -> list[Quote]:
        refs = [i for i in instruments if "angel" in i.broker_refs]
        if not refs:
            return []
        by_ex: dict[str, list[str]] = {}
        for i in refs:
            by_ex.setdefault(i.broker_refs["angel"]["exchange"], []).append(i.broker_refs["angel"]["symboltoken"])
        resp = await self.call("getMarketData", "FULL", by_ex)
        by_tok = {i.broker_refs["angel"]["symboltoken"]: i for i in refs}
        now = self.clock.ts()
        out = []
        for q in ((_data(resp) or {}).get("fetched") or []):
            inst = by_tok.get(str(q.get("symbolToken")))
            ltp = num(q.get("ltp"))
            if inst and ltp:
                out.append(Quote(instrument_key=inst.key, ltp=ltp, volume=int(num(q.get("tradeVolume"), 0) or 0), oi=int(num(q.get("opnInterest"), 0) or 0),
                                 recv_ts=now, source="angel", label=DataLabel.LIVE_UNVERIFIED))
        return out

    async def fetch_order_book(self) -> list[OrderStatusReport]:
        return parse_order_book(await self.call("orderBook"))

    async def fetch_positions(self) -> list[PositionReport]:
        return parse_positions(await self.call("position"))

    async def fetch_funds(self) -> FundsReport:
        return parse_funds(await self.call("rmsLimit"), self.clock.ts())

    async def _place(self, req: BrokerOrderRequest) -> BrokerAck:
        try:
            oid = await self.call("placeOrder", order_params(req), timeout=self.settings.order_ack_timeout_seconds)
        except BrokerRejected:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_place_error(exc) from exc
        if not oid:
            return BrokerAck(accepted=False, status="UNKNOWN", message="no order id returned")
        return BrokerAck(accepted=True, broker_order_id=str(oid), status="OPEN", message="accepted")

    async def _cancel(self, broker_order_id: str) -> BrokerAck:
        resp = await self.call("cancelOrder", broker_order_id, "NORMAL")
        ok = isinstance(resp, dict) and resp.get("status") is True
        return BrokerAck(accepted=ok, broker_order_id=broker_order_id, status="CANCELLED" if ok else "UNKNOWN", message=str(resp)[:200])
