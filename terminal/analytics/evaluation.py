"""Council evaluation harness.

Every council decision is journaled with the market features it saw. After
``DECISION_SCORE_MINUTES`` the scorer looks at what actually happened: the
underlying's forward move relative to the expected move over that horizon
(the quantity a premium seller cares about), and, when a plan was executed,
the run's realised outcome. From that it maintains a scorecard: per agent, how
often a favourable score (> 0) was followed by a seller-friendly outcome and a
negative score by a hostile one. The scorecard feeds the learned model and is
shown on the dashboard.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List


def features_for(ctx, assessments) -> Dict[str, Any]:
    ind = ctx.indicators or {}
    ch = ctx.chain
    return {
        "spot": ctx.spot, "vix": ctx.vix, "vix_rank": (ctx.vix_rank or {}).get("rank"), "rsi": ind.get("rsi"), "trend": ind.get("trend"), "supertrend": ind.get("supertrend"),
        "realized_vol": ind.get("realized_vol"), "iv_atm": ch.iv_atm if ch else None, "pcr": ch.pcr if ch else None, "expected_move": ch.expected_move if ch else None,
        "dte": ctx.days_to_expiry, "ret_5m_pct": ind.get("ret_5m_pct"), "regime": ctx.memory.get("regime"),
        "agents": {a.agent: {"score": a.score, "confidence": a.confidence, "veto": a.veto, "stance": a.stance} for a in assessments},
    }


class CouncilEvaluator:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.horizon = int(getattr(terminal.settings, "decision_score_minutes", 30)) * 60
        self.scored = 0

    def record(self, decision, ctx, assessments) -> None:
        try:
            self.t.db.add_decision(decision.id, decision.ts, decision.underlying, decision.decision, {**features_for(ctx, assessments), "plan_id": decision.plan_id, "consensus": decision.consensus})
        except Exception:
            pass

    def _forward_move(self, underlying: str, ts: float) -> float | None:
        """Spot move (fraction) from the decision time to now, from the tick history."""
        st = self.t.processor.symbols.get(underlying)
        if not st or not st.last:
            return None
        ref = None
        for tick in st.ticks:
            if tick.ts >= ts:
                ref = tick
                break
        if ref is None or ref.ltp <= 0:
            return None
        return st.last.ltp / ref.ltp - 1

    def score_pending(self) -> int:
        now = time.time()
        n = 0
        for row in self.t.db.unscored_decisions(now - self.horizon):
            f = row["features"]
            move = self._forward_move(row["underlying"], row["ts"])
            em = f.get("expected_move") or 0.0
            spot = f.get("spot") or 0.0
            # horizon-scaled 1σ: expected move is to expiry; scale by sqrt(horizon / time-to-expiry)
            dte_days = max(float(f.get("dte") or 1.0), 0.02)
            horizon_days = max(self.horizon, 60.0) / 86400.0
            sigma_h = (em / spot) * math.sqrt(min(1.0, horizon_days / dte_days)) if spot and em else None
            seller_friendly: bool | None = None
            z = None
            if move is not None and sigma_h:
                z = move / sigma_h
                seller_friendly = abs(z) < 1.0
            run_pnl = None
            if f.get("plan_id"):
                for run in self.t.strategies.runs.values():
                    if run.plan_id == f["plan_id"]:
                        run_pnl = run.realized_pnl if run.status == "CLOSED" else run.mtm
                        break
            outcome = {"forward_move_pct": round(move * 100, 3) if move is not None else None, "z": round(z, 2) if z is not None else None, "seller_friendly": seller_friendly, "run_pnl": run_pnl, "scored_ts": now}
            self.t.db.score_decision(row["id"], outcome)
            n += 1
        self.scored += n
        return n

    def scorecard(self) -> Dict[str, Any]:
        rows = self.t.db.scored_decisions(2000)
        agents: Dict[str, Dict[str, float]] = {}
        decisions: Dict[str, Dict[str, float]] = {}
        z_values: List[float] = []
        for r in rows:
            out = r["outcome"]
            sf = out.get("seller_friendly")
            if sf is None:
                continue
            if out.get("z") is not None:
                z_values.append(abs(out["z"]))
            d = decisions.setdefault(r["decision"], {"n": 0, "friendly": 0})
            d["n"] += 1
            d["friendly"] += 1 if sf else 0
            for name, a in (r["features"].get("agents") or {}).items():
                sc = a.get("score", 0.0)
                if abs(sc) < 0.05:
                    continue
                st = agents.setdefault(name, {"n": 0, "hits": 0})
                st["n"] += 1
                st["hits"] += 1 if ((sc > 0) == sf) else 0
        return {
            "scored": len(rows), "horizon_minutes": self.horizon // 60,
            "agents": {k: {"n": v["n"], "hit_rate": round(v["hits"] / v["n"], 3) if v["n"] else None} for k, v in sorted(agents.items())},
            "decisions": {k: {"n": v["n"], "seller_friendly_rate": round(v["friendly"] / v["n"], 3) if v["n"] else None} for k, v in decisions.items()},
            "mean_abs_z": round(sum(z_values) / len(z_values), 3) if z_values else None,
        }
