"""Injectable clock. All safety logic reads time through a Clock so tests can control it."""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = dt.UTC


class Clock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)

    def ts(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def ist(self) -> dt.datetime:
        return self.now().astimezone(IST)


class ManualClock(Clock):
    """Deterministic clock for tests and replays."""

    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 09:30 IST on a Monday
        self._mono = 1000.0

    def now(self) -> dt.datetime:
        return self._now

    def ts(self) -> float:
        return self._now.timestamp()

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)
        self._mono += seconds

    def set(self, when: dt.datetime) -> None:
        """Jump to an absolute time (forward or backward); monotonic time only moves forward."""
        delta = (when - self._now).total_seconds()
        self._now = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
        self._mono += abs(delta)

    def set_ist(self, hh: int, mm: int, ss: int = 0) -> None:
        cur = self._now.astimezone(IST)
        target = cur.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        delta = (target - cur).total_seconds()
        self.advance(delta)


def parse_hhmm(s: str) -> dt.time:
    hh, mm = s.strip().split(":")
    return dt.time(int(hh), int(mm))


def ist_date(ts: float) -> dt.date:
    return dt.datetime.fromtimestamp(ts, IST).date()
