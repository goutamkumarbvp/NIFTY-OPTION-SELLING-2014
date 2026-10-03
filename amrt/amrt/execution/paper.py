"""Paper execution venue — technically isolated from live trading.

* Imports nothing from `amrt.brokers`; it has no broker credentials and no network client.
* Refuses any ticket whose mode is not PAPER (TicketChecker), and the Gateway
  refuses to route PAPER intents to anything but this venue.
* Fills only against fresh quotes (live-verified or labelled historical replay):
  MARKET → crosses the spread plus configured slippage; LIMIT → fills if
  marketable, otherwise rests and is re-checked on every quote.
* Every fill, position and P&L it produces is SIMULATED.
"""
from __future__ import annotations

import itertools

from amrt.core.enums import DataLabel, Exchange, OrderType, Side
from amrt.core.errors import DataUnavailable
from amrt.execution.intents import BrokerOrderRequest, SubmissionTicket
from amrt.execution.venue import BrokerAck, FundsReport, OrderStatusReport, PositionReport, TicketChecker, Venue
from amrt.marketdata.instruments import InstrumentMaster
from amrt.marketdata.models import Instrument
from amrt.quant.costs import option_charges
from amrt.risk.margin import estimate_option_margin

PAPER_LABEL = DataLabel.SIMULATED


