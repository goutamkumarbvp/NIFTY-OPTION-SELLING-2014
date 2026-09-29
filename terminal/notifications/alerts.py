"""Alert engine: dashboard stream + optional Telegram / e-mail delivery with de-duplication."""
from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from email.message import EmailMessage
from typing import Dict, List

import httpx

from terminal.core.models import Alert

log = logging.getLogger("terminal.alerts")


class AlertEngine:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.recent: List[Alert] = []
        self._dedupe: Dict[str, float] = {}
        self.delivery_stats = {"telegram_sent": 0, "telegram_failed": 0, "email_sent": 0, "email_failed": 0}
        self.channels = {"telegram": bool(terminal.settings.telegram_bot_token and terminal.settings.telegram_chat_id), "email": bool(terminal.settings.smtp_host and terminal.settings.alert_email_to)}

    async def emit(self, level: str, category: str, title: str, body: str = "", market: str = "", dedupe_seconds: float = 60.0) -> Alert | None:
        key = f"{level}:{category}:{title}"
        now = time.time()
        if now - self._dedupe.get(key, 0) < dedupe_seconds:
            return None
        self._dedupe[key] = now
        alert = Alert(level=level, category=category, title=title, body=body, market=market)
        self.recent.append(alert)
        self.recent = self.recent[-300:]
        self.t.db.save_alert(alert.model_dump(mode="json"))
        self.t.log(level if level in ("INFO", "WARNING", "CRITICAL") else "INFO", f"alert.{category}", f"{title} — {body}")
        await self.t.bus.publish("alert", alert)
        if level in ("WARNING", "CRITICAL"):
            asyncio.create_task(self._deliver(alert))
        return alert

    async def _deliver(self, alert: Alert) -> None:
        s = self.t.settings
        text = f"[{alert.level}] {alert.title}\n{alert.body}"
        if self.channels["telegram"]:
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    r = await client.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage", json={"chat_id": s.telegram_chat_id, "text": text})
                    r.raise_for_status()
                self.delivery_stats["telegram_sent"] += 1
            except Exception as exc:
                self.delivery_stats["telegram_failed"] += 1
                log.warning("telegram delivery failed: %s", exc)
        if self.channels["email"] and alert.level == "CRITICAL":
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._send_email, alert.title, text)
                self.delivery_stats["email_sent"] += 1
            except Exception as exc:
                self.delivery_stats["email_failed"] += 1
                log.warning("email delivery failed: %s", exc)

    def _send_email(self, subject: str, body: str) -> None:
        s = self.t.settings
        msg = EmailMessage()
        msg["Subject"] = f"[AI Options Terminal] {subject}"
        msg["From"] = s.smtp_user or "terminal@localhost"
        msg["To"] = s.alert_email_to
        msg.set_content(body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as srv:
            srv.starttls()
            if s.smtp_user:
                srv.login(s.smtp_user, s.smtp_password)
            srv.send_message(msg)

    def acknowledge(self, alert_id: str) -> bool:
        for a in self.recent:
            if a.id == alert_id:
                a.acknowledged = True
                self.t.db.save_alert(a.model_dump(mode="json"))
                return True
        return False

    def describe(self) -> dict:
        return {"channels": self.channels, "delivery": self.delivery_stats, "recent": [a.model_dump(mode="json") for a in self.recent[-100:][::-1]]}
