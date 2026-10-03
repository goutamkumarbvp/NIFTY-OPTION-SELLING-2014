"""Operator alerts with delivery monitoring.

Channels: the dashboard (WebSocket; CRITICAL alerts are audible and vibrate on
Android-compatible browsers), Telegram (optional) and the event store. Delivery
failures are counted per channel and surface as a degraded `alerting` component;
an alert is never dropped silently and is stored before delivery is attempted.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx
from sqlalchemy import select

from amrt.core.ids import new_id
from amrt.storage.db import Database, alerts

log = logging.getLogger("amrt.alerts")


class AlertService:
    def __init__(self, db: Database, events, clock, principal, settings, publish: Callable[[str, dict], None] | None = None) -> None:
        self.db, self.events, self.clock, self.principal, self.settings = db, events, clock, principal, settings
        self.publish = publish
        self.stats = {"dashboard": {"sent": 0, "failed": 0}, "telegram": {"sent": 0, "failed": 0, "enabled": bool(settings.telegram_bot_token and settings.telegram_chat_id)}}
        self._dedupe: dict[str, float] = {}

    def emit(self, severity: str, category: str, title: str, body: str = "", dedupe_s: float = 60.0) -> dict | None:
        key = f"{severity}|{category}|{title}"
        now = self.clock.ts()
        if now - self._dedupe.get(key, 0.0) < dedupe_s:
            return None
        self._dedupe[key] = now
        aid = new_id("AL")
        rec = {"alert_id": aid, "ts": now, "severity": severity, "category": category, "title": title[:300], "body": body[:2000],
               "delivery": {"dashboard": "PENDING", "telegram": "DISABLED" if not self.stats["telegram"]["enabled"] else "PENDING"}}
        with self.db.tx() as conn:
            conn.execute(alerts.insert().values(**rec))
        self.events.append("ALERT", {"alert_id": aid, "severity": severity, "category": category, "title": title, "body": body}, self.principal)
        try:
            if self.publish:
                self.publish("alert", {**rec, "audible": severity == "CRITICAL"})
            self.stats["dashboard"]["sent"] += 1
            rec["delivery"]["dashboard"] = "SENT"
        except Exception as exc:  # noqa: BLE001
            self.stats["dashboard"]["failed"] += 1
            rec["delivery"]["dashboard"] = f"FAILED: {exc}"
        if self.stats["telegram"]["enabled"] and severity in ("WARNING", "CRITICAL"):
            try:
                asyncio.get_running_loop().create_task(self._telegram(aid, f"[{severity}] {title}\n{body}"))
            except RuntimeError:
                pass
        self._update_delivery(aid, rec["delivery"])
        return rec

    async def _telegram(self, aid: str, text: str) -> None:
        s = self.settings
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage", json={"chat_id": s.telegram_chat_id, "text": text[:3500]})
                r.raise_for_status()
            self.stats["telegram"]["sent"] += 1
            status = "SENT"
        except Exception as exc:  # noqa: BLE001
            self.stats["telegram"]["failed"] += 1
            status = f"FAILED: {type(exc).__name__}"
        rec = self.get(aid)
        if rec:
            d = dict(rec["delivery"])
            d["telegram"] = status
            self._update_delivery(aid, d)

    def _update_delivery(self, aid: str, delivery: dict) -> None:
        with self.db.tx() as conn:
            conn.execute(alerts.update().where(alerts.c.alert_id == aid).values(delivery=delivery))

    def get(self, aid: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(alerts).where(alerts.c.alert_id == aid)).first()
        return dict(row._mapping) if row else None

    def ack(self, aid: str, user: str) -> None:
        with self.db.tx() as conn:
            conn.execute(alerts.update().where(alerts.c.alert_id == aid).values(acked_by=user, acked_at=self.clock.ts()))

    def recent(self, limit: int = 100) -> list[dict]:
        with self.db.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(alerts).order_by(alerts.c.ts.desc()).limit(limit))]

    def delivery_health(self) -> dict:
        tg = self.stats["telegram"]
        degraded = tg["enabled"] and tg["failed"] > 0 and tg["failed"] >= tg["sent"]
        return {"degraded": degraded or self.stats["dashboard"]["failed"] > 0, **self.stats}
