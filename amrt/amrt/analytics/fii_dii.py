"""FII / DII institutional analytics from official NSE publications.

Sources (official, published by NSE):
* Participant-wise open interest, F&O (daily CSV):
  https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv
* FII/FPI & DII cash-market provisional activity (daily, ₹ crore):
  https://www.nseindia.com/api/fiidiiTradeReact  (JSON)  or the CSV export of that page.

What is computed — and what is not:
* Gross long, gross short, net (= long − short) per instrument class and client type.
* Day-over-day change in each position = value(D) − value(previous available date).
* Cash market: buy, sell, net (= buy − sell, checked against the published net).
* Period aggregates (W / M / Y / 5Y) of cash net flows, and end-of-period positions.
* Completeness = available trading days ÷ expected trading days in the period
  (weekdays minus a supplied holiday list). Periods below the threshold are flagged INCOMPLETE.
* No profit, loss, win rate or drawdown is computed for institutions: position
  data does not include prices paid, so realised or unrealised P&L cannot be
  derived defensibly. Changes in positions are NOT profit or loss.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

PARTICIPANT_FIELDS = ["future_index_long", "future_index_short", "future_stock_long", "future_stock_short", "option_index_call_long",
                      "option_index_put_long", "option_index_call_short", "option_index_put_short", "option_stock_call_long", "option_stock_put_long",
                      "option_stock_call_short", "option_stock_put_short", "total_long_contracts", "total_short_contracts"]
CLIENT_TYPES = ["Client", "DII", "FII", "Pro"]
METHODOLOGY_VERSION = "fiidii/1.0"


class ParseError(ValueError):
    pass


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def _num(v: str) -> float:
    s = str(v).strip().replace(",", "")
    if s in ("", "-"):
        raise ParseError(f"empty numeric field {v!r}")
    return float(s)


def _date_from_title(title: str) -> dt.date | None:
    m = re.search(r"as on\s+([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", title)
    if m:
        return dt.datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
    return None


def file_hash(raw: str | bytes) -> str:
    return hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()


def parse_participant_oi(raw: str, file_date: dt.date | None = None, tolerance: float = 0.5) -> list[dict[str, Any]]:
    """Parse the NSE participant-wise OI CSV. Validates that client types sum to the TOTAL row."""
    rows = list(csv.reader(io.StringIO(raw.strip())))
    if not rows:
        raise ParseError("empty file")
    date = file_date
    start = 0
    if rows[0] and "participant" in rows[0][0].lower():
        date = _date_from_title(rows[0][0]) or date
        start = 1
    if date is None:
        raise ParseError("publication date not found in title and not supplied")
    header = [_norm(h) for h in rows[start]]
    if header[0] != "client_type":
        raise ParseError(f"unexpected header {rows[start][:3]}")
    idx = {}
    for f in PARTICIPANT_FIELDS:
        if f not in header:
            raise ParseError(f"missing column {f}")
        idx[f] = header.index(f)
    by_type: dict[str, dict[str, float]] = {}
    for r in rows[start + 1:]:
        if not r or not r[0].strip():
            continue
        ctype = r[0].strip()
        by_type[ctype if ctype.upper() != "TOTAL" else "TOTAL"] = {f: _num(r[i]) for f, i in idx.items()}
    for ct in CLIENT_TYPES:
        if ct not in by_type:
            raise ParseError(f"missing client type {ct}")
    if "TOTAL" in by_type:
        for f in PARTICIPANT_FIELDS:
            s = sum(by_type[ct][f] for ct in CLIENT_TYPES)
            if abs(s - by_type["TOTAL"][f]) > tolerance:
                raise ParseError(f"client types do not sum to TOTAL for {f}: {s} vs {by_type['TOTAL'][f]}")
    return [{"date": date.isoformat(), "client_type": ct, "figures": by_type[ct]} for ct in CLIENT_TYPES + (["TOTAL"] if "TOTAL" in by_type else [])]


def _cash_category(raw: str) -> str:
    s = raw.upper()
    if "FII" in s or "FPI" in s:
        return "FII"
    if "DII" in s:
        return "DII"
    raise ParseError(f"unknown category {raw!r}")


def _cash_date(s: str) -> dt.date:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ParseError(f"bad date {s!r}")


def parse_fii_dii_cash(raw: str, tolerance: float = 0.05) -> list[dict[str, Any]]:
    """Parse NSE FII/DII cash activity (the JSON API response or the page's CSV export). Values in ₹ crore."""
    text = raw.strip()
    items: list[dict[str, Any]] = []
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("data") or data.get("rows") or []
        for d in data:
            items.append({"category": d.get("category"), "date": d.get("date"), "buy": d.get("buyValue"), "sell": d.get("sellValue"), "net": d.get("netValue")})
    else:
        rdr = csv.DictReader(io.StringIO(text))
        for d in rdr:
            nd = {_norm(k): v for k, v in d.items() if k}
            items.append({"category": nd.get("category"), "date": nd.get("date"), "buy": nd.get("buy_value"), "sell": nd.get("sell_value"), "net": nd.get("net_value")})
    out = []
    for it in items:
        if not it.get("category"):
            continue
        buy, sell, net = _num(it["buy"]), _num(it["sell"]), _num(it["net"])
        if abs((buy - sell) - net) > tolerance:
            raise ParseError(f"net {net} != buy {buy} − sell {sell}")
        out.append({"date": _cash_date(str(it["date"])).isoformat(), "category": _cash_category(str(it["category"])), "buy_value": buy, "sell_value": sell, "net_value": net})
    if not out:
        raise ParseError("no rows")
    return out


# ------------------------------------------------------------------ analytics
def net_positions(rec_figures: dict[str, float]) -> dict[str, float]:
    f = rec_figures
    return {
        "index_futures_net": f["future_index_long"] - f["future_index_short"],
        "stock_futures_net": f["future_stock_long"] - f["future_stock_short"],
        "index_call_net": f["option_index_call_long"] - f["option_index_call_short"],
        "index_put_net": f["option_index_put_long"] - f["option_index_put_short"],
        "total_net": f["total_long_contracts"] - f["total_short_contracts"],
        "index_futures_long_pct": round(100.0 * f["future_index_long"] / (f["future_index_long"] + f["future_index_short"]), 2)
        if (f["future_index_long"] + f["future_index_short"]) > 0 else None,
    }


def participant_series(records: Iterable[dict[str, Any]], client_type: str) -> list[dict[str, Any]]:
    rows = sorted((r for r in records if r["client_type"] == client_type), key=lambda r: r["date"])
    out, prev = [], None
    for r in rows:
        nets = net_positions(r["figures"])
        change = None if prev is None else {k: (None if nets[k] is None or prev[k] is None else round(nets[k] - prev[k], 2)) for k in nets}
        out.append({"date": r["date"], "gross": r["figures"], "net": nets, "change_vs_previous": change})
        prev = nets
    return out


def expected_trading_days(start: dt.date, end: dt.date, holidays: set[str] | None = None) -> list[dt.date]:
    holidays = holidays or set()
    days, d = [], start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def _period_key(d: dt.date, period: str) -> str:
    if period == "D":
        return d.isoformat()
    if period == "W":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if period == "M":
        return f"{d.year}-{d.month:02d}"
    if period == "Y":
        return str(d.year)
    raise ValueError(period)


def aggregate_cash(records: Iterable[dict[str, Any]], category: str, period: str, holidays: set[str] | None = None,
                   min_completeness: float = 0.9, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Sum of net flows per period with completeness. period ∈ D, W, M, Y, 5Y."""
    rows = sorted((r for r in records if r["category"] == category), key=lambda r: r["date"])
    if not rows:
        return []
    if period == "5Y":
        end = today or dt.date.fromisoformat(rows[-1]["date"])
        start = end.replace(year=end.year - 5) + dt.timedelta(days=1)
        sel = [r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()]
        exp = expected_trading_days(start, end, holidays)
        comp = len(sel) / len(exp) if exp else 0.0
        return [{"period": f"{start.isoformat()}..{end.isoformat()}", "net_value": round(sum(r["net_value"] for r in sel), 2),
                 "buy_value": round(sum(r["buy_value"] for r in sel), 2), "sell_value": round(sum(r["sell_value"] for r in sel), 2),
                 "days": len(sel), "expected_days": len(exp), "completeness": round(comp, 4), "status": "OK" if comp >= min_completeness else "INCOMPLETE"}]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[_period_key(dt.date.fromisoformat(r["date"]), period)].append(r)
    out = []
    for key, rs in sorted(groups.items()):
        d0 = dt.date.fromisoformat(rs[0]["date"])
        if period == "D":
            start = end = d0
        elif period == "W":
            start = d0 - dt.timedelta(days=d0.weekday())
            end = start + dt.timedelta(days=6)
        elif period == "M":
            start = d0.replace(day=1)
            end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        else:
            start, end = d0.replace(month=1, day=1), d0.replace(month=12, day=31)
        if today is not None and end > today:
            end = today
        exp = expected_trading_days(start, end, holidays)
        comp = len(rs) / len(exp) if exp else 0.0
        out.append({"period": key, "net_value": round(sum(r["net_value"] for r in rs), 2), "buy_value": round(sum(r["buy_value"] for r in rs), 2),
                    "sell_value": round(sum(r["sell_value"] for r in rs), 2), "days": len(rs), "expected_days": len(exp), "completeness": round(min(1.0, comp), 4),
                    "status": "OK" if comp >= min_completeness else "INCOMPLETE"})
    return out


def participant_url(day: dt.date) -> str:
    return f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{day.strftime('%d%m%Y')}.csv"


CASH_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

METHODOLOGY = {
    "version": METHODOLOGY_VERSION,
    "net_position": "gross long contracts − gross short contracts (per instrument class, per client type)",
    "daily_change": "net(D) − net(previous available publication date); gaps are not interpolated",
    "cash_net": "buy value − sell value (₹ crore), cross-checked against the published net value",
    "completeness": "available trading days ÷ expected trading days (weekdays minus supplied exchange holidays)",
    "not_computed": "profit, loss, win rate, drawdown — position counts carry no prices, so P&L cannot be derived",
}
