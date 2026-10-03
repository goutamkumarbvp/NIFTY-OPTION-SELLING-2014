"""Human authentication: scrypt password hashes, optional TOTP, lockout, sessions and step-up.

* Sessions live in memory (a restart logs everyone out — deliberate).
* Sensitive actions (mode change, policy/limit change, automation approval,
  freeze / kill-switch release, unquarantine) need a *step-up*: the password or
  TOTP re-entered within `step_up_ttl_seconds`.
* First run: when no user exists a one-time setup code is printed to the
  console (never logged to file); it creates the OWNER account exactly once.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

import pyotp
from sqlalchemy import func, select

from amrt.core.errors import AuthenticationRequired, PermissionDenied, StepUpRequired
from amrt.security.identity import Principal, PrincipalKind
from amrt.storage.db import Database, users

ROLE_KIND = {"OWNER": PrincipalKind.OWNER, "OPERATOR": PrincipalKind.OPERATOR, "READ_ONLY": PrincipalKind.READ_ONLY}
MIN_PASSWORD = 10


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32).hex()


@dataclass
class Session:
    token: str
    username: str
    role: str
    created_at: float
    expires_at: float
    step_up_at: float = 0.0
    principal: Principal = field(default=None)  # type: ignore[assignment]


class AuthService:
    def __init__(self, db: Database, clock_ts=time.time, session_ttl_minutes: int = 480, step_up_ttl_seconds: int = 300,
                 lockout_attempts: int = 5, lockout_minutes: int = 15) -> None:
        self.db = db
        self._ts = clock_ts
        self.session_ttl = session_ttl_minutes * 60
        self.step_up_ttl = step_up_ttl_seconds
        self.lockout_attempts = lockout_attempts
        self.lockout_seconds = lockout_minutes * 60
        self.sessions: dict[str, Session] = {}
        self._setup_code_hash: str | None = None
        self.failures: deque[float] = deque(maxlen=1000)
        self.lockouts: deque[float] = deque(maxlen=200)

    # --------------------------------------------------------------- users
    def user_count(self) -> int:
        with self.db.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(users)).scalar() or 0)

    def create_user(self, username: str, password: str, role: str, totp_secret: str | None = None) -> None:
        role = role.upper()
        if role not in ROLE_KIND:
            raise ValueError("role must be OWNER, OPERATOR or READ_ONLY")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        if not username.isidentifier():
            raise ValueError("username must be alphanumeric/underscore")
        salt = secrets.token_hex(16)
        with self.db.tx() as conn:
            conn.execute(users.insert().values(username=username, role=role, pw_hash=hash_password(password, salt), pw_salt=salt,
                                               totp_secret=totp_secret, created_at=self._ts(), failed_count=0, locked_until=0.0, disabled=False))

    def _row(self, username: str):
        with self.db.engine.connect() as conn:
            return conn.execute(select(users).where(users.c.username == username)).first()

    # ------------------------------------------------------------ first run
    def issue_setup_code(self) -> str | None:
        if self.user_count() > 0:
            return None
        code = secrets.token_urlsafe(12)
        self._setup_code_hash = hashlib.sha256(code.encode()).hexdigest()
        return code

    def complete_setup(self, code: str, username: str, password: str, totp_secret: str | None = None) -> None:
        if self.user_count() > 0 or self._setup_code_hash is None:
            raise PermissionDenied("setup already completed")
        if not hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), self._setup_code_hash):
            raise PermissionDenied("invalid setup code")
        self.create_user(username, password, "OWNER", totp_secret)
        self._setup_code_hash = None

    # ----------------------------------------------------------- login
    def _verify(self, row, password: str, totp: str | None) -> bool:
        ok = hmac.compare_digest(hash_password(password, row.pw_salt), row.pw_hash)
        if ok and row.totp_secret:
            ok = bool(totp) and pyotp.TOTP(row.totp_secret).verify(totp, valid_window=1)
        return ok

    def login(self, username: str, password: str, totp: str | None = None) -> Session:
        row = self._row(username)
        now = self._ts()
        if row is None or row.disabled:
            hash_password(password, "00" * 16)  # equalise timing
            self.failures.append(now)
            raise AuthenticationRequired("invalid credentials")
        if row.locked_until > now:
            raise AuthenticationRequired("account locked", locked_until=row.locked_until)
        if not self._verify(row, password, totp):
            fails = row.failed_count + 1
            locked = now + self.lockout_seconds if fails >= self.lockout_attempts else 0.0
            with self.db.tx() as conn:
                conn.execute(users.update().where(users.c.username == username).values(failed_count=0 if locked else fails, locked_until=locked))
            self.failures.append(now)
            if locked:
                self.lockouts.append(now)
            raise AuthenticationRequired("invalid credentials")
        with self.db.tx() as conn:
            conn.execute(users.update().where(users.c.username == username).values(failed_count=0, locked_until=0.0))
        tok = secrets.token_urlsafe(32)
        s = Session(token=tok, username=username, role=row.role, created_at=now, expires_at=now + self.session_ttl, step_up_at=now,
                    principal=Principal.of(ROLE_KIND[row.role], username))
        self.sessions[tok] = s
        return s

    def logout(self, token: str) -> None:
        self.sessions.pop(token, None)

    def session(self, token: str | None) -> Session:
        s = self.sessions.get(token or "")
        if s is None or s.expires_at <= self._ts():
            if s is not None:
                self.sessions.pop(s.token, None)
            raise AuthenticationRequired("login required")
        return s

    def step_up(self, token: str, password: str, totp: str | None = None) -> Session:
        s = self.session(token)
        row = self._row(s.username)
        if row is None or not self._verify(row, password, totp):
            raise AuthenticationRequired("step-up failed")
        s.step_up_at = self._ts()
        return s

    def require_step_up(self, s: Session) -> None:
        if self._ts() - s.step_up_at > self.step_up_ttl:
            raise StepUpRequired("re-enter your password to confirm this action")

    def recent_failures(self, window_s: float = 900.0) -> int:
        cut = self._ts() - window_s
        return sum(1 for t in self.failures if t >= cut)

    def recent_lockouts(self, window_s: float = 900.0) -> int:
        cut = self._ts() - window_s
        return sum(1 for t in self.lockouts if t >= cut)

    def describe(self) -> dict:
        return {"users": self.user_count(), "active_sessions": len(self.sessions), "setup_pending": self._setup_code_hash is not None,
                "lockout_attempts": self.lockout_attempts, "step_up_ttl_seconds": self.step_up_ttl}
