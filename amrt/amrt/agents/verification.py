"""Independent Verification Agent.

Re-derives key numbers from the raw chain through its own code path (it does
not call the analytics module) and checks every other agent's claims and any
proposal against raw data. A failed critical check makes its stance BLOCK.
"""
from __future__ import annotations

from amrt.agents.base import Agent, AgentContext, AgentOutput
from amrt.core.enums import DataLabel, Stance


def _close(a: float | None, b: float | None, rel: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


class IndependentVerificationAgent(Agent):
    name = "independent_verification"
    critical = True

    def analyze(self, ctx: AgentContext, peers: dict[str, AgentOutput] | None = None) -> dict:
        peers = peers or {}
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str = "", critical: bool = True) -> None:
            checks.append({"check": name, "passed": bool(ok), "detail": detail, "critical": critical})

        snap = ctx.chain
        # 1. PCR recomputed from raw rows inside the analytics window
        if snap is not None and ctx.analytics is not None and ctx.analytics.window:
            lo, hi = ctx.analytics.window[0], ctx.analytics.window[-1]
            ce = sum(float(r.ce.oi) for r in snap.rows if lo <= r.strike <= hi and r.ce is not None and r.ce.oi is not None)
            pe = sum(float(r.pe.oi) for r in snap.rows if lo <= r.strike <= hi and r.pe is not None and r.pe.oi is not None)
            mine = None if ce == 0 else pe / ce
            theirs = ctx.analytics.pcr_total_oi.value if ctx.analytics.pcr_total_oi else None
            if ctx.analytics.status == "OK":
                check("pcr_total_oi_recomputed", _close(round(mine, 4) if mine is not None else None, theirs, 1e-3), f"independent {mine} vs analytics {theirs}")
            reported = (peers.get("pcr_positioning") and peers["pcr_positioning"].observations.get("pcr_total_oi", {}) or {}).get("value")
            if reported is not None:
                check("pcr_agent_matches_raw", _close(reported, theirs), f"agent {reported} vs analytics {theirs}")
            mx = max(((float(r.ce.oi), r.strike) for r in snap.rows if lo <= r.strike <= hi and r.ce is not None and r.ce.oi is not None), default=None)
            if mx is not None and ctx.analytics.ce_max_oi is not None:
                check("ce_max_oi_value", _close(mx[0], ctx.analytics.ce_max_oi.value), f"raw {mx} vs {ctx.analytics.ce_max_oi.value}")
        # 2. timestamps: market-data agents must cite the snapshot this cycle used
        for name, out in peers.items():
            if out.data_as_of is not None and snap is not None and out.data_as_of != snap.ts and out.data_label != "OFFICIAL END-OF-DAY":
                check(f"{name}_timestamp", False, "agent used a different snapshot than the cycle")
            if out.data_as_of is not None and out.data_as_of > ctx.now_ts + 1:
                check(f"{name}_future_timestamp", False, "data timestamp in the future")
        if snap is not None:
            age_ok = ctx.chain_age_ms is not None and ctx.chain_age_ms <= ctx.max_data_age_ms
            check("chain_fresh", age_ok, f"age {ctx.chain_age_ms} ms, limit {ctx.max_data_age_ms} ms")
        # 3. proposal checks
        strat = peers.get("quantitative_strategy")
        act = strat.proposed_action if strat else None
        if act is not None:
            pol = ctx.policy or {}
            quotes = {}
            if snap is not None:
                for r in snap.rows:
                    for leg in (r.ce, r.pe):
                        if leg is not None:
                            quotes[leg.instrument_key] = leg
            if ctx.account_kind == "LIVE":
                check("live_requires_verified_data", ctx.chain_label == DataLabel.LIVE_VERIFIED.value, f"label {ctx.chain_label}")
            check("expiry_matches_chain", snap is not None and act.expiry == snap.expiry.isoformat(), f"{act.expiry}")
            for leg in act.legs:
                q = quotes.get(leg.instrument_key)
                if leg.purpose in ("ENTRY", "HEDGE", "ADJUSTMENT") and not leg.reduce_only:
                    check(f"leg_listed:{leg.instrument_key}", q is not None, "instrument present in the loaded chain")
                    if pol:
                        check(f"lots_within_policy:{leg.instrument_key}", leg.lots <= pol.get("max_lots_per_order", 0), f"{leg.lots} lots")
                if q is not None and leg.limit_price is not None and q.bid and q.ask:
                    tick = 0.05
                    check(f"price_inside_quote:{leg.instrument_key}", q.bid - tick <= leg.limit_price <= q.ask + tick,
                          f"limit {leg.limit_price} vs bid {q.bid} / ask {q.ask}")
            opens = [leg for leg in act.legs if not leg.reduce_only]
            if opens and act.kind in ("ENTRY", "ROLL"):
                shorts = [leg for leg in opens if leg.side == "SELL"]
                longs = [leg for leg in opens if leg.side == "BUY"]
                hedged = all(any(lg.option_type == s.option_type for lg in longs) for s in shorts)
                if not (ctx.policy or {}).get("allow_naked_short", False):
                    check("short_legs_hedged", hedged, "policy forbids naked short options")
                credit = sum((leg.limit_price or 0) * (1 if leg.side == "SELL" else -1) for leg in opens)
                if act.metrics.get("net_credit_per_unit") is not None:
                    check("net_credit_recomputed", _close(round(credit, 2), act.metrics["net_credit_per_unit"], 1e-6), f"{credit:.2f}")
        failed = [c for c in checks if not c["passed"]]
        crit = [c for c in failed if c["critical"]]
        stance = Stance.BLOCK if crit else Stance.NEUTRAL
        return {"status": "OK" if checks else "INSUFFICIENT_DATA", "stance": stance if checks else Stance.INSUFFICIENT_DATA,
                "confidence": 1.0 if checks else 0.0, "confidence_method": "deterministic re-computation and cross-checks",
                "observations": {"checks": checks, "failed": len(failed), "critical_failed": len(crit)},
                "warnings": [f"verification failed: {c['check']} ({c['detail']})" for c in failed]}
