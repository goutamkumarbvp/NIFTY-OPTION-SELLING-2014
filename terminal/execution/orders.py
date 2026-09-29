"""Order manager: risk gate -> mode gate -> broker -> position update, all audited."""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from terminal.core.models import Exchange, Order, OrderSource, OrderStatus, OrderType, OptionType, Side, TerminalMode


class OrderManager:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.orders: Dict[str, Order] = {}
        self._lock = asyncio.Lock()
        self.trades_today = 0
        self._day = time.strftime("%Y-%m-%d")

    def _roll(self) -> None:
        d = time.strftime("%Y-%m-%d")
        if d != self._day:
            self._day, self.trades_today = d, 0

    def build(self, symbol: str, side: Side, lots: int, source: OrderSource, order_type: OrderType = OrderType.MARKET, limit_price: Optional[float] = None,
              run_id: Optional[str] = None, tag: str = "", reason: str = "") -> Order:
        q = self.t.quote(symbol)
        if q is None:
            raise ValueError(f"UNKNOWN_SYMBOL:{symbol}")
        u = self.t.universe.get(q.underlying)
        return Order(symbol=symbol, underlying=q.underlying, exchange=u.exchange, expiry=q.expiry, strike=q.strike, option_type=q.option_type, side=side, lots=lots,
                     lot_size=u.lot_size, order_type=order_type, limit_price=limit_price, source=source, strategy_run_id=run_id, tag=tag, reason=reason)

    def is_reducing(self, order: Order) -> bool:
        pos = self.t.positions.positions.get(order.symbol)
        if not pos or pos.net_qty == 0:
            return False
        return (pos.net_qty > 0 and order.side == Side.SELL) or (pos.net_qty < 0 and order.side == Side.BUY)

    async def submit(self, order: Order, actor: str = "system", protective: bool = False) -> Order:
        """Submit an order through every gate. Returns the (possibly rejected) order."""
        self._roll()
        async with self._lock:
            self.orders[order.id] = order
            reducing = self.is_reducing(order)
            decision = self.t.risk.pre_trade(order, reducing=reducing or protective)
            if not decision["allowed"]:
                order.status = OrderStatus.RISK_REJECTED
                order.message = "; ".join(decision["reasons"])
                order.updated_at = time.time()
                self._persist(order)
                self.t.audit.record("ORDER_RISK_REJECTED", order.model_dump(mode="json"), actor)
                await self.t.alerts.emit("WARNING", "risk", f"Order blocked: {order.symbol} {order.side.value} {order.lots}L", order.message, market=order.exchange.value)
                await self.t.bus.publish("order.rejected", order)
                return order
            # Mode gate: agent originated *entries* need human approval in MANUAL
            if (self.t.mode == TerminalMode.MANUAL and order.source in (OrderSource.AUTO, OrderSource.STRATEGY) and not reducing and not protective
                    and self.t.settings.manual_confirmation_for_agent_orders):
                order.status = OrderStatus.PENDING_APPROVAL
                order.message = "AWAITING_HUMAN_CONFIRMATION"
                self._persist(order)
                self.t.audit.record("ORDER_PENDING_APPROVAL", order.model_dump(mode="json"), actor)
                await self.t.bus.publish("order.pending_approval", order)
                return order
            return await self._execute(order, actor)

    async def approve(self, order_id: str, actor: str) -> Order:
        order = self.orders[order_id]
        if order.status != OrderStatus.PENDING_APPROVAL:
            raise ValueError("ORDER_NOT_PENDING_APPROVAL")
        self.t.audit.record("ORDER_APPROVED", {"id": order_id}, actor)
        return await self._execute(order, actor)

    async def reject(self, order_id: str, actor: str, reason: str = "") -> Order:
        order = self.orders[order_id]
        if order.status != OrderStatus.PENDING_APPROVAL:
            raise ValueError("ORDER_NOT_PENDING_APPROVAL")
        order.status = OrderStatus.REJECTED
        order.message = f"REJECTED_BY_{actor}:{reason}"
        order.updated_at = time.time()
        self._persist(order)
        self.t.audit.record("ORDER_REJECTED_BY_USER", {"id": order_id, "reason": reason}, actor)
        await self.t.bus.publish("order.rejected", order)
        return order

    async def _execute(self, order: Order, actor: str) -> Order:
        try:
            order = await self.t.broker.place(order, self.t.quote)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.message = f"BROKER_ERROR:{exc}"
        order.updated_at = time.time()
        self._persist(order)
        if order.status == OrderStatus.FILLED:
            self.trades_today += 1
            pos = self.t.positions.apply_fill(order)
            self.t.db.add_fill(order.id, order.symbol, order.side.value, order.filled_qty, order.filled_price or 0.0, order.charges, order.strategy_run_id, order.source.value)
            self.t.audit.record("ORDER_FILLED", order.model_dump(mode="json"), actor)
            self.t.log("INFO", "orders", f"FILLED {order.side.value} {order.lots}L {order.symbol} @ {order.filled_price} ({order.source.value}) {order.reason}")
            await self.t.bus.publish("order.filled", order)
            await self.t.bus.publish("position.updated", pos)
        else:
            self.t.audit.record("ORDER_" + order.status.value, order.model_dump(mode="json"), actor)
            self.t.log("WARNING", "orders", f"{order.status.value} {order.side.value} {order.lots}L {order.symbol}: {order.message}")
            await self.t.bus.publish("order.updated", order)
        return order

    async def cancel(self, order_id: str, actor: str) -> Order:
        order = self.orders[order_id]
        if order.status == OrderStatus.PENDING_APPROVAL:
            return await self.reject(order_id, actor, "cancelled")
        order = await self.t.broker.cancel(order)
        self._persist(order)
        self.t.audit.record("ORDER_CANCELLED", {"id": order_id}, actor)
        await self.t.bus.publish("order.updated", order)
        return order

    async def flatten_all(self, source: OrderSource, actor: str, reason: str) -> List[Order]:
        """Close every open position with market orders (protective path)."""
        out: List[Order] = []
        # buy back shorts first (margin release), then sell longs
        positions = sorted(self.t.positions.open_positions(), key=lambda p: p.net_qty)
        for p in positions:
            lots = p.lots or 1
            side = Side.BUY if p.net_qty < 0 else Side.SELL
            try:
                o = self.build(p.symbol, side, lots, source, run_id=p.strategy_run_id, tag="flatten", reason=reason)
            except ValueError:
                continue
            out.append(await self.submit(o, actor, protective=True))
        for run in list(self.t.strategies.runs.values()):
            if run.status == "ACTIVE":
                self.t.strategies.mark_closed(run, reason)
        return out

    def pending_approval(self) -> List[Order]:
        return [o for o in self.orders.values() if o.status == OrderStatus.PENDING_APPROVAL]

    def recent(self, limit: int = 100) -> List[dict]:
        rows = sorted(self.orders.values(), key=lambda o: o.created_at, reverse=True)[:limit]
        return [o.model_dump(mode="json") | {"quantity": o.quantity} for o in rows]

    def _persist(self, order: Order) -> None:
        self.t.db.save_order(order.model_dump(mode="json"))
