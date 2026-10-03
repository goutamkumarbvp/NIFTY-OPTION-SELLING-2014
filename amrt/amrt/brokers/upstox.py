"""Upstox (official SDK `upstox-python-sdk`, module `upstox_client`).

Uses the official put/call option chain endpoint, whose market data includes
bid/ask, OI and previous-day OI (so OI change is derived as oi − prev_oi) plus
broker-computed Greeks. Field names are taken from the SDK models
(`MarketData`, `AnalyticsData`, `OrderBookData`, `PositionData`, `UserFundMarginData`).
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
UNDERLYING_KEY = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank", "SENSEX": "BSE_INDEX|SENSEX"}
STATUS = {"complete": "FILLED", "rejected": "REJECTED", "cancelled": "CANCELLED", "open": "OPEN", "trigger pending": "OPEN", "open pending": "OPEN",
          "validation pending": "OPEN", "put order req received": "OPEN", "modify pending": "OPEN", "after market order req received": "OPEN"}

PROFILE = BrokerProfile(
    name="upstox", display="Upstox", sdk_package="upstox-python-sdk (upstox_client)", sdk_version_tested="2.30.0", official_docs="https://upstox.com/developer/api-documentation/",
    api_version="v2 REST", auth="OAuth 2 daily access token", instruments=["NSE_FO", "BSE_FO", "MCX_FO", "NSE_EQ"], order_types=["LIMIT", "MARKET", "SL", "SL-M"],
    market_data="REST full market quote / LTP; WebSocket v3 feed", option_chain="REST get_put_call_option_chain(instrument_key, expiry_date) with Greeks and prev_oi",
    historical_data="REST historical candles (not used)", websocket="Market data feed v3 — not used by AMRT v1", rate_limits="documented per-second/minute limits per API",
    sandbox="sandbox for orders documented by Upstox; not verified", tag_field="tag", tag_max_len=20,
    known_limitations=["access token expires daily", "MCX option chain availability not verified"],
    capabilities=[
        CapabilityEntry(feature="authentication", status=FS.PARTIAL, evidence="SDK model/signature contract test", notes="live BLOCKED: network policy"),
        CapabilityEntry(feature="option_chain", status=FS.PARTIAL, evidence="parser test using SDK model field names (MarketData, AnalyticsData)"),
        CapabilityEntry(feature="orders", status=FS.BLOCKED, evidence="contract test (PlaceOrderRequest fields)"),
        CapabilityEntry(feature="order_book/positions/funds", status=FS.PARTIAL, evidence="parser tests using SDK model field names"),
        CapabilityEntry(feature="websocket", status=FS.UNAVAILABLE, evidence="not implemented in AMRT v1"),
    ],
    verification_date="2026-10-03", overall_status=FS.PARTIAL)


def _get(o, name, default=None):
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


def order_body_kwargs(req: BrokerOrderRequest) -> dict:
    return {"quantity": int(req.quantity), "product": "D" if req.product == Product.NRML else "I", "validity": req.validity.value,
            "price": float(req.limit_price) if req.order_type == OrderType.LIMIT else 0.0, "tag": req.client_tag[:20],
            "instrument_token": req.broker_ref.get("instrument_key", ""), "order_type": "LIMIT" if req.order_type == OrderType.LIMIT else "MARKET",
            "transaction_type": "BUY" if req.side == Side.BUY else "SELL", "disclosed_quantity": 0, "trigger_price": 0.0, "is_amo": False}


def parse_chain(data, underlying: str, exchange, expiry: dt.date, recv_ts: float) -> tuple[ChainSnapshot, dict[str, dict]]:
    rows, refs, spot = [], {}, None
    for r in data or []:
        k = num(_get(r, "strike_price"))
        if k is None:
            continue
        spot = spot or num(_get(r, "underlying_spot_price"))
        legs = {}
        for ot, attr in (("CE", "call_options"), ("PE", "put_options")):
            o = _get(r, attr)
            if not o:
                continue
            md, gk = _get(o, "market_data") or {}, _get(o, "option_greeks") or {}
            ikey = Instrument.option_key(exchange, underlying, expiry, k, ot)
            oi, prev = num(_get(md, "oi")), num(_get(md, "prev_oi"))
            legs[ot] = OptionLeg(instrument_key=ikey, ltp=num(_get(md, "ltp")), bid=num(_get(md, "bid_price")), ask=num(_get(md, "ask_price")),
                                 volume=int(num(_get(md, "volume"), 0) or 0), oi=int(oi) if oi is not None else None,
                                 oi_change=int(oi - prev) if oi is not None and prev is not None else None, iv=num(_get(gk, "iv")), delta=num(_get(gk, "delta")),
                                 gamma=num(_get(gk, "gamma")), theta=num(_get(gk, "theta")), vega=num(_get(gk, "vega")))
            refs[ikey] = {"instrument_key": str(_get(o, "instrument_key") or "")}
        rows.append(ChainRow(strike=k, ce=legs.get("CE"), pe=legs.get("PE")))
    if not rows:
        raise DataUnavailable("DATA UNAVAILABLE", reason="empty upstox option chain")
    return ChainSnapshot(underlying=underlying, exchange=exchange, expiry=expiry, spot=spot, ts=recv_ts, source="upstox", label=DataLabel.LIVE_UNVERIFIED, rows=rows), refs


def parse_order_book(rows) -> list[OrderStatusReport]:
    out = []
    for r in rows or []:
        qty, filled = int(_get(r, "quantity") or 0), int(_get(r, "filled_quantity") or 0)
        tt = str(_get(r, "transaction_type") or "").upper()
        out.append(OrderStatusReport(broker_order_id=str(_get(r, "order_id") or ""), client_tag=_get(r, "tag"), trading_symbol=str(_get(r, "trading_symbol") or _get(r, "tradingsymbol") or ""),
                                     side=Side.BUY if tt == "BUY" else (Side.SELL if tt == "SELL" else None), quantity=qty, filled_qty=filled,
                                     avg_price=float(_get(r, "average_price") or 0.0), status=normalise_status(str(_get(r, "status") or ""), filled, qty, STATUS),
                                     message=str(_get(r, "status_message") or "")[:200]))
    return out


def parse_positions(rows) -> list[PositionReport]:
    return [PositionReport(trading_symbol=str(_get(r, "trading_symbol") or _get(r, "tradingsymbol") or ""), net_qty=int(_get(r, "quantity") or 0),
                           avg_price=float(_get(r, "average_price") or 0.0)) for r in rows or [] if int(_get(r, "quantity") or 0) != 0]


def parse_funds(data, ts: float) -> FundsReport:
    avail = used = 0.0
    seen = False
    for seg in ("equity", "commodity"):
        d = _get(data, seg)
        if not d:
            continue
        seen = True
        avail += float(_get(d, "available_margin") or 0.0)
        used += float(_get(d, "used_margin") or 0.0)
    return FundsReport(available=avail if seen else None, margin_used=used if seen else None, total=(avail + used) if seen else None, ts=ts)


class UpstoxSession(BrokerSession):
    name = "upstox"
    profile = PROFILE
    API_VERSION = "2.0"

    def credentials_present(self) -> bool:
        return bool(self.settings.upstox_access_token)

    def _login_sync(self) -> dict:
        if self._sdk_factory is not None:
            apis = self._sdk_factory()
        else:
            import upstox_client
            cfg = upstox_client.Configuration()
            cfg.access_token = self.settings.upstox_access_token
            api = upstox_client.ApiClient(cfg)
            apis = type("UpstoxApis", (), {})()
            apis.orders, apis.portfolio, apis.user = upstox_client.OrderApi(api), upstox_client.PortfolioApi(api), upstox_client.UserApi(api)
            apis.quotes, apis.options = upstox_client.MarketQuoteApi(api), upstox_client.OptionsApi(api)
        apis.user.get_profile(self.API_VERSION)
        self._set_client(apis)
        return {"ok": True}

    async def _api(self, group: str, method: str, *args, timeout: float = 10.0, **kw):
        import asyncio
        fn = getattr(getattr(self._client(), group), method)
        self.calls += 1
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kw), timeout)
        except TimeoutError as exc:
            self.errors += 1
            from amrt.core.errors import BrokerTimeout
            raise BrokerTimeout(f"upstox.{method} timed out") from exc

    async def fetch_expiries(self, underlying: str) -> list[str]:
        key = UNDERLYING_KEY.get(underlying)
        if key is None:
            raise DataUnavailable("DATA UNAVAILABLE", reason=f"no upstox instrument key for {underlying}")
        resp = await self._api("options", "get_option_contracts", key)
        return sorted({str(_get(c, "expiry"))[:10] for c in (_get(resp, "data") or [])})

    async def fetch_chain(self, underlying: str, expiry_iso: str, instruments) -> tuple[ChainSnapshot, Quote | None]:
        from amrt.marketdata.instruments import spec
        sp = spec(underlying)
        key = UNDERLYING_KEY.get(underlying)
        if key is None:
            raise DataUnavailable("DATA UNAVAILABLE", reason=f"no upstox instrument key for {underlying}")
        resp = await self._api("options", "get_put_call_option_chain", key, expiry_iso, timeout=15)
        exp = dt.date.fromisoformat(expiry_iso)
        snap, refs = parse_chain(_get(resp, "data"), underlying, sp.exchange, exp, self.clock.ts())
        for ikey, ref in refs.items():
            p = ikey.split(":")
            instruments.add(instruments.option(underlying, exp, float(p[3]), p[4]), source="upstox")
            instruments.set_broker_ref(ikey, "upstox", ref)
        spot_q = Quote(instrument_key=Instrument.underlying_key(sp.exchange, underlying), ltp=snap.spot, recv_ts=snap.ts, source="upstox",
                       label=DataLabel.LIVE_UNVERIFIED) if snap.spot else None
        return snap, spot_q

    async def fetch_quotes(self, instruments: list) -> list[Quote]:
        refs = {i.broker_refs["upstox"]["instrument_key"]: i for i in instruments if "upstox" in i.broker_refs}
        if not refs:
            return []
        resp = await self._api("quotes", "get_full_market_quote", ",".join(refs), self.API_VERSION)
        now = self.clock.ts()
        out = []
        for _, q in (_get(resp, "data") or {}).items():
            inst = refs.get(str(_get(q, "instrument_token")))
            ltp = num(_get(q, "last_price"))
            if inst and ltp:
                out.append(Quote(instrument_key=inst.key, ltp=ltp, volume=int(_get(q, "volume") or 0), oi=int(num(_get(q, "oi"), 0) or 0), recv_ts=now,
                                 source="upstox", label=DataLabel.LIVE_UNVERIFIED))
        return out

    async def fetch_order_book(self) -> list[OrderStatusReport]:
        return parse_order_book(_get(await self._api("orders", "get_order_book", self.API_VERSION), "data"))

    async def fetch_positions(self) -> list[PositionReport]:
        return parse_positions(_get(await self._api("portfolio", "get_positions", self.API_VERSION), "data"))

    async def fetch_funds(self) -> FundsReport:
        return parse_funds(_get(await self._api("user", "get_user_fund_margin", self.API_VERSION), "data"), self.clock.ts())

    async def _place(self, req: BrokerOrderRequest) -> BrokerAck:
        import upstox_client
        body = upstox_client.PlaceOrderRequest(**order_body_kwargs(req))
        try:
            resp = await self._api("orders", "place_order", body, self.API_VERSION, timeout=self.settings.order_ack_timeout_seconds)
        except BrokerRejected:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_place_error(exc) from exc
        oid = _get(_get(resp, "data") or {}, "order_id")
        if not oid:
            return BrokerAck(accepted=False, status="UNKNOWN", message=str(resp)[:200])
        return BrokerAck(accepted=True, broker_order_id=str(oid), status="OPEN", message="accepted")

    async def _cancel(self, broker_order_id: str) -> BrokerAck:
        resp = await self._api("orders", "cancel_order", broker_order_id, self.API_VERSION)
        ok = str(_get(resp, "status") or "").lower() == "success"
        return BrokerAck(accepted=ok, broker_order_id=broker_order_id, status="CANCELLED" if ok else "UNKNOWN")
