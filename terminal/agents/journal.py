"""Daily journal: what the desk did, why, and how it went. Written at the end
of the operating window (and on demand), with the narrator's briefing when an
LLM is configured. Retrieval finds similar past days by feature distance."""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List

from terminal.core.clock import now_ist


class Journal:
    def __init__(self, terminal) -> None:
        self.t = terminal

    def build_entry(self, day: str | None = None) -> Dict[str, Any]:
        t = self.t
        day = day or now_ist().date().isoformat()
        day_start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        trades = [x for x in t.db.trades(limit=2000, since=day_start)]
        runs = [r for r in t.db.runs(300) if r.get("entered_at", 0) >= day_start]
        decisions = [d for d in t.db.council(300) if d.get("ts", 0) >= day_start]
        alerts = [a for a in t.db.alerts(300) if a.get("ts", 0) >= day_start and a.get("level") in ("WARNING", "CRITICAL")]
        by_decision: Dict[str, int] = {}
        for d in decisions:
            by_decision[d["decision"]] = by_decision.get(d["decision"], 0) + 1
        pnl = round(sum(float(x["pnl"]) for x in trades), 2)
        wins = sum(1 for x in trades if float(x["pnl"]) > 0)
        m = {x["symbol"]: x for x in t.market_overview()}
        nifty = m.get("NIFTY", {})
        vix = t.processor.last_tick("INDIAVIX")
        entry = {
            "day": day, "written_at": time.time(), "mode": t.mode.value, "env": t.env.value,
            "pnl": pnl, "trades": len(trades), "win_rate": round(wins / len(trades), 3) if trades else None, "charges": round(sum(float(x.get("charges") or 0) for x in trades), 2),
            "runs": [{"strategy": r["strategy"], "underlying": r["underlying"], "lots": r["lots"], "pnl": r.get("realized_pnl"), "exit": r.get("exit_reason"), "status": r.get("status")} for r in runs],
            "council": {"cycles": len(decisions), "by_decision": by_decision},
            "alerts": [{"level": a["level"], "title": a["title"]} for a in alerts[:20]],
            "market": {"nifty_close": nifty.get("ltp"), "nifty_change_pct": nifty.get("change_pct"), "vix": vix.ltp if vix else None, "pcr": nifty.get("pcr"), "iv_atm": nifty.get("iv_atm")},
            "features": {"nifty_change_pct": nifty.get("change_pct") or 0.0, "vix": (vix.ltp if vix else 13.0), "pcr": nifty.get("pcr") or 1.0, "iv_atm": nifty.get("iv_atm") or 13.0},
            "brief": t.council.last_brief.get("NIFTY") or "",
            "lessons": self._lessons(trades, runs, alerts),
        }
        return entry

    @staticmethod
    def _lessons(trades, runs, alerts) -> List[str]:
        out: List[str] = []
        sl = [r for r in runs if str(r.get("exit_reason", "")).startswith("STOP_LOSS")]
        tg = [r for r in runs if str(r.get("exit_reason", "")).startswith("TARGET")]
        if sl and len(sl) > len(tg):
            out.append(f"{len(sl)} stop-loss exits vs {len(tg)} targets: consider wider strikes or smaller size in this regime.")
        if any(a.get("level") == "CRITICAL" for a in alerts):
            out.append("A CRITICAL alert fired today; review the audit trail before the next session.")
        if trades and sum(float(x["pnl"]) for x in trades) > 0 and not sl:
            out.append("Clean day: no stop-loss hits; the regime call was right.")
        if not trades:
            out.append("No trades: the council stayed out (thresholds or vetoes). Check evaluation hit-rates before lowering thresholds.")
        else:
            net = sum(float(x["pnl"]) for x in trades)
            out.append(f"{len(trades)} fills, net ₹{net:,.0f}; {len(sl)} stop-loss and {len(tg)} target exits across {len(runs)} run(s).")
        return out

    def write_today(self) -> Dict[str, Any]:
        entry = self.build_entry()
        self.t.db.save_journal(entry["day"], entry)
        self.t.audit.record("JOURNAL_WRITTEN", {"day": entry["day"], "pnl": entry["pnl"], "trades": entry["trades"]}, "journal")
        return entry

    def similar_days(self, features: Dict[str, float], limit: int = 5) -> List[Dict[str, Any]]:
        rows = self.t.db.journal(365)
        scored = []
        for r in rows:
            f = r.get("features") or {}
            d = math.sqrt(((f.get("nifty_change_pct", 0) - features.get("nifty_change_pct", 0)) / 1.0) ** 2 + ((f.get("vix", 13) - features.get("vix", 13)) / 3.0) ** 2 + ((f.get("pcr", 1) - features.get("pcr", 1)) / 0.2) ** 2 + ((f.get("iv_atm", 13) - features.get("iv_atm", 13)) / 3.0) ** 2)
            scored.append((d, r))
        scored.sort(key=lambda x: x[0])
        return [{"day": r["day"], "distance": round(d, 3), "pnl": r["pnl"], "trades": r["trades"], "lessons": r.get("lessons", [])} for d, r in scored[:limit]]
