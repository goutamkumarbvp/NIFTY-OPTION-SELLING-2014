"""Performance analytics from the trade book."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Dict, List

from terminal.core.clock import IST


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, IST).date().isoformat()


def summarize(trades: List[dict]) -> Dict:
    if not trades:
        return {"trades": 0, "net_pnl": 0.0, "gross_pnl": 0.0, "charges": 0.0, "win_rate": None, "avg_win": None, "avg_loss": None, "profit_factor": None,
                "max_drawdown": 0.0, "best": 0.0, "worst": 0.0, "daily": [], "monthly": [], "by_strategy": [], "by_market": [], "equity": []}
    rows = sorted(trades, key=lambda r: r["ts_close"])
    pnls = [float(r["pnl"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    equity, peak, dd, cum = [], 0.0, 0.0, 0.0
    for r, p in zip(rows, pnls):
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        equity.append({"ts": r["ts_close"], "equity": round(cum, 2)})
    daily: Dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    monthly: Dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    by_strat: Dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    by_market: Dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for r, p in zip(rows, pnls):
        for bucket, key in ((daily, _day(r["ts_close"])), (monthly, _day(r["ts_close"])[:7]), (by_strat, r.get("strategy") or "manual"), (by_market, r.get("exchange") or "?")):
            bucket[key]["pnl"] = round(bucket[key]["pnl"] + p, 2)
            bucket[key]["trades"] += 1
            bucket[key]["wins"] += 1 if p > 0 else 0
    def _fmt(d: Dict[str, dict]) -> List[dict]:
        return [{"key": k, **v, "win_rate": round(v["wins"] / v["trades"], 3) if v["trades"] else None} for k, v in sorted(d.items())]
    return {
        "trades": len(rows), "net_pnl": round(sum(pnls), 2), "gross_pnl": round(sum(pnls) + sum(float(r.get("charges") or 0) for r in rows), 2),
        "charges": round(sum(float(r.get("charges") or 0) for r in rows), 2), "win_rate": round(len(wins) / len(pnls), 3),
        "avg_win": round(gross_win / len(wins), 2) if wins else None, "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None, "max_drawdown": round(dd, 2), "best": round(max(pnls), 2), "worst": round(min(pnls), 2),
        "daily": _fmt(daily), "monthly": _fmt(monthly), "by_strategy": _fmt(by_strat), "by_market": _fmt(by_market), "equity": equity[-500:],
    }
