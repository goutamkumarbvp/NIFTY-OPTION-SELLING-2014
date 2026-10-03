"""Groww Trade API (official SDK `growwapi`).

Included only for features the official SDK exposes: access token, orders with a
caller `order_reference_id`, order list/status, positions, margin, quotes and an
option-chain endpoint. Response shapes are not documented in the SDK; parsers are
defensive and every Groww feature is PARTIAL/BLOCKED until verified live.
"""
from __future__ import annotations

import datetime as dt

from amrt.brokers.base import BrokerProfile, BrokerSession, CapabilityEntry, classify_place_error, normalise_status, num, pick
from amrt.core.enums import DataLabel, FeatureStatus, OrderType, Product, Side
from amrt.core.errors import BrokerRejected, DataUnavailable
from amrt.execution.intents import BrokerOrderRequest
from amrt.execution.venue import BrokerAck, FundsReport, OrderStatusReport, PositionReport
from amrt.marketdata.models import ChainRow, ChainSnapshot, Instrument, OptionLeg, Quote

FS = FeatureStatus
STATUS = {"executed": "FILLED", "completed": "FILLED", "complete": "FILLED", "rejected": "REJECTED", "failed": "REJECTED", "cancelled": "CANCELLED",
          "open": "OPEN", "new": "OPEN", "acked": "OPEN", "approved": "OPEN", "trigger_pending": "OPEN", "modification_requested": "OPEN"}

PROFILE = BrokerProfile(
    name="groww", display="Groww Trade API", sdk_package="growwapi", sdk_version_tested="1.5.0", official_docs="https://groww.in/trade-api/docs",
    api_version="Groww Trade API v1 (SDK 1.5.0)", auth="API key + secret (or TOTP) → access token", instruments=["NSE", "BSE", "MCX (per SDK constants)"],
    order_types=["LIMIT", "MARKET", "SL", "SL_M"], market_data="REST get_quote/get_ltp; GrowwFeed", option_chain="REST get_option_chain(exchange, underlying, expiry_date)",
    historical_data="REST candles (not used)", websocket="GrowwFeed — not used by AMRT v1", rate_limits="not stated in SDK", sandbox="none known",
    tag_field="order_reference_id", tag_max_len=20,
    known_limitations=["response shapes not documented in the SDK", "official feature coverage for MCX options not verified"],
    capabilities=[
        CapabilityEntry(feature="authentication", status=FS.PARTIAL, evidence="SDK signature contract test", notes="live BLOCKED: network policy"),
        CapabilityEntry(feature="option_chain", status=FS.PARTIAL, evidence="defensive parser; payload shape unverified"),
        CapabilityEntry(feature="orders", status=FS.BLOCKED, evidence="contract test (kwargs bind to SDK)"),
        CapabilityEntry(feature="order_book/positions/funds", status=FS.PARTIAL, evidence="defensive parsers; shapes unverified"),
        CapabilityEntry(feature="websocket", status=FS.UNAVAILABLE, evidence="not implemented in AMRT v1"),
    ],
    verification_date="2026-10-03", overall_status=FS.PARTIAL)


def order_kwargs(req: BrokerOrderRequest) -> dict:
    return {"validity": req.validity.value, "exchange": req.exchange, "order_type": "LIMIT" if req.order_type == OrderType.LIMIT else "MARKET",
            "product": "NRML" if req.product == Product.NRML else "MIS", "quantity": int(req.quantity), "segment": "COMMODITY" if req.exchange == "MCX" else "FNO",
            "trading_symbol": req.broker_ref.get("trading_symbol") or req.trading_symbol, "transaction_type": "BUY" if req.side == Side.BUY else "SELL",
            "order_reference_id": req.client_tag[:20], "price": float(req.limit_price) if req.order_type == OrderType.LIMIT else 0.0}


