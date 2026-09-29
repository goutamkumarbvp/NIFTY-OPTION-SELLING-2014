"""Web Push delivery for the installed PWA (VAPID). Subscriptions are stored in the
runtime database; WARNING / CRITICAL alerts are pushed to every subscribed phone.
Without `pywebpush` or VAPID keys the feature is simply reported as unavailable."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List

log = logging.getLogger("terminal.push")


class WebPush:
    KEY = "push_subscriptions"

    def __init__(self, terminal) -> None:
        self.t = terminal
        s = terminal.settings
        self.public_key = (s.vapid_public_key or "").strip()
        self.private_key = (s.vapid_private_key or "").strip()
        self.subject = (s.vapid_subject or "mailto:operator@localhost").strip()
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        try:
            import pywebpush  # noqa: F401
            self._lib = True
        except Exception:
            self._lib = False

    @property
    def enabled(self) -> bool:
        return bool(self._lib and self.public_key and self.private_key)

    # ---------------------------------------------------------- subscriptions
    def subscriptions(self) -> List[Dict[str, Any]]:
        return list(self.t.db.get_setting(self.KEY, []) or [])

    def subscribe(self, sub: Dict[str, Any], user: str = "") -> int:
        subs = [x for x in self.subscriptions() if x.get("endpoint") != sub.get("endpoint")]
        subs.append({"endpoint": sub["endpoint"], "keys": sub.get("keys", {}), "expirationTime": sub.get("expirationTime"), "user": user, "ts": time.time(), "ua": sub.get("ua", "")[:120]})
        self.t.db.set_setting(self.KEY, subs[-50:])
        self.t.audit.record("PUSH_SUBSCRIBED", {"user": user, "endpoint": sub["endpoint"][:60]}, user or "pwa")
        return len(subs)

    def unsubscribe(self, endpoint: str) -> int:
        subs = [x for x in self.subscriptions() if x.get("endpoint") != endpoint]
        self.t.db.set_setting(self.KEY, subs)
        return len(subs)

    # ---------------------------------------------------------------- deliver
    def _send_one(self, sub: Dict[str, Any], payload: str) -> None:
        from pywebpush import webpush

        webpush(subscription_info={"endpoint": sub["endpoint"], "keys": sub.get("keys", {})}, data=payload, vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject}, ttl=600, timeout=8)

    async def send(self, title: str, body: str = "", level: str = "INFO", category: str = "", view: str = "overview") -> Dict[str, int]:
        subs = self.subscriptions()
        if not self.enabled or not subs:
            return {"sent": 0, "failed": 0, "skipped": len(subs)}
        payload = json.dumps({"title": title, "body": body, "level": level, "category": category, "view": view, "ts": time.time()})
        loop = asyncio.get_running_loop()
        sent = failed = 0
        dead: List[str] = []
        for sub in subs:
            try:
                await loop.run_in_executor(None, self._send_one, sub, payload)
                sent += 1
            except Exception as exc:  # 404 / 410 mean the browser dropped the subscription
                failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (404, 410):
                    dead.append(sub["endpoint"])
        for ep in dead:
            self.unsubscribe(ep)
        self.sent += sent
        self.failed += failed
        return {"sent": sent, "failed": failed, "skipped": 0}

    def describe(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "library": self._lib, "keys_configured": bool(self.public_key and self.private_key), "public_key": self.public_key or None,
                "subscriptions": len(self.subscriptions()), "sent": self.sent, "failed": self.failed, "last_error": self.last_error}
