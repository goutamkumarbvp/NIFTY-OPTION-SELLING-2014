"""Incidents. Opened with evidence, appended to, resolved with a resolution note — never deleted or suppressed."""
from __future__ import annotations

from sqlalchemy import select

from amrt.core.ids import new_id
from amrt.storage.db import Database, incidents

SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")


class IncidentManager:
    def __init__(self, db: Database, events, clock) -> None:
        self.db, self.events, self.clock = db, events, clock

    def open(self, component: str, severity: str, title: str, evidence: dict, actor) -> dict:
        assert severity in SEVERITIES
        existing = self.find_open(component, title)
        if existing:
            body = dict(existing["body"])
            body.setdefault("occurrences", []).append({"ts": self.clock.ts(), "evidence": evidence})
            body["occurrences"] = body["occurrences"][-50:]
            with self.db.tx() as conn:
                conn.execute(incidents.update().where(incidents.c.incident_id == existing["incident_id"]).values(body=body))
            existing["body"] = body
            return existing
        iid = new_id("INC")
        body = {"evidence": evidence, "timeline": [{"ts": self.clock.ts(), "event": "OPENED", "by": actor.id}], "escalation": "OBSERVING", "actions": []}
        rec = {"incident_id": iid, "opened_at": self.clock.ts(), "severity": severity, "component": component, "title": title[:300], "status": "OPEN", "body": body}
        with self.db.tx() as conn:
            conn.execute(incidents.insert().values(**rec))
        self.events.append("INCIDENT_OPENED", {"incident_id": iid, "component": component, "severity": severity, "title": title, "evidence": evidence},
                           actor, correlation_id=iid)
        return rec

    def find_open(self, component: str, title: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(incidents).where(incidents.c.component == component, incidents.c.title == title[:300],
                                                       incidents.c.status.in_(["OPEN", "ESCALATED", "RECOVERING"]))).first()
        return dict(row._mapping) if row else None

    def get(self, incident_id: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(incidents).where(incidents.c.incident_id == incident_id)).first()
        return dict(row._mapping) if row else None

    def update(self, incident_id: str, actor, event: str, status: str | None = None, escalation: str | None = None, **detail) -> dict:
        rec = self.get(incident_id)
        body = dict(rec["body"])
        body.setdefault("timeline", []).append({"ts": self.clock.ts(), "event": event, "by": actor.id, **detail})
        if escalation:
            body["escalation"] = escalation
        values = {"body": body}
        if status:
            values["status"] = status
        with self.db.tx() as conn:
            conn.execute(incidents.update().where(incidents.c.incident_id == incident_id).values(**values))
        self.events.append("INCIDENT_UPDATED", {"incident_id": incident_id, "event": event, "status": status, "escalation": escalation, **detail}, actor,
                           correlation_id=incident_id)
        rec.update(values)
        return rec

    def resolve(self, incident_id: str, actor, resolution: str, verification: dict | None = None) -> dict:
        rec = self.update(incident_id, actor, "RESOLVED", status="RESOLVED", resolution=resolution, verification=verification or {})
        with self.db.tx() as conn:
            conn.execute(incidents.update().where(incidents.c.incident_id == incident_id).values(closed_at=self.clock.ts(), closed_by=actor.id))
        return rec

    def list(self, status: str | None = None, limit: int = 200) -> list[dict]:
        q = select(incidents).order_by(incidents.c.opened_at.desc()).limit(limit)
        if status:
            q = q.where(incidents.c.status == status)
        with self.db.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def open_count(self) -> int:
        return len([i for i in self.list(limit=1000) if i["status"] != "RESOLVED"])
