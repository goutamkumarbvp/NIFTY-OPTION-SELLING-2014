"""Market clock utilities (IST) and expiry calendar."""
from __future__ import annotations

import datetime as dt
from typing import List
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE / BSE holidays (illustrative list, extend via settings/holidays.json)
DEFAULT_HOLIDAYS = {
    "2026-01-26", "2026-02-17", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
    "2026-05-01", "2026-05-28", "2026-06-26", "2026-08-15", "2026-08-26", "2026-10-02", "2026-10-20",
    "2026-11-09", "2026-11-24", "2026-12-25",
}


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def parse_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def is_holiday(d: dt.date, holidays: set | None = None) -> bool:
    hol = holidays or DEFAULT_HOLIDAYS
    return d.weekday() >= 5 or d.isoformat() in hol


def session_state(now: dt.datetime, open_s: str, close_s: str, holidays: set | None = None) -> str:
    """Return PRE_OPEN | OPEN | CLOSED for the given session times."""
    if is_holiday(now.date(), holidays):
        return "CLOSED"
    t = now.time()
    o, c = parse_hhmm(open_s), parse_hhmm(close_s)
    if t < o:
        return "PRE_OPEN"
    if t >= c:
        return "CLOSED"
    return "OPEN"


def next_weekday_expiry(from_date: dt.date, weekday: int, holidays: set | None = None) -> dt.date:
    """Nearest expiry date on `weekday` (0=Mon). If the day is a holiday, expiry
    moves to the previous trading day (exchange convention)."""
    days_ahead = (weekday - from_date.weekday()) % 7
    d = from_date + dt.timedelta(days=days_ahead)
    hol = holidays or DEFAULT_HOLIDAYS
    while d.weekday() >= 5 or d.isoformat() in hol:
        d -= dt.timedelta(days=1)
    if d < from_date:
        return next_weekday_expiry(from_date + dt.timedelta(days=7 - days_ahead if days_ahead else 7), weekday, holidays)
    return d


def last_weekday_of_month(year: int, month: int, weekday: int, holidays: set | None = None) -> dt.date:
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    d = nxt - dt.timedelta(days=1)
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    hol = holidays or DEFAULT_HOLIDAYS
    while d.weekday() >= 5 or d.isoformat() in hol:
        d -= dt.timedelta(days=1)
    return d


def expiry_series(from_date: dt.date, weekday: int, weekly: bool, count: int = 4, holidays: set | None = None) -> List[dt.date]:
    out: List[dt.date] = []
    if weekly:
        d = next_weekday_expiry(from_date, weekday, holidays)
        while len(out) < count:
            if d >= from_date and d not in out:
                out.append(d)
            d = next_weekday_expiry(d + dt.timedelta(days=1), weekday, holidays)
        return out
    y, m = from_date.year, from_date.month
    while len(out) < count:
        d = last_weekday_of_month(y, m, weekday, holidays)
        if d >= from_date:
            out.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def year_fraction_to_expiry(now: dt.datetime, expiry: dt.date, close_s: str = "15:30") -> float:
    """Time to expiry in years, using calendar time to the session close of expiry day."""
    exp_dt = dt.datetime.combine(expiry, parse_hhmm(close_s), IST)
    seconds = (exp_dt - now).total_seconds()
    return max(seconds, 60.0) / (365.0 * 24 * 3600)
