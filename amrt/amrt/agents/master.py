"""Master AI Agent: coordinates specialists and produces a structured decision package.

The Master never authorizes anything. Its statuses are ADVISORY, REQUIRES
APPROVAL, NO ACTION, REJECTED and INSUFFICIENT DATA; AUTHORIZED is set only by
the Automatic Mode controller after the Risk Kernel approves every order.

Aggregation is deterministic and documented (docs/AGENTS.md):
1. A critical agent that failed, timed out, was quarantined or had no data
   → INSUFFICIENT DATA. A LIVE account also needs LIVE DATA VERIFIED.
2. Independent verification failures → REJECTED (proposal) / NO ACTION.
3. Risk-reducing proposals (all legs reduce-only) skip stance gating.
4. New-risk proposals: any BLOCK → REJECTED (a ROLL degrades to its exit legs);
   CAUTION outnumbering SUPPORTIVE among context agents → NO ACTION (recorded
   as a disagreement).
5. A non-binding Risk Kernel pre-check of every leg; any failure → REJECTED.
6. Otherwise REQUIRES APPROVAL — the owner or a pre-approved automation policy decides.
"""
from __future__ import annotations

import concurrent.futures as cf
from collections.abc import Callable
from typing import Any

from amrt.agents.base import Agent, AgentContext, AgentOutput, synthetic
from amrt.core.enums import DataLabel, DecisionStatus, HealthState, Stance
from amrt.core.ids import digest, new_id
from amrt.security.identity import Capability, Principal, PrincipalKind, require

MASTER_VERSION = "master/1.0"
CONTEXT_AGENTS = ("market_intelligence", "option_chain_intelligence", "pcr_positioning", "fii_dii", "portfolio_exposure", "news_events")
PAPER_LABELS = {DataLabel.LIVE_VERIFIED.value, DataLabel.LIVE_UNVERIFIED.value, DataLabel.HISTORICAL_REPLAY.value, DataLabel.SIMULATED.value}


