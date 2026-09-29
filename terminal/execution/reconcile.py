"""Live-broker reconciliation.

* OrderReconciler  – polls the broker for every working order and applies
  fills (full or partial), cancellations and rejections to the terminal book.
  Without it a live fill would never reach positions and the exit guard could
  re-send a square-off that had already executed.
* PositionReconciler – compares the broker's position book and margins with
  the terminal's every ``RECONCILE_SECONDS``; mismatches raise alerts, and the
  broker's margin figure replaces the SPAN estimate when it is fresh.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from terminal.core.models import OptionType, OrderStatus

log = logging.getLogger("terminal.reconcile")


class OrderReconciler:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.polls = 0
        self.updates = 0
        self.errors = 0
        self.last_error = ""
        self.last_ts = 0.0

    def working_orders(self) -> List[Any]:
        return [o for o in self.t.orders.orders.values() if o.status in (OrderStatus.OPEN, OrderStatus.PENDING) and o.broker_order_id]

    async def tick(self) -> int:
        if not getattr(self.t.broker, "live", False):
            return 0
        applied = 0
        for order in self.working_orders():
            self.polls += 1
            try:
                upd = await self.t.broker.fetch_order(order)
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                continue
            if not upd:
                continue
            changed = await self.t.orders.apply_broker_update(order, upd)
            if changed:
                applied += 1
                self.updates += 1
            if order.status == OrderStatus.OPEN and time.time() - order.created_at > 45 and order.order_type.value == "MARKET":
                self.t.log("WARNING", "reconcile", f"market order {order.id} still OPEN after 45s at the broker")
        self.last_ts = time.time()
        return applied

    def describe(self) -> dict:
        return {"working": len(self.working_orders()), "polls": self.polls, "updates": self.updates, "errors": self.errors, "last_error": self.last_error, "last_ts": self.last_ts}


class PositionReconciler:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.last: Dict[str, Any] = {"ts": 0.0, "ok": None, "diffs": [], "broker_only": [], "terminal_only": []}
        self.margin: Dict[str, Any] = {"ts": 0.0, "available": None, "used": None}
        self.runs = 0
        self.errors = 0

    def _our_symbol(self, row: Dict[str, Any]) -> str | None:
        """Map a broker position row to the terminal's option symbol via the live session's cache."""
        live = self.t.live
        ts = str(row.get("trading_symbol") or "")
        if live is not None:
            hit = live.lookup_trading_symbol(ts) if hasattr(live, "lookup_trading_symbol") else None
            if hit:
                underlying, expiry, strike, ot = hit
                return self.t.chain_builder.option_symbol(underlying, expiry, strike, ot)
        return ts or None

    async def tick(self, adopt: bool = False) -> Dict[str, Any]:
        if not getattr(self.t.broker, "live", False):
            return self.last
        self.runs += 1
        try:
            rows = await self.t.broker.broker_positions()
        except Exception as exc:
            self.errors += 1
            self.last = {"ts": time.time(), "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "diffs": [], "broker_only": [], "terminal_only": []}
            return self.last
        broker: Dict[str, Dict[str, Any]] = {}
        for row in rows or []:
            sym = self._our_symbol(row)
            if not sym:
                continue
            broker[sym] = {"net_qty": int(row.get("net_qty") or 0), "avg_price": float(row.get("avg_price") or 0.0), "raw": row}
        ours = {p.symbol: p for p in self.t.positions.open_positions()}
        diffs, broker_only, terminal_only = [], [], []
        for sym, b in broker.items():
            if b["net_qty"] == 0:
                continue
            if sym not in ours:
                broker_only.append({"symbol": sym, "broker_qty": b["net_qty"]})
            elif ours[sym].net_qty != b["net_qty"]:
                diffs.append({"symbol": sym, "terminal_qty": ours[sym].net_qty, "broker_qty": b["net_qty"]})
        for sym, p in ours.items():
            if sym not in broker or broker[sym]["net_qty"] == 0:
                terminal_only.append({"symbol": sym, "terminal_qty": p.net_qty})
        ok = not (diffs or broker_only or terminal_only)
        self.last = {"ts": time.time(), "ok": ok, "diffs": diffs, "broker_only": broker_only, "terminal_only": terminal_only}
        if not ok:
            self.t.log("WARNING", "reconcile", f"position mismatch: diffs={diffs} broker_only={broker_only} terminal_only={terminal_only}")
            await self.t.alerts.emit("WARNING", "reconcile", "Position book differs from broker", f"{len(diffs)} qty diffs, {len(broker_only)} only at broker, {len(terminal_only)} only in terminal", dedupe_seconds=300)
            if adopt:
                self._adopt(broker, diffs, broker_only, terminal_only)
        try:
            m = await self.t.broker.margins()
            if m and m.get("available") is not None:
                self.margin = {"ts": time.time(), "available": float(m.get("available") or 0), "used": float(m["used"]) if m.get("used") is not None else None}
        except Exception as exc:
            self.errors += 1
            log.warning("margin fetch failed: %s", exc)
        return self.last

    def _adopt(self, broker: Dict[str, Dict[str, Any]], diffs, broker_only, terminal_only) -> None:
        live = self.t.live
        for d in diffs + broker_only:
            sym = d["symbol"]
            b = broker[sym]
            hit = live.lookup_trading_symbol(str(b["raw"].get("trading_symbol") or "")) if live is not None and hasattr(live, "lookup_trading_symbol") else None
            if not hit:
                continue
            underlying, expiry, strike, ot = hit
            u = self.t.universe.get(underlying)
            self.t.positions.adopt(sym, underlying, u.exchange, expiry, float(strike), OptionType(ot), u.lot_size, b["net_qty"], b["avg_price"])
        for d in terminal_only:
            p = self.t.positions.positions.get(d["symbol"])
            if p:
                self.t.positions.adopt(p.symbol, p.underlying, p.exchange, p.expiry, p.strike, p.option_type, p.lot_size, 0, p.avg_price)
        self.t.audit.record("POSITIONS_ADOPTED_FROM_BROKER", {"diffs": diffs, "broker_only": broker_only, "terminal_only": terminal_only}, "reconciler")

    def describe(self) -> dict:
        return {**self.last, "margin": self.margin, "runs": self.runs, "errors": self.errors}