def _list(resp, *keys):
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in keys:
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def parse_order_book(resp) -> list[OrderStatusReport]:
    out = []
    for r in _list(resp, "order_list", "orders", "data"):
        qty, filled = int(num(pick(r, "quantity"), 0) or 0), int(num(pick(r, "filled_quantity"), 0) or 0)
        tt = str(pick(r, "transaction_type", default="")).upper()
        out.append(OrderStatusReport(broker_order_id=str(pick(r, "groww_order_id", "order_id", default="")), client_tag=pick(r, "order_reference_id"),
                                     trading_symbol=str(pick(r, "trading_symbol", default="")), side=Side.BUY if tt == "BUY" else (Side.SELL if tt == "SELL" else None),
                                     quantity=qty, filled_qty=filled, avg_price=num(pick(r, "average_fill_price", "average_price"), 0.0) or 0.0,
                                     status=normalise_status(str(pick(r, "order_status", "status", default="")), filled, qty, STATUS),
                                     message=str(pick(r, "remark", "message", default=""))[:200]))
    return out


def parse_positions(resp) -> list[PositionReport]:
    out = []
    for r in _list(resp, "positions", "data"):
        net = int(num(pick(r, "quantity", "net_quantity"), 0) or 0)
        if net:
            out.append(PositionReport(trading_symbol=str(pick(r, "trading_symbol", default="")), net_qty=net, avg_price=num(pick(r, "net_price", "average_price"), 0.0) or 0.0))
    return out


def parse_funds(resp, ts: float) -> FundsReport:
    d = resp if isinstance(resp, dict) else {}
    avail = num(pick(d, "clear_cash", "available_margin"))
    used = num(pick(d, "net_margin_used", "used_margin"))
    return FundsReport(available=avail, margin_used=used, total=(avail + used) if avail is not None and used is not None else None, ts=ts)


def parse_chain(resp, underlying: str, exchange, expiry: dt.date, recv_ts: float) -> tuple[ChainSnapshot, dict]:
    d = resp if isinstance(resp, dict) else {}
    strikes = d.get("strikes") or d.get("data") or d.get("option_chain") or {}
    items = strikes.items() if isinstance(strikes, dict) else [(pick(s, "strike_price", "strike"), s) for s in strikes]
    rows, refs = [], {}
    for k_raw, s in items:
        k = num(k_raw)
        if k is None or not isinstance(s, dict):
            continue
        legs = {}
        for ot in ("CE", "PE"):
            o = s.get(ot) or s.get(ot.lower())
            if not isinstance(o, dict):
                continue
            ikey = Instrument.option_key(exchange, underlying, expiry, k, ot)
            g = o.get("greeks") or {}
            legs[ot] = OptionLeg(instrument_key=ikey, ltp=num(pick(o, "ltp", "last_price")), oi=int(num(pick(o, "open_interest", "oi"), 0) or 0) if pick(o, "open_interest", "oi") is not None else None,
                                 volume=int(num(pick(o, "volume"), 0) or 0) if pick(o, "volume") is not None else None, iv=num(pick(g, "iv")), delta=num(pick(g, "delta")),
                                 gamma=num(pick(g, "gamma")), theta=num(pick(g, "theta")), vega=num(pick(g, "vega")))
            if pick(o, "trading_symbol"):
                refs[ikey] = {"trading_symbol": str(pick(o, "trading_symbol")), "exchange": exchange.value}
        rows.append(ChainRow(strike=k, ce=legs.get("CE"), pe=legs.get("PE")))
    if not rows:
        raise DataUnavailable("DATA UNAVAILABLE", reason="groww option chain payload not understood")
    spot = num(pick(d, "underlying_ltp", "spot_price", "underlying_price"))
    return ChainSnapshot(underlying=underlying, exchange=exchange, expiry=expiry, spot=spot, ts=recv_ts, source="groww", label=DataLabel.LIVE_UNVERIFIED,
                         rows=sorted(rows, key=lambda r: r.strike)), refs


