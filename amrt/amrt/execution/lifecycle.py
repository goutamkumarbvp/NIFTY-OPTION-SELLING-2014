"""Order lifecycle state machine and the persistent order book.

Intent → (Risk Kernel) → SUBMITTING → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED
                                     ↘ REJECTED / CANCELLED
                                     ↘ ORDER STATE UNKNOWN  (timeout, ambiguity)
UNKNOWN is resolved only by broker reconciliation; it is never assumed filled or
unfilled. NOT_FOUND_AT_BROKER is a final state reached only after repeated,
successful, complete order-book reads that do not contain the order.
The idempotency key is UNIQUE in the database, so the same logical order can
never be persisted — or submitted — twice.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from amrt.core.enums import FINAL_ORDER_STATES, WORKING_ORDER_STATES, OrderState
from amrt.core.errors import InvalidTransition
from amrt.core.ids import broker_tag
from amrt.storage.db import Database, orders

S = OrderState
TRANSITIONS: dict[OrderState, set[OrderState]] = {
    S.INTENT_PERSISTED: {S.REJECTED_PRE_TRADE, S.SUBMITTING},
    S.SUBMITTING: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.UNKNOWN, S.CANCELLED},
    S.ACKNOWLEDGED: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.UNKNOWN},
    S.UNKNOWN: {S.UNKNOWN, S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.CANCELLED, S.NOT_FOUND_AT_BROKER},
    S.REJECTED_PRE_TRADE: set(), S.FILLED: set(), S.REJECTED: set(), S.CANCELLED: set(), S.NOT_FOUND_AT_BROKER: set(),
}


class OrderBook:
    def __init__(self, db: Database, clock_ts=time.time) -> None:
        self.db = db
        self._ts = clock_ts
        self._lock = threading.RLock()

    # ------------------------------------------------------------ persistence
    def create(self, intent, risk_decision: dict | None, simulated: bool, tag_len: int = 20) -> tuple[dict, bool]:
        """Persist an intent. Returns (record, created). An existing idempotency key returns the original record."""
        now = self._ts()
        rec = {"intent_id": intent.intent_id, "account_id": intent.account_id, "broker": intent.broker, "mode": intent.mode.value,
               "instrument_key": intent.instrument_key, "side": intent.side.value, "quantity": intent.quantity, "state": S.INTENT_PERSISTED.value,
               "idempotency_key": intent.idempotency_key, "client_tag": broker_tag(intent.idempotency_key, tag_len), "broker_order_id": None,
               "filled_qty": 0, "avg_price": 0.0, "intent": intent.model_dump(mode="json"), "risk_decision": risk_decision,
               "history": [{"ts": now, "state": S.INTENT_PERSISTED.value, "note": "intent persisted before any submission"}],
               "created_at": now, "updated_at": now, "unknown_since": None, "reconciled_at": None, "last_error": None, "simulated": simulated}
        with self._lock:
            try:
                with self.db.tx() as conn:
                    conn.execute(orders.insert().values(**rec))
                return rec, True
            except IntegrityError:
                existing = self.by_idempotency_key(intent.idempotency_key)
                if existing is None:
                    raise
                return existing, False

    def get(self, intent_id: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(orders).where(orders.c.intent_id == intent_id)).first()
        return dict(row._mapping) if row else None

    def by_client_tag(self, tag: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(orders).where(orders.c.client_tag == tag)).first()
        return dict(row._mapping) if row else None

    def by_idempotency_key(self, key: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(orders).where(orders.c.idempotency_key == key)).first()
        return dict(row._mapping) if row else None

    def transition(self, intent_id: str, new_state: OrderState, note: str = "", **fields: Any) -> dict:
        with self._lock:
            rec = self.get(intent_id)
            if rec is None:
                raise KeyError(f"UNKNOWN_ORDER:{intent_id}")
            cur = OrderState(rec["state"])
            if new_state not in TRANSITIONS[cur]:
                raise InvalidTransition(f"{cur} → {new_state} not allowed", order=intent_id)
            now = self._ts()
            hist = list(rec["history"]) + [{"ts": now, "state": new_state.value, "note": note, **{k: v for k, v in fields.items() if k in ("broker_order_id", "filled_qty", "avg_price")}}]
            upd: dict[str, Any] = {"state": new_state.value, "history": hist, "updated_at": now}
            if new_state == S.UNKNOWN and cur != S.UNKNOWN:
                upd["unknown_since"] = now
            if cur == S.UNKNOWN and new_state != S.UNKNOWN:
                upd["reconciled_at"] = now
            for k in ("broker_order_id", "filled_qty", "avg_price", "last_error", "risk_decision"):
                if k in fields and fields[k] is not None:
                    upd[k] = fields[k]
            with self.db.tx() as conn:
                conn.execute(orders.update().where(orders.c.intent_id == intent_id).values(**upd))
            rec.update(upd)
            return rec

    def note(self, intent_id: str, note: str) -> None:
        rec = self.get(intent_id)
        if rec is None:
            return
        hist = list(rec["history"]) + [{"ts": self._ts(), "state": rec["state"], "note": note}]
        with self.db.tx() as conn:
            conn.execute(orders.update().where(orders.c.intent_id == intent_id).values(history=hist, updated_at=self._ts()))

    # ------------------------------------------------------------------ views
    def query(self, account_id: str | None = None, states: set[OrderState] | None = None, limit: int = 500) -> list[dict]:
        q = select(orders).order_by(orders.c.created_at.desc()).limit(limit)
        if account_id:
            q = q.where(orders.c.account_id == account_id)
        if states:
            q = q.where(orders.c.state.in_([s.value for s in states]))
        with self.db.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def working(self, account_id: str | None = None) -> list[dict]:
        return self.query(account_id, WORKING_ORDER_STATES)

    def unknown(self, account_id: str | None = None) -> list[dict]:
        return self.query(account_id, {S.UNKNOWN})

    def inflight_reducing_qty(self, account_id: str, instrument_key: str) -> int:
        tot = 0
        for r in self.working(account_id):
            if r["instrument_key"] == instrument_key and r["intent"].get("reduce_only"):
                tot += int(r["quantity"]) - int(r["filled_qty"])
        return tot

    def orders_since(self, account_id: str, since_ts: float) -> int:
        return sum(1 for r in self.query(account_id, limit=1000) if r["created_at"] >= since_ts and r["state"] != S.REJECTED_PRE_TRADE.value)

    @staticmethod
    def is_final(rec: dict) -> bool:
        return OrderState(rec["state"]) in FINAL_ORDER_STATES