class PaperVenue(Venue):
    kind = "PAPER"

    def __init__(self, account_id: str, hub, gateway_verifier, clock, capital: float, slippage_bps: float = 5.0, max_quote_age_ms: float = 5000,
                 allow_simulated: bool = False) -> None:
        self.name = "paper"
        self.account_id = account_id
        self.hub = hub
        self.clock = clock
        self.capital = capital
        self.slippage_bps = slippage_bps
        self.max_quote_age_ms = max_quote_age_ms
        self.allow_simulated = allow_simulated
        self.checker = TicketChecker(gateway_verifier, "PAPER", clock.ts)
        self._ids = itertools.count(1)
        self.book: dict[str, dict] = {}
        self.net: dict[str, dict] = {}
        self.margin_used = 0.0

    def _quote(self, key: str):
        q = self.hub.quote(key, self.max_quote_age_ms)
        ok = {DataLabel.LIVE_VERIFIED, DataLabel.LIVE_UNVERIFIED, DataLabel.HISTORICAL_REPLAY} | ({DataLabel.SIMULATED} if self.allow_simulated else set())
        if q.label not in ok:
            raise DataUnavailable("DATA UNAVAILABLE", reason=f"label {q.label}")
        return q

    def _fill_price(self, req: BrokerOrderRequest, q) -> float | None:
        bid, ask = q.bid or q.ltp, q.ask or q.ltp
        slip = q.ltp * self.slippage_bps / 10_000
        if req.order_type == OrderType.MARKET:
            return round(ask + slip, 2) if req.side == Side.BUY else round(max(0.05, bid - slip), 2)
        if req.side == Side.BUY and req.limit_price >= ask:
            return min(req.limit_price, round(ask + slip, 2))
        if req.side == Side.SELL and req.limit_price <= bid:
            return max(req.limit_price, round(bid - slip, 2))
        return None

    def _apply(self, o: dict, price: float) -> None:
        o.update(status="FILLED", filled_qty=o["quantity"], avg_price=price,
                 charges=option_charges(Exchange(o["exchange"]), o["side"], price * o["quantity"], self.clock.ist().date())["total"])
        n = self.net.setdefault(o["trading_symbol"], {"instrument_key": o["instrument_key"], "net_qty": 0, "avg_price": 0.0})
        signed = o["quantity"] if o["side"] == Side.BUY else -o["quantity"]
        if n["net_qty"] == 0 or (n["net_qty"] > 0) == (signed > 0):
            tot = abs(n["net_qty"]) + o["quantity"]
            n["avg_price"] = (abs(n["net_qty"]) * n["avg_price"] + o["quantity"] * price) / tot
        n["net_qty"] += signed
        if n["net_qty"] == 0:
            n["avg_price"] = 0.0

    async def submit(self, req: BrokerOrderRequest, ticket: SubmissionTicket) -> BrokerAck:
        self.checker.check(ticket, req)
        oid = f"PAPER-{next(self._ids):06d}"
        o = {"broker_order_id": oid, "client_tag": req.client_tag, "trading_symbol": req.trading_symbol, "instrument_key": req.instrument_key,
             "exchange": req.exchange, "side": req.side, "quantity": req.quantity, "order_type": req.order_type, "limit_price": req.limit_price,
             "status": "OPEN", "filled_qty": 0, "avg_price": 0.0, "ts": self.clock.ts()}
        try:
            q = self._quote(req.instrument_key)
        except DataUnavailable as exc:
            o["status"], o["message"] = "REJECTED", f"DATA UNAVAILABLE: {exc.detail.get('reason', '')}"
            self.book[oid] = o
            return BrokerAck(accepted=False, broker_order_id=oid, status="REJECTED", message=o["message"])
        px = self._fill_price(req, q)
        self.book[oid] = o
        if px is not None:
            self._apply(o, px)
            return BrokerAck(accepted=True, broker_order_id=oid, status="FILLED", filled_qty=o["filled_qty"], avg_price=px, message="SIMULATED fill")
        return BrokerAck(accepted=True, broker_order_id=oid, status="OPEN", message="SIMULATED resting limit order")

    def on_quote(self, q) -> list[dict]:
        """Re-check resting paper limit orders against a new quote; returns orders that filled."""
        filled = []
        for o in self.book.values():
            if o["status"] == "OPEN" and o["instrument_key"] == q.instrument_key and o["order_type"] == OrderType.LIMIT:
                req = BrokerOrderRequest(account_id=self.account_id, instrument_key=o["instrument_key"], trading_symbol=o["trading_symbol"], exchange=o["exchange"],
                                         segment="", side=o["side"], quantity=o["quantity"], order_type=OrderType.LIMIT, limit_price=o["limit_price"],
                                         product="NRML", validity="DAY", client_tag=o["client_tag"])
                px = self._fill_price(req, q)
                if px is not None:
                    self._apply(o, px)
                    filled.append(o)
        return filled

    async def cancel(self, broker_order_id: str, ticket: SubmissionTicket) -> BrokerAck:
        self.checker.check(ticket)
        o = self.book.get(broker_order_id)
        if o is None:
            return BrokerAck(accepted=False, broker_order_id=broker_order_id, status="REJECTED", message="unknown paper order")
        if o["status"] == "OPEN":
            o["status"] = "CANCELLED"
        return BrokerAck(accepted=True, broker_order_id=broker_order_id, status=o["status"], filled_qty=o["filled_qty"], avg_price=o["avg_price"])

    async def order_book(self) -> list[OrderStatusReport]:
        return [OrderStatusReport(broker_order_id=o["broker_order_id"], client_tag=o["client_tag"], trading_symbol=o["trading_symbol"], side=o["side"],
                                  quantity=o["quantity"], filled_qty=o["filled_qty"], avg_price=o["avg_price"], status=o["status"], message=o.get("message", ""))
                for o in self.book.values()]

    async def positions(self) -> list[PositionReport]:
        return [PositionReport(trading_symbol=s, instrument_key=n["instrument_key"], net_qty=n["net_qty"], avg_price=n["avg_price"]) for s, n in self.net.items() if n["net_qty"]]

    def _estimate_margin(self) -> float:
        total = 0.0
        for n in self.net.values():
            if not n["net_qty"]:
                continue
            k = InstrumentMaster.parse_key(n["instrument_key"])
            try:
                spot = self.hub.quote(Instrument.underlying_key(k["exchange"], k["underlying"]), 60_000).ltp
            except DataUnavailable:
                spot = None
            side = Side.SELL if n["net_qty"] < 0 else Side.BUY
            m = estimate_option_margin(side, abs(n["net_qty"]), spot, n["avg_price"])
            total += m or 0.0
        return round(total, 2)

    async def funds(self) -> FundsReport:
        self.margin_used = self._estimate_margin()
        return FundsReport(available=self.capital - self.margin_used, margin_used=self.margin_used, total=self.capital, ts=self.clock.ts())

    def charges_for(self, broker_order_id: str) -> float:
        return float(self.book.get(broker_order_id, {}).get("charges", 0.0))