class GrowwSession(BrokerSession):
    name = "groww"
    profile = PROFILE

    def credentials_present(self) -> bool:
        s = self.settings
        return bool(s.groww_access_token or (s.groww_api_key and s.groww_api_secret))

    def _login_sync(self) -> dict:
        s = self.settings
        if self._sdk_factory is not None:
            client = self._sdk_factory()
        else:
            from growwapi import GrowwAPI
            token = s.groww_access_token or GrowwAPI.get_access_token(api_key=s.groww_api_key, secret=s.groww_api_secret)
            client = GrowwAPI(token if isinstance(token, str) else str(pick(token, "token", "access_token", default="")))
        self._set_client(client)
        return {"ok": True}

    async def fetch_expiries(self, underlying: str) -> list[str]:
        from amrt.marketdata.instruments import spec
        resp = await self.call("get_expiries", exchange=spec(underlying).exchange.value, underlying_symbol=underlying)
        return sorted({str(x)[:10] for x in _list(resp, "expiries", "data")})

    async def fetch_chain(self, underlying: str, expiry_iso: str, instruments) -> tuple[ChainSnapshot, Quote | None]:
        from amrt.marketdata.instruments import spec
        sp = spec(underlying)
        resp = await self.call("get_option_chain", exchange=sp.exchange.value, underlying=underlying, expiry_date=expiry_iso, timeout=15)
        exp = dt.date.fromisoformat(expiry_iso)
        snap, refs = parse_chain(resp, underlying, sp.exchange, exp, self.clock.ts())
        for ikey, ref in refs.items():
            p = ikey.split(":")
            instruments.add(instruments.option(underlying, exp, float(p[3]), p[4]), source="groww")
            instruments.set_broker_ref(ikey, "groww", ref)
        spot_q = Quote(instrument_key=Instrument.underlying_key(sp.exchange, underlying), ltp=snap.spot, recv_ts=snap.ts, source="groww",
                       label=DataLabel.LIVE_UNVERIFIED) if snap.spot else None
        return snap, spot_q

    async def fetch_quotes(self, instruments: list) -> list[Quote]:
        out, now = [], self.clock.ts()
        for i in instruments:
            ref = i.broker_refs.get("groww")
            if not ref:
                continue
            q = await self.call("get_quote", trading_symbol=ref["trading_symbol"], exchange=ref["exchange"], segment="COMMODITY" if ref["exchange"] == "MCX" else "FNO")
            ltp = num(pick(q or {}, "last_price", "ltp"))
            if ltp:
                out.append(Quote(instrument_key=i.key, ltp=ltp, oi=int(num(pick(q, "open_interest"), 0) or 0), volume=int(num(pick(q, "volume"), 0) or 0),
                                 recv_ts=now, source="groww", label=DataLabel.LIVE_UNVERIFIED))
        return out

    async def fetch_order_book(self) -> list[OrderStatusReport]:
        return parse_order_book(await self.call("get_order_list", segment="FNO", page_size=100))

    async def fetch_positions(self) -> list[PositionReport]:
        return parse_positions(await self.call("get_positions_for_user", segment="FNO"))

    async def fetch_funds(self) -> FundsReport:
        return parse_funds(await self.call("get_available_margin_details"), self.clock.ts())

    async def _place(self, req: BrokerOrderRequest) -> BrokerAck:
        try:
            resp = await self.call("place_order", **order_kwargs(req), timeout=self.settings.order_ack_timeout_seconds)
        except BrokerRejected:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_place_error(exc) from exc
        oid = pick(resp or {}, "groww_order_id", "order_id")
        if not oid:
            return BrokerAck(accepted=False, status="UNKNOWN", message=str(resp)[:200])
        st = normalise_status(str(pick(resp, "order_status", default="open")), 0, req.quantity, STATUS)
        if st == "REJECTED":
            raise BrokerRejected(str(pick(resp, "remark", default="rejected")))
        return BrokerAck(accepted=True, broker_order_id=str(oid), status="OPEN", message="accepted")

    async def _cancel(self, broker_order_id: str) -> BrokerAck:
        resp = await self.call("cancel_order", groww_order_id=broker_order_id, segment="FNO")
        st = normalise_status(str(pick(resp or {}, "order_status", default="")), 0, 0, STATUS)
        return BrokerAck(accepted=st == "CANCELLED", broker_order_id=broker_order_id, status="CANCELLED" if st == "CANCELLED" else "UNKNOWN")
