"""Time based trading rules per exchange (entry window, square-off, sessions)."""
from __future__ import annotations

import datetime as dt
from typing import Dict, Optional

from terminal.core.clock import IST, is_holiday, now_ist, parse_hhmm, session_state
from terminal.core.models import Exchange, Underlying


class Scheduler:
    def __init__(self, settings, sim_always_open: bool = False) -> None:
        self.s = settings
        self.sim_always_open = sim_always_open
        self.overrides: Dict[str, str] = {}

    def _t(self, key: str) -> dt.time:
        return parse_hhmm(self.overrides.get(key) or getattr(self.s, key))

    def session(self, u: Underlying, now: Optional[dt.datetime] = None) -> str:
        now = now or now_ist()
        if self.sim_always_open:
            return "OPEN"
        return session_state(now, u.session_open, u.session_close)

    def can_enter(self, u: Underlying, now: Optional[dt.datetime] = None) -> tuple[bool, str]:
        now = now or now_ist()
        if not self.in_operating_window(now):
            return False, "OUTSIDE_OPERATING_HOURS"
        if self.session(u, now) != "OPEN":
            return False, "SESSION_CLOSED"
        if self.sim_always_open:
            return True, "OK"
        t = now.time()
        if u.exchange == Exchange.MCX:
            start, end = parse_hhmm("09:05"), parse_hhmm("22:30")
        else:
            start, end = self._t("entry_window_start"), self._t("entry_window_end")
        if t < start:
            return False, "BEFORE_ENTRY_WINDOW"
        if t > end:
            return False, "AFTER_ENTRY_WINDOW"
        return True, "OK"

    def in_operating_window(self, now: Optional[dt.datetime] = None) -> bool:
        """Terminal-wide window (default 09:00–23:30 IST): no council cycles and no
        new entries outside it; exits are always allowed."""
        now = now or now_ist()
        t = now.time()
        return self._t("terminal_start_time") <= t < self._t("terminal_end_time")

    def past_end_time(self, now: Optional[dt.datetime] = None) -> bool:
        now = now or now_ist()
        return now.time() >= self._t("terminal_end_time")

    def must_square_off(self, u: Underlying, now: Optional[dt.datetime] = None) -> bool:
        now = now or now_ist()
        if self.sim_always_open:
            return False
        t = now.time()
        cutoff = self._t("mcx_square_off_time") if u.exchange == Exchange.MCX else self._t("square_off_time")
        return t >= cutoff and session_state(now, u.session_open, u.session_close) != "CLOSED"

    def is_expiry_day(self, expiry: str, now: Optional[dt.datetime] = None) -> bool:
        now = now or now_ist()
        return now.date().isoformat() == expiry

    def describe(self) -> dict:
        now = now_ist()
        return {
            "now_ist": now.isoformat(timespec="seconds"),
            "holiday": is_holiday(now.date()),
            "operating_window": [self.overrides.get("terminal_start_time", self.s.terminal_start_time), self.overrides.get("terminal_end_time", self.s.terminal_end_time)],
            "in_operating_window": self.in_operating_window(now),
            "entry_window": [self.overrides.get("entry_window_start", self.s.entry_window_start), self.overrides.get("entry_window_end", self.s.entry_window_end)],
            "square_off": self.overrides.get("square_off_time", self.s.square_off_time),
            "mcx_square_off": self.overrides.get("mcx_square_off_time", self.s.mcx_square_off_time),
            "sim_always_open": self.sim_always_open,
        }
