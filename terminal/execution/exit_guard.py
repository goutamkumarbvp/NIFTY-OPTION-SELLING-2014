"""Exit guard: the single authority for squaring off positions.

Every stop-loss / target / square-off / halt / kill-switch / manual close goes
through ``ExitGuard.request``. It guarantees two things:

1. **No duplicate exits.** A symbol has at most one exit *intent*; while an
   exit order is in flight the same symbol cannot receive a second buy-back
   (whichever layer asks). Exit quantity is always capped at the open
   quantity minus what is already in flight, so a position can never be
   over-bought or flipped by a protective order.
2. **Retry until flat.** If the exit order is rejected, stays unfilled or
   fills only partially, the guard cancels the stale order and re-sends a
   market order for the remaining quantity every ``EXIT_RETRY_SECONDS``
   (default 10 s) until the position is flat, escalating alerts as attempts
   grow.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from terminal.core.models import Order, OrderSource, OrderStatus, Side

log = logging.getLogger("terminal.exit_guard")


@dataclass
class ExitIntent:
    symbol: str
    reason: str
    source: OrderSource
    run_id: Optional[str]
    created: float = field(default_factory=time.time)
    attempts: int = 0
    last_attempt: float = 0.0
    last_order_id: Optional[str] = None
    suppressed: int = 0
    resolved_at: Optional[float] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ExitGuard:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.intents: Dict[str, ExitIntent] = {}
        self.history: List[dict] = []
        self.retry_seconds = float(getattr(terminal.settings, "exit_retry_seconds", 10.0))
        self.max_attempts = int(getattr(terminal.settings, "exit_max_attempts", 60))

    # ---------------------------------------------------------------- requests
    async def request(self, symbol: str, reason: str, source: OrderSource, run_id: Optional[str] = None, actor: str = "exit-guard") -> Optional[Order]:
        """Ask for `symbol` to be squared off. Returns the order sent, or None when
        nothing was sent (already flat, or an exit is already in flight)."""
        pos = self.t.positions.positions.get(symbol)
        if pos is None or pos.net_qty == 0:
            # another layer already squared this off: record it, send nothing
            self.t.audit.record("EXIT_SUPPRESSED_ALREADY_FLAT", {"symbol": symbol, "reason": reason, "source": source.value}, actor)
            self._resolve(symbol, "already flat")
            return None
        intent = self.intents.get(symbol)
        if intent is None:
            intent = ExitIntent(symbol=symbol, reason=reason, source=source, run_id=run_id or pos.strategy_run_id)
            self.intents[symbol] = intent
            self.t.audit.record("EXIT_REQUESTED", {"symbol": symbol, "reason": reason, "source": source.value, "run": intent.run_id}, actor)
        elif self.t.orders.in_flight_reducing_qty(symbol) > 0 or (time.time() - intent.last_attempt) < self.retry_seconds:
            intent.suppressed += 1
            self.t.audit.record("EXIT_DUPLICATE_SUPPRESSED", {"symbol": symbol, "reason": reason, "source": source.value, "first_reason": intent.reason}, actor)
            self.t.log("INFO", "exit-guard", f"duplicate exit for {symbol} ({reason}) suppressed; {intent.reason} already in flight (attempt {intent.attempts})")
            return None
        return await self._place(intent, actor)

    async def _place(self, intent: ExitIntent, actor: str) -> Optional[Order]:
        async with intent.lock:  # one exit order per symbol at a time, whichever layer asks
            return await self._place_locked(intent, actor)

    async def _place_locked(self, intent: ExitIntent, actor: str) -> Optional[Order]:
        pos = self.t.positions.positions.get(intent.symbol)
        if pos is None or pos.net_qty == 0:
            self._resolve(intent.symbol, "flat")
            return None
        remaining = abs(pos.net_qty) - self.t.orders.in_flight_reducing_qty(intent.symbol)
        if remaining <= 0:
            intent.suppressed += 1
            return None
        lots = max(1, remaining // pos.lot_size)
        side = Side.BUY if pos.net_qty < 0 else Side.SELL
        intent.attempts += 1
        intent.last_attempt = time.time()
        tag = "exit"
        run = self.t.strategies.runs.get(intent.run_id) if intent.run_id else None
        if run:
            tag = run.strategy
        order = self.t.orders.build(intent.symbol, side, lots, intent.source, run_id=intent.run_id, tag=tag, reason=f"{intent.reason} (attempt {intent.attempts})")
        order = await self.t.orders.submit(order, actor, protective=True)
        intent.last_order_id = order.id
        if order.status == OrderStatus.FILLED:
            pos2 = self.t.positions.positions.get(intent.symbol)
            if pos2 is None or pos2.net_qty == 0:
                self._resolve(intent.symbol, f"filled on attempt {intent.attempts}")
        elif order.status in (OrderStatus.REJECTED, OrderStatus.RISK_REJECTED):
            self.t.log("WARNING", "exit-guard", f"exit attempt {intent.attempts} for {intent.symbol} rejected: {order.message}; retry in {self.retry_seconds:.0f}s")
        return order

    # ---------------------------------------------------------------- supervisor
    async def tick(self) -> None:
        """Called every second: retry unfilled exits, resolve flat ones."""
        now = time.time()
        for symbol, intent in list(self.intents.items()):
            pos = self.t.positions.positions.get(symbol)
            if pos is None or pos.net_qty == 0:
                self._resolve(symbol, f"flat after {intent.attempts} attempt(s)")
                continue
            if now - intent.last_attempt < self.retry_seconds:
                continue
            # stale working order? cancel it before re-sending
            for o in self.t.orders.reducing_orders(symbol):
                if o.status in (OrderStatus.OPEN, OrderStatus.PENDING):
                    try:
                        await self.t.orders.cancel(o.id, "exit-guard")
                    except Exception as exc:
                        log.warning("cancel of stale exit order %s failed: %s", o.id, exc)
            if intent.attempts >= self.max_attempts:
                await self.t.alerts.emit("CRITICAL", "exit", f"Cannot square off {symbol}", f"{intent.attempts} attempts failed; intervene manually with the broker.", dedupe_seconds=300)
            elif intent.attempts in (3, 6, 12):
                await self.t.alerts.emit("WARNING" if intent.attempts < 12 else "CRITICAL", "exit", f"Square-off retry {intent.attempts} for {symbol}", intent.reason, dedupe_seconds=0)
            self.t.log("WARNING", "exit-guard", f"{symbol} still open ({pos.net_qty}); retrying square-off (attempt {intent.attempts + 1})")
            await self._place(intent, "exit-guard")
        await self.t.strategies.check_exiting()

    def _resolve(self, symbol: str, note: str) -> None:
        intent = self.intents.pop(symbol, None)
        if intent:
            intent.resolved_at = time.time()
            self.history.append({"symbol": symbol, "reason": intent.reason, "attempts": intent.attempts, "suppressed": intent.suppressed, "note": note, "seconds": round(intent.resolved_at - intent.created, 2)})
            self.history = self.history[-200:]
            self.t.log("INFO", "exit-guard", f"{symbol} exit resolved: {note}")

    def pending(self) -> List[dict]:
        return [{"symbol": i.symbol, "reason": i.reason, "source": i.source.value, "run_id": i.run_id, "attempts": i.attempts, "suppressed": i.suppressed, "age": round(time.time() - i.created, 1),
                 "next_retry_in": max(0.0, round(self.retry_seconds - (time.time() - i.last_attempt), 1))} for i in self.intents.values()]

    def describe(self) -> dict:
        return {"retry_seconds": self.retry_seconds, "max_attempts": self.max_attempts, "pending": self.pending(), "history": self.history[-30:][::-1]}
