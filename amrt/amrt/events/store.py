"""Append-only, hash-chained event store (the audit trail).

Every event carries a correlation id (the decision / incident it belongs to), an
optional causation id and idempotency key, the acting principal and a redacted
payload. `hash = sha256(prev_hash || canonical(event))` links the chain; the
database rejects UPDATE/DELETE on the table. `verify()` recomputes the chain.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from amrt.core.ids import canonical_json, new_id, sha256_hex
from amrt.security.identity import Principal
from amrt.security.redact import REDACTOR
from amrt.storage.db import Database, event_chain_head, events


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    ts: float
    type: str
    actor_id: str
    actor_kind: str
    correlation_id: str | None
    causation_id: str | None
    idempotency_key: str | None
    payload: dict
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _body(event_id: str, ts: float, type_: str, actor_id: str, actor_kind: str, correlation_id, causation_id, idempotency_key, payload) -> dict:
    return {"event_id": event_id, "ts": round(ts, 6), "type": type_, "actor_id": actor_id, "actor_kind": actor_kind, "correlation_id": correlation_id,
            "causation_id": causation_id, "idempotency_key": idempotency_key, "payload": payload}


class EventStore:
    def __init__(self, db: Database, clock_ts: Callable[[], float] = time.time) -> None:
        self.db = db
        self._ts = clock_ts
        self._listeners: list[Callable[[Event], None]] = []
        self.appended = 0
        self.failures = 0

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def append(self, type_: str, payload: dict[str, Any], actor: Principal, correlation_id: str | None = None,
               causation_id: str | None = None, idempotency_key: str | None = None) -> Event:
        clean = REDACTOR.value(payload)
        try:
            with self.db.tx() as conn:
                if idempotency_key:
                    prev = conn.execute(select(events).where(events.c.type == type_, events.c.idempotency_key == idempotency_key)).first()
                    if prev is not None:
                        return Event(**prev._mapping)
                q = select(event_chain_head).where(event_chain_head.c.id == 1)
                if self.db.dialect == "postgresql":
                    q = q.with_for_update()
                head = conn.execute(q).first()
                prev_hash = head.hash if head else "0" * 64
                eid, ts = new_id("EV"), self._ts()
                body = _body(eid, ts, type_, actor.id, actor.kind.value, correlation_id, causation_id, idempotency_key, clean)
                h = sha256_hex(prev_hash + canonical_json(body))
                res = conn.execute(events.insert().values(event_id=eid, ts=round(ts, 6), type=type_, actor_id=actor.id, actor_kind=actor.kind.value,
                                                          correlation_id=correlation_id, causation_id=causation_id, idempotency_key=idempotency_key,
                                                          payload=clean, prev_hash=prev_hash, hash=h))
                seq = int(res.inserted_primary_key[0])
                conn.execute(event_chain_head.update().where(event_chain_head.c.id == 1).values(seq=seq, hash=h))
        except Exception:
            self.failures += 1
            raise
        ev = Event(seq=seq, event_id=eid, ts=round(ts, 6), type=type_, actor_id=actor.id, actor_kind=actor.kind.value, correlation_id=correlation_id,
                   causation_id=causation_id, idempotency_key=idempotency_key, payload=clean, prev_hash=prev_hash, hash=h)
        self.appended += 1
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    # ------------------------------------------------------------------ query
    def _rows(self, q) -> list[Event]:
        with self.db.engine.connect() as conn:
            return [Event(**r._mapping) for r in conn.execute(q)]

    def tail(self, limit: int = 200, type_prefix: str | None = None) -> list[Event]:
        q = select(events).order_by(events.c.seq.desc()).limit(limit)
        if type_prefix:
            q = q.where(events.c.type.like(f"{type_prefix}%"))
        return self._rows(q)

    def by_correlation(self, correlation_id: str) -> list[Event]:
        return self._rows(select(events).where(events.c.correlation_id == correlation_id).order_by(events.c.seq))

    def by_type(self, type_: str, limit: int = 500) -> list[Event]:
        return self._rows(select(events).where(events.c.type == type_).order_by(events.c.seq.desc()).limit(limit))

    def count(self) -> int:
        with self.db.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(events)).scalar() or 0)

    def iter_all(self, batch: int = 2000) -> Iterable[Event]:
        last = 0
        while True:
            rows = self._rows(select(events).where(events.c.seq > last).order_by(events.c.seq).limit(batch))
            if not rows:
                return
            yield from rows
            last = rows[-1].seq

    def verify(self) -> dict:
        prev = "0" * 64
        n = 0
        for ev in self.iter_all():
            body = _body(ev.event_id, ev.ts, ev.type, ev.actor_id, ev.actor_kind, ev.correlation_id, ev.causation_id, ev.idempotency_key, ev.payload)
            if ev.prev_hash != prev or sha256_hex(prev + canonical_json(body)) != ev.hash:
                return {"ok": False, "checked": n, "first_bad_seq": ev.seq}
            prev = ev.hash
            n += 1
        with self.db.engine.connect() as conn:
            head = conn.execute(select(event_chain_head)).first()
        if head is not None and n and head.hash != prev:
            return {"ok": False, "checked": n, "first_bad_seq": None, "reason": "chain head mismatch"}
        return {"ok": True, "checked": n, "head": prev}
