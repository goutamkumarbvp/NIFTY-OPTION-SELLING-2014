"""Order manager: risk gate -> mode gate -> broker -> position update, all audited."""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List

from terminal.core.models import Order, OrderSource, OrderStatus, OrderType, Side, TerminalMode


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

    def build(self, symbol: str, side: Side, lots: int, source: OrderSource, order_type: OrderType = OrderType.MARKET, limit_price: float | None = None,
              run_id: str | None = None, tag: str = "", reason: str = "") -> Order:
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

    def reducing_orders(self, symbol: str) -> List[Order]:
        pos = self.t.positions.positions.get(symbol)
        if not pos or pos.net_qty == 0:
            return []
        side = Side.BUY if pos.net_qty < 0 else Side.SELL
        return [o for o in self.orders.values() if o.symbol == symbol and o.side == side]

    def in_flight_reducing_qty(self, symbol: str) -> int:
        """Units of `symbol` already covered by working (unfilled) exit orders."""
        return sum(o.quantity - o.filled_qty for o in self.reducing_orders(symbol) if o.status in (OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PENDING_APPROVAL))

    async def submit(self, order: Order, actor: str = "system", protective: bool = False) -> Order:
        """Submit an order through every gate. Returns the (possibly rejected) order."""
        self._roll()
        async with self._lock:
            reducing = self.is_reducing(order)
            if protective and not reducing:
                # an exit for a position that is already flat must never become a fresh entry
                order.status = OrderStatus.REJECTED
                order.message = "NO_POSITION_TO_EXIT"
                order.updated_at = time.time()
                self.orders[order.id] = order
                self._persist(order)
                self.t.audit.record("EXIT_WITHOUT_POSITION_SUPPRESSED", {"symbol": order.symbol, "side": order.side.value, "reason": order.reason}, actor)
                return order
            if reducing:
                pos = self.t.positions.positions[order.symbol]
                remaining = abs(pos.net_qty) - self.in_flight_reducing_qty(order.symbol)
                if remaining <= 0:
                    order.status = OrderStatus.REJECTED
                    order.message = "DUPLICATE_EXIT_SUPPRESSED"
                    order.updated_at = time.time()
                    self.orders[order.id] = order
                    self._persist(order)
                    self.t.audit.record("ORDER_DUPLICATE_EXIT_SUPPRESSED", {"symbol": order.symbol, "side": order.side.value, "lots": order.lots, "reason": order.reason}, actor)
                    self.t.log("WARNING", "orders", f"duplicate exit suppressed: {order.side.value} {order.lots}L {order.symbol} ({order.reason})")
                    return order
                if order.quantity > remaining:
                    # never flip a position through zero with an exit order
                    order.lots = max(1, remaining // order.lot_size)
                    order.message = f"EXIT_QTY_CAPPED_TO_{order.quantity}"
            self.orders[order.id] = order
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
        self.t.tca.on_submit(order)
        self.t.pretrade.record_sent(order)
        t0 = time.perf_counter()
        try:
            order = await self.t.broker.place(order, self.t.quote)
            self.t.latency.observe("order_submit_to_ack", (time.perf_counter() - t0) * 1000)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.message = f"BROKER_ERROR:{exc}"
        order.updated_at = time.time()
        self._persist(order)
        if order.status == OrderStatus.FILLED:
            self.trades_today += 1
            self.t.tca.on_fill(order)
            self.t.latency.observe("order_submit_to_fill", (time.perf_counter() - t0) * 1000)
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

    async def apply_broker_update(self, order: Order, upd: Dict[str, object]) -> bool:
        """Apply a broker-reported state to a working order. Returns True when something changed."""
        status = str(upd.get("status") or "").upper()
        filled_qty = int(upd.get("filled_qty") or 0)
        avg_price = float(upd.get("avg_price") or 0.0)
        message = str(upd.get("message") or "")
        changed = False
        new_units = filled_qty - order.filled_qty
        if new_units > 0 and avg_price > 0:
            # incremental fill: book only the new units at the broker's average price
            from terminal.execution.brokers.paper import option_charges
            charges = option_charges(order.exchange, order.side, avg_price * new_units)
            order.filled_price = avg_price
            order.charges = round(order.charges + charges, 2)
            prev_units = order.filled_qty
            order.filled_qty = filled_qty
            if prev_units == 0:
                self.trades_today += 1
            self.t.tca.on_fill(order, price=avg_price, qty=new_units)
            self.t.latency.observe("order_submit_to_fill", (time.time() - order.created_at) * 1000)
            pos = self.t.positions.apply_fill(order, qty_units=new_units, price=avg_price, charges=charges)
            self.t.db.add_fill(order.id, order.symbol, order.side.value, new_units, avg_price, charges, order.strategy_run_id, order.source.value)
            self.t.audit.record("ORDER_PARTIAL_FILL" if filled_qty < order.quantity else "ORDER_FILLED", order.model_dump(mode="json"), "reconciler")
            self.t.log("INFO", "orders", f"BROKER FILL {order.side.value} {new_units}u {order.symbol} @ {avg_price} ({order.filled_qty}/{order.quantity})")
            await self.t.bus.publish("order.filled", order)
            await self.t.bus.publish("position.updated", pos)
            changed = True
        if filled_qty >= order.quantity and order.quantity > 0:
            if order.status != OrderStatus.FILLED:
                order.status = OrderStatus.FILLED
                order.message = message or "FILLED_AT_BROKER"
                changed = True
        elif status in ("CANCELLED", "CANCELED"):
            if order.status != OrderStatus.CANCELLED:
                order.status = OrderStatus.CANCELLED
                order.message = message or "CANCELLED_AT_BROKER"
                changed = True
        elif status == "REJECTED":
            if order.status != OrderStatus.REJECTED:
                order.status = OrderStatus.REJECTED
                order.message = message or "REJECTED_AT_BROKER"
                self.t.log("WARNING", "orders", f"{order.id} rejected at broker: {order.message}")
                changed = True
        if changed:
            order.updated_at = time.time()
            self._persist(order)
            await self.t.bus.publish("order.updated", order)
        return changed

    def restore(self) -> int:
        """Reload today's working orders after a restart so the reconciler can finish them."""
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        n = 0
        for row in self.t.db.open_orders(day_start):
            try:
                o = Order(**row)
            except Exception:
                continue
            self.orders[o.id] = o
            n += 1
        return n

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
        """Close every open position through the exit guard (deduplicated, retried until flat)."""
        out: List[Order] = []
        for run in list(self.t.strategies.runs.values()):
            if run.status == "ACTIVE":
                run.status = "EXITING"
                run.exit_reason = reason
        # buy back shorts first (margin release), then sell longs
        positions = sorted(self.t.positions.open_positions(), key=lambda p: p.net_qty)
        for p in positions:
            o = await self.t.exit_guard.request(p.symbol, reason, source, run_id=p.strategy_run_id, actor=actor)
            if o is not None:
                out.append(o)
        await self.t.strategies.check_exiting()
        return out

    def pending_approval(self) -> List[Order]:
        return [o for o in self.orders.values() if o.status == OrderStatus.PENDING_APPROVAL]

    def recent(self, limit: int = 100) -> List[dict]:
        rows = sorted(self.orders.values(), key=lambda o: o.created_at, reverse=True)[:limit]
        return [o.model_dump(mode="json") | {"quantity": o.quantity} for o in rows]

    def _persist(self, order: Order) -> None:
        self.t.db.save_order(order.model_dump(mode="json"))