class MasterAgent:
    def __init__(self, specialists: list[Agent], strategy: Agent, verifier: Agent, health=None,
                 precheck: Callable[[str, dict], dict] | None = None, timeout_s: float = 2.0, package_ttl_s: float = 120.0) -> None:
        self.principal = Principal.of(PrincipalKind.MASTER_AGENT, "agent:master")
        self.specialists, self.strategy, self.verifier = list(specialists), strategy, verifier
        self.health, self.precheck, self.timeout_s, self.package_ttl_s = health, precheck, timeout_s, package_ttl_s
        self._pool = cf.ThreadPoolExecutor(max_workers=max(4, len(self.specialists) + 1), thread_name_prefix="agent")
        if health is not None:
            for a in self.all_agents():
                health.register(f"agent:{a.name}", critical=a.critical, period_s=60.0, kind="agent", restartable=True)

    def all_agents(self) -> list[Agent]:
        return [*self.specialists, self.strategy, self.verifier]

    def _quarantined(self, a: Agent) -> bool:
        c = self.health.components.get(f"agent:{a.name}") if self.health is not None else None
        return bool(c and c.quarantined)

    def _record(self, out: AgentOutput) -> None:
        if self.health is None:
            return
        name = f"agent:{out.agent}"
        if out.status in ("ERROR", "TIMEOUT"):
            self.health.fail(name, "; ".join(out.errors) or out.status)
        elif out.status != "QUARANTINED":
            self.health.beat(name, HealthState.HEALTHY if out.status == "OK" else HealthState.DEGRADED, reason=out.status, latency_ms=out.latency_ms)

    def run_agents(self, ctx: AgentContext) -> dict[str, AgentOutput]:
        outs: dict[str, AgentOutput] = {}
        futs = {}
        for a in [*self.specialists, self.strategy]:
            if self._quarantined(a):
                outs[a.name] = synthetic(a, ctx, "QUARANTINED", "agent is quarantined by the Reliability Control Plane")
            else:
                futs[a.name] = (a, self._pool.submit(a.run, ctx, None))
        for name, (a, fut) in futs.items():
            try:
                outs[name] = fut.result(timeout=self.timeout_s)
            except cf.TimeoutError:
                outs[name] = synthetic(a, ctx, "TIMEOUT", f"no output within {self.timeout_s}s")
        if self._quarantined(self.verifier):
            outs[self.verifier.name] = synthetic(self.verifier, ctx, "QUARANTINED", "verifier quarantined")
        else:
            fut = self._pool.submit(self.verifier.run, ctx, dict(outs))
            try:
                outs[self.verifier.name] = fut.result(timeout=self.timeout_s)
            except cf.TimeoutError:
                outs[self.verifier.name] = synthetic(self.verifier, ctx, "TIMEOUT", f"no output within {self.timeout_s}s")
        for o in outs.values():
            self._record(o)
        return outs

    def decide(self, ctx: AgentContext) -> dict[str, Any]:
        require(self.principal, Capability.PROPOSE_DECISION, "master decision")
        outs = self.run_agents(ctx)
        agents = {a.name: a for a in self.all_agents()}
        reasons: list[str] = []
        status: DecisionStatus

        missing = [n for n, o in outs.items() if agents[n].critical and o.status in ("ERROR", "TIMEOUT", "QUARANTINED", "INSUFFICIENT_DATA")]
        label_ok = ctx.chain_label == DataLabel.LIVE_VERIFIED.value if ctx.account_kind == "LIVE" else ctx.chain_label in PAPER_LABELS
        stances = {n: o.stance for n, o in outs.items()}
        blockers = sorted(n for n, s in stances.items() if s == Stance.BLOCK)
        sup = sorted(n for n in CONTEXT_AGENTS if stances.get(n) == Stance.SUPPORTIVE)
        cau = sorted(n for n in CONTEXT_AGENTS if stances.get(n) == Stance.CAUTION)
        neu = sorted(n for n in CONTEXT_AGENTS if stances.get(n) == Stance.NEUTRAL)
        disagreements = []
        if sup and cau:
            disagreements.append({"supportive": sup, "caution": cau, "resolution": "caution prevails when it outnumbers or equals support for new risk"})
        strat = outs.get(self.strategy.name)
        action = strat.proposed_action.model_dump() if strat is not None and strat.proposed_action is not None else None
        verification = outs.get(self.verifier.name)
        ver_failed = [c for c in (verification.observations.get("checks", []) if verification else []) if not c["passed"] and c["critical"]]
        precheck = None

        if missing or not label_ok:
            status = DecisionStatus.INSUFFICIENT_DATA
            if missing:
                reasons.append("critical agent(s) without usable output: " + ", ".join(missing))
            if not label_ok:
                reasons.append(f"market data label {ctx.chain_label} is not acceptable for a {ctx.account_kind} account")
        elif action is None:
            status = DecisionStatus.NO_ACTION if blockers else DecisionStatus.ADVISORY
            reasons.append("no action proposed" + (f"; blocking stances: {', '.join(blockers)}" if blockers else ""))
        else:
            reduce_only = all(leg["reduce_only"] for leg in action["legs"])
            if ver_failed:
                status = DecisionStatus.REJECTED
                reasons += [f"verification failed: {c['check']}" for c in ver_failed]
            elif not reduce_only and blockers:
                if action["kind"] == "ROLL":
                    exits = [leg for leg in action["legs"] if leg["reduce_only"]]
                    action = {**action, "kind": "EXIT", "legs": exits, "metrics": {},
                              "rationale": action["rationale"] + ["new legs dropped: blocking stance(s) " + ", ".join(blockers)]}
                    reduce_only = True
                    reasons.append("roll reduced to exit legs because of blocking stances")
                else:
                    status = DecisionStatus.REJECTED
                    reasons.append("blocking stance(s): " + ", ".join(blockers))
            if not reduce_only and not blockers and not ver_failed and len(cau) >= max(1, len(sup)):
                status = DecisionStatus.NO_ACTION
                reasons.append(f"specialists disagree or advise caution ({len(cau)} caution vs {len(sup)} supportive)")
            elif not ver_failed and (reduce_only or not blockers):
                precheck = self.precheck(ctx.account_id, action) if self.precheck else {"approved": None, "note": "pre-check unavailable"}
                if precheck.get("approved") is False:
                    status = DecisionStatus.REJECTED
                    reasons.append("Risk Kernel pre-check rejected: " + "; ".join(precheck.get("failed", []))[:500])
                else:
                    status = DecisionStatus.REQUIRES_APPROVAL
                    reasons.append("proposal passed verification and the non-binding Risk Kernel pre-check")

        considered = [o for n, o in outs.items() if n in CONTEXT_AGENTS and o.status in ("OK", "DEGRADED")]
        mean_conf = sum(o.confidence for o in considered) / len(considered) if considered else 0.0
        agreement = 1.0 - (min(len(sup), len(cau)) / max(1, len(sup) + len(cau)))
        quality = 1.0 if ctx.chain_label == DataLabel.LIVE_VERIFIED.value else 0.5
        confidence = round(mean_conf * agreement * quality, 3)
        market = outs.get("market_intelligence")
        package = {
            "decision_id": new_id("DP"), "master_version": MASTER_VERSION, "created_at": ctx.now_ts, "expires_at": ctx.now_ts + self.package_ttl_s,
            "mode": ctx.mode, "account_id": ctx.account_id, "account_kind": ctx.account_kind, "underlying": ctx.underlying,
            "instrument": {"exchange": ctx.exchange, "underlying": ctx.underlying, "expiry": ctx.chain.expiry.isoformat() if ctx.chain is not None else None,
                           "lot_size": ctx.lot_size},
            "market_regime": (market.observations.get("regime") if market else None) or "UNKNOWN",
            "data": {"label": ctx.chain_label, "source": getattr(ctx.chain, "source", None), "as_of": getattr(ctx.chain, "ts", None),
                     "age_ms": ctx.chain_age_ms, "chain_status": getattr(ctx.analytics, "status", "DATA UNAVAILABLE")},
            "specialists": [{"agent": o.agent, "version": o.version, "status": o.status, "stance": o.stance.value, "confidence": o.confidence,
                             "confidence_method": o.confidence_method, "warnings": o.warnings, "inferences": o.inferences, "latency_ms": o.latency_ms,
                             "errors": o.errors} for o in outs.values()],
            "agreement": {"supportive": sup, "neutral": neu, "caution": cau, "blocking": blockers, "disagreements": disagreements},
            "verification": {"passed": not ver_failed, "checks": verification.observations.get("checks", []) if verification else []},
            "proposed_action": action, "risk_precheck": precheck,
            "rationale": (action or {}).get("rationale", []) + reasons,
            "assumptions": sorted({a for o in outs.values() for a in o.assumptions}),
            "risks": sorted({w for o in outs.values() for w in o.warnings}),
            "invalidation": sorted({i for o in outs.values() for i in o.invalidation}),
            "confidence": confidence,
            "confidence_method": "mean context-agent confidence × agreement (1 − min(support, caution)/(support + caution)) × data quality (1.0 verified, 0.5 otherwise); heuristic, not a probability",
            "required_approvals": (["OWNER_APPROVAL or ACTIVE AUTOMATION POLICY", "RISK KERNEL (every order)"] if status == DecisionStatus.REQUIRES_APPROVAL else []),
            "status": status.value, "reasons": reasons, "input_digest": ctx.digest(),
            "outputs": {n: o.model_dump(mode="json") for n, o in outs.items()},
        }
        package["package_digest"] = digest({k: v for k, v in package.items() if k != "outputs"})
        return package

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
