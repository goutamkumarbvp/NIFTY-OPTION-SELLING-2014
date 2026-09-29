"""Authentication & role based access.

* Loopback bind with no users / token configured -> open access as ``admin``
  (single-operator workstation default).
* ``API_AUTH_TOKEN`` -> bearer token grants ``admin``.
* ``TERMINAL_USERS="name:password:role,..."`` -> login issues session tokens.

Roles: viewer < trader < admin.
"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Dict

from fastapi import Depends, HTTPException, Request, WebSocket

ROLE_RANK = {"viewer": 0, "trader": 1, "admin": 2}


class AuthManager:
    def __init__(self, settings) -> None:
        self.s = settings
        self.sessions: Dict[str, dict] = {}
        self.users = {u["username"]: u for u in settings.user_table()}
        self.open_access = settings.is_loopback and not settings.api_auth_token and not self.users
        self.failures: Dict[str, list] = {}
        self.lockout_attempts = int(getattr(settings, "login_lockout_attempts", 5))
        self.lockout_seconds = float(getattr(settings, "login_lockout_minutes", 15)) * 60
        self.session_ttl = float(getattr(settings, "session_ttl_hours", 12)) * 3600

    def locked(self, username: str) -> bool:
        now = time.time()
        recent = [ts for ts in self.failures.get(username, []) if now - ts <= self.lockout_seconds]
        self.failures[username] = recent
        return len(recent) >= self.lockout_attempts

    def login(self, username: str, password: str) -> dict | None:
        if self.locked(username):
            return None
        u = self.users.get(username)
        if u is None or not hmac.compare_digest(u["password"], password):
            self.failures.setdefault(username, []).append(time.time())
            return None
        self.failures.pop(username, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"username": username, "role": u["role"], "created": time.time()}
        return {"token": token, "username": username, "role": u["role"]}

    def logout(self, token: str) -> None:
        self.sessions.pop(token, None)

    def identify(self, token: str | None) -> dict | None:
        if token:
            if self.s.api_auth_token and hmac.compare_digest(token, self.s.api_auth_token):
                return {"username": "api-token", "role": "admin"}
            sess = self.sessions.get(token)
            if sess:
                if time.time() - sess["created"] > self.session_ttl:
                    self.sessions.pop(token, None)
                    return None
                return sess
        if self.open_access:
            return {"username": "operator", "role": "admin"}
        return None

    def describe(self) -> dict:
        return {"open_access": self.open_access, "users_configured": len(self.users), "token_configured": bool(self.s.api_auth_token), "session_ttl_hours": self.session_ttl / 3600,
                "lockout": {"attempts": self.lockout_attempts, "minutes": self.lockout_seconds / 60, "locked_users": [u for u in self.failures if self.locked(u)]}}


def _extract(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key") or request.query_params.get("token")


def current_user(request: Request) -> dict:
    auth: AuthManager = request.app.state.auth
    user = auth.identify(_extract(request))
    if user is None:
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
    return user


def require(role: str):
    def _dep(user: dict = Depends(current_user)) -> dict:
        if ROLE_RANK.get(user["role"], -1) < ROLE_RANK[role]:
            raise HTTPException(status_code=403, detail=f"ROLE_{role.upper()}_REQUIRED")
        return user
    return _dep


def ws_user(websocket: WebSocket) -> dict | None:
    auth: AuthManager = websocket.app.state.auth
    token = websocket.query_params.get("token")
    return auth.identify(token)
