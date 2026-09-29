"""Paper broker: realistic fills with bid/ask crossing, slippage and Indian
option charges (brokerage, STT, exchange txn, SEBI, stamp duty, GST)."""
from __future__ import annotations

import random
import time
from typing import Dict, Optional

from terminal.core.models import Exchange, Order, OrderStatus, OrderType, Side
from terminal.execution.brokers.base import Broker, QuoteLookup
from terminal.market.pricing import round_to_tick


def option_charges(exchange: Exchange, side: Side, premium_value: float, brokerage_flat: float = 20.0) -> float:
    txn_rate = {Exchange.NSE: 0.0003503, Exchange.BSE: 0.000325, Exchange.MCX: 0.0005}[exchange]
    txn = premium_value * txn_rate
    stt = premium_value * (0.001 if side == Side.SELL and exchange != Exchange.MCX else 0.0)
    ctt = premium_value * (0.0005 if side == Side.SELL and exchange == Exchange.MCX else 0.0)
    sebi = premium_value * 10 / 1e7
    stamp = premium_value * (0.00003 if side == Side.BUY else 0.0)
    gst = (brokerage_flat + txn + sebi) * 0.18
    return round(brokerage_flat + txn + stt + ctt + sebi + stamp + gst, 2)


class PaperBroker(Broker):
    name = "paper"
    live = False

    def __init__(self, capital: float, seed: Optional[int] = None) -> None:
        super().__init__()
        self.capital = capital
        self.rng = random.Random(seed)
        self.cash_ledger = 0.0

    async def connect(self) -> None:
        self.connected = True

    async def place(self, order: Order, quote_lookup: QuoteLookup) -> Order:
        q = quote_lookup(order.symbol)
        if q is None:
            order.status = OrderStatus.REJECTED
            order.message = "NO_QUOTE"
            return order
        if order.order_type == OrderType.LIMIT and order.limit_price:
            # fill only if marketable
            if order.side == Side.BUY and order.limit_price < q.ask:
                order.status = OrderStatus.OPEN
                order.message = "LIMIT_NOT_MARKETABLE"
                return order
            if order.side == Side.SELL and order.limit_price > q.bid:
                order.status = OrderStatus.OPEN
                order.message = "LIMIT_NOT_MARKETABLE"
                return order
            px = order.limit_price
        else:
            px = q.ask if order.side == Side.BUY else q.bid
            # market impact / slippage scaled by lots
            slip = q.ltp * 0.0008 * (1 + order.lots / 10) * self.rng.uniform(0.2, 1.0)
            px = px + slip if order.side == Side.BUY else max(0.05, px - slip)
        px = round_to_tick(px)
        qty = order.quantity
        order.filled_price = px
        order.filled_qty = qty
        order.charges = option_charges(order.exchange, order.side, px * qty)
        order.status = OrderStatus.FILLED
        order.broker_order_id = f"PAPER-{int(time.time()*1000)}"
        order.message = "FILLED_PAPER"
        order.updated_at = time.time()
        return order

    async def cancel(self, order: Order) -> Order:
        if order.status in (OrderStatus.OPEN, OrderStatus.PENDING):
            order.status = OrderStatus.CANCELLED
            order.updated_at = time.time()
        return order

    async def margins(self) -> Dict[str, float]:
        return {"available": self.capital, "capital": self.capital}
