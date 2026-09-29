"""The Council: orchestrates every agent, forms a weighted consensus and acts
according to the terminal mode.

AUTO   -> qualifying plans are deployed immediately (still through RiskManager).
MANUAL -> plans are queued for human approval; only protective actions run.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List

from terminal.agents.base import Agent, MarketContext
from terminal.agents.llm import LLMGateway
from terminal.agents.specialists import (
    EventRiskAgent,
    ExecutionAgent,
    LearnedModelAgent,
    MarketAnalystAgent,
    OptionsFlowAgent,
    ReviewAgent,
    RiskAgent,
    SentinelAgent,
    StrategySelectorAgent,
    VolatilityAgent,
)
from terminal.core.models import Assessment, CouncilDecision, OrderSource, TerminalMode, TradePlan

log = logging.getLogger("terminal.council")


class Council:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.agents: List[Agent] = [MarketAnalystAgent(terminal), VolatilityAgent(terminal), OptionsFlowAgent(terminal), EventRiskAgent(terminal), SentinelAgent(terminal),
                                    RiskAgent(terminal), LearnedModelAgent(terminal), StrategySelectorAgent(terminal), ExecutionAgent(terminal), ReviewAgent(terminal)]
        self.llm = LLMGateway(terminal.settings)
        self.decisions: List[CouncilDecision] = []
        self.last_brief: Dict[str, str] = {}
        self.cycle = 0
        self.last_cycle_ts = 0.0
        self.focus: List[str] = [u.symbol for u in terminal.universe.all() if u.symbol in ("NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL")] or [u.symbol for u in terminal.universe.all()][:3]
        self.auto_trades_today = 0
        self._day = time.strftime("%Y-%m-%d")
        self._proposal_cooldown: Dict[str, float] = {}
        self.busy = False
        saved = terminal.db.get_setting("agent_config")
        if isinstance(saved, dict):
            for a in self.agents:
                cfg = saved.get(a.name)
                if isinstance(cfg, dict):
                    a.enabled = bool(cfg.get("enabled", True))
                    a.weight = float(cfg.get("weight", a.weight))
            if isinstance(saved.get("focus"), list):
                self.focus = [s for s in saved["focus"] if terminal.universe.has(s)] or self.focus

    # ------------------------------------------------------------- context
    def build_context(self, underlying: str) -> MarketContext:
        t = self.t
        u = t.universe.get(underlying)
        spot = t.processor.last_price(underlying) or u.base_spot
        chain = t.chains.get(underlying)
        ok, why = t.scheduler.can_enter(u)
        expiry = chain.expiry if chain else t.chain_builder.nearest_expiry(u)
        vix = t.processor.last_price("INDIAVIX")
        return MarketContext(underlying=underlying, exchange=u.exchange.value, spot=spot, vix=vix, vix_rank=t.processor.vix_rank(), chain=chain,
                             indicators=t.processor.indicators(underlying), pcr_history=list(t.processor.pcr_history[underlying])[-20:], risk=t.risk.snapshot,
                             active_runs=t.strategies.active_runs(), mode=t.mode, session=t.scheduler.session(u), can_enter=ok, can_enter_reason=why,
                             is_expiry_day=t.scheduler.is_expiry_day(expiry), days_to_expiry=chain.days_to_expiry if chain else 0.0,
                             sigma_move_5m=t.processor.sigma_move(underlying, t.settings.black_swan_window_minutes * 60, u.base_vol),
                             feed_fresh=t.feed.is_fresh(t.settings.feed_stale_seconds), memory={"strategy_stats": t.db.memory_get("strategy_stats", {}) or {}},
                             events=t.db.get_setting("events", []) or [])

    # ------------------------------------------------------------- cycle
    async def run_cycle(self, underlyings: List[str] | None = None, force: bool = False) -> List[CouncilDecision]:
        if self.busy and not force:
            return []
        self.busy = True
        try:
            day = time.strftime("%Y-%m-%d")
            if day != self._day:
                self._day, self.auto_trades_today = day, 0
            out = []
            for sym in (underlyings or self.focus):
                if not self.t.universe.has(sym):
                    continue
                try:
                    out.append(await self._deliberate(sym))
                except Exception:
                    log.exception("council cycle failed for %s", sym)
            self.cycle += 1
            self.last_cycle_ts = time.time()
            return out
        finally:
            self.busy = False

    async def _deliberate(self, underlying: str) -> CouncilDecision:
        ctx = self.build_context(underlying)
        active = [a for a in self.agents if a.enabled]
        # stage 1: independent analysts run concurrently
        stage1 = [a for a in active if a.name in ("MarketAnalyst", "VolatilityAgent", "OptionsFlow", "EventRisk", "Sentinel", "RiskGuardian", "PostTradeReviewer", "LearnedModel")]
        results = await asyncio.gather(*(a.run(ctx) for a in stage1))
        by_name = {r.agent: r for r in results}
        # share findings with the selector / execution agents
        ctx.memory["regime"] = by_name.get("MarketAnalyst", Assessment(agent="", role="", stance="RANGE", score=0, confidence=0)).data.get("regime", "RANGE")
        ctx.memory["vol_score"] = by_name.get("VolatilityAgent", Assessment(agent="", role="", stance="", score=0, confidence=0)).score
        ctx.memory["flow"] = by_name.get("OptionsFlow", Assessment(agent="", role="", stance="", score=0, confidence=0)).data
        stage2 = [a for a in active if a.name in ("StrategySelector", "ExecutionTactician")]
        results2 = await asyncio.gather(*(a.run(ctx) for a in stage2))
        assessments = list(results) + list(results2)
        votes = {a.agent: a.score for a in assessments}
        weights = {a.name: a.weight for a in active}
        voting = [a for a in assessments if a.agent not in ("PostTradeReviewer",) and a.stance not in ("ERROR", "WARMING_UP", "NO_CHAIN", "NO_MODEL", "NO_FEATURES")]
        wsum = sum(weights.get(a.agent, 1.0) * a.confidence for a in voting) or 1.0
        consensus = sum(weights.get(a.agent, 1.0) * a.confidence * a.score for a in voting) / wsum
        confidence = sum(a.confidence for a in voting) / len(voting) if voting else 0.0
        vetoes = [a for a in assessments if a.veto]
        sentinel = by_name.get("Sentinel")
        decision = "HOLD"
        plan: TradePlan | None = None
        summary_bits: List[str] = []
        # protective actions first (both modes)
        if sentinel and sentinel.stance == "PROTECT" and self.t.strategies.active_runs():
            decision = "PROTECT"
            summary_bits.append("Sentinel protective action: " + ", ".join(sentinel.data.get("protect", [])))
            if self.t.mode == TerminalMode.AUTO or self.t.settings.protect_in_manual:
                for run in self.t.strategies.active_runs():
                    await self.t.strategies.exit_run(run.id, "sentinel", "SENTINEL_PROTECT", source=OrderSource.SENTINEL)
            else:
                await self.t.alerts.emit("CRITICAL", "sentinel", f"Protective exit recommended on {underlying}", "; ".join(sentinel.findings))
        elif vetoes:
            decision = "VETO"
            summary_bits.append("Vetoed by " + ", ".join(v.agent for v in vetoes))
        elif not ctx.can_enter:
            decision = "AVOID"
            summary_bits.append(f"No entries: {ctx.can_enter_reason}")
        elif self.t.strategies.active_runs(underlying):
            decision = "HOLD"
            summary_bits.append(f"Managing {len(self.t.strategies.active_runs(underlying))} active run(s)")
        elif consensus >= self.t.settings.auto_min_consensus and confidence >= self.t.settings.auto_min_confidence:
            sel = by_name.get("StrategySelector") or next((a for a in results2 if a.agent == "StrategySelector"), None)
            key = sel.data.get("strategy") if sel else None
            lots = int(sel.data.get("lots", 1)) if sel else 1
            cooldown_ok = time.time() - self._proposal_cooldown.get(underlying, 0) > 300
            if key and cooldown_ok:
                rationale = [f"{a.agent}: {a.findings[0]}" for a in assessments if a.findings]
                try:
                    plan = self.t.strategies.make_plan(key, underlying, lots, rationale=rationale, confidence=round(confidence, 3), consensus=round(consensus, 3), votes=votes,
                                                       source=OrderSource.AUTO)
                except Exception as exc:
                    plan = None
                    summary_bits.append(f"plan build failed: {exc}")
                if plan:
                    check = self.t.risk.check_plan(plan)
                    if not check["allowed"]:
                        plan.status = "RISK_REJECTED"
                        decision = "AVOID"
                        summary_bits.append("Risk pre-check rejected plan: " + ", ".join(check["reasons"]))
                        self.t.db.save_plan(plan.model_dump(mode="json"))
                    elif self.t.mode == TerminalMode.AUTO:
                        if self.auto_trades_today >= self.t.settings.auto_max_trades_per_day:
                            decision = "AVOID"
                            summary_bits.append("AUTO daily trade cap reached")
                        else:
                            try:
                                run = await self.t.strategies.deploy(plan, actor="council-auto", source=OrderSource.AUTO)
                                run.notes.insert(0, f"regime={ctx.memory['regime']} consensus={consensus:.2f}")
                                self.auto_trades_today += 1
                                decision = "ENTER"
                                summary_bits.append(f"AUTO deployed {key} x{lots} (consensus {consensus:.2f})")
                            except Exception as exc:
                                decision = "AVOID"
                                summary_bits.append(f"deploy failed: {exc}")
                            self._proposal_cooldown[underlying] = time.time()
                    else:
                        decision = "PROPOSE"
                        plan.status = "PROPOSED"
                        self.t.db.save_plan(plan.model_dump(mode="json"))
                        self._proposal_cooldown[underlying] = time.time()
                        summary_bits.append(f"Proposed {key} x{lots} for operator approval (consensus {consensus:.2f})")
                        await self.t.alerts.emit("INFO", "council", f"Trade proposal: {key} on {underlying}", f"{lots} lot(s), credit ₹{plan.premium_collected * lots:,.0f}. Approve in the Approvals panel.", market=ctx.exchange)
                        await self.t.bus.publish("plan.proposed", plan)
            elif not cooldown_ok:
                summary_bits.append("Favourable but in proposal cool-down")
        else:
            decision = "HOLD"
            summary_bits.append(f"Consensus {consensus:.2f} below threshold {self.t.settings.auto_min_consensus}")
        summary = f"{underlying}: {decision}. " + " ".join(summary_bits) + f" Regime {ctx.memory['regime']}, consensus {consensus:+.2f}, confidence {confidence:.2f}."
        cd = CouncilDecision(underlying=underlying, mode=self.t.mode, consensus=round(consensus, 3), confidence=round(confidence, 3), decision=decision, assessments=assessments,
                             plan_id=plan.id if plan else None, summary=summary)
        self.decisions.append(cd)
        self.decisions = self.decisions[-200:]
        if getattr(self.t, "evaluator", None) is not None:
            self.t.evaluator.record(cd, ctx, assessments)
        # persist every actionable decision, and a periodic HOLD sample to keep the DB lean
        if decision != "HOLD" or self.cycle % 30 == 0:
            self.t.db.save_council(cd.model_dump(mode="json"))
            self.t.log("INFO", "council", summary)
        else:
            log.debug("[council] %s", summary)
        await self.t.bus.publish("council.decision", cd)
        if decision in ("ENTER", "PROPOSE", "PROTECT", "VETO") or self.cycle % 6 == 0:
            brief = await self.llm.brief({"underlying": underlying, "decision": decision, "consensus": consensus, "mode": self.t.mode.value,
                                          "assessments": [a.model_dump(mode="json") for a in assessments], "plan": plan.model_dump(mode="json") if plan else None})
            if brief:
                self.last_brief[underlying] = brief
                await self.t.bus.publish("council.brief", {"underlying": underlying, "brief": brief})
        return cd

    # ------------------------------------------------------------- approvals
    def pending_plans(self) -> List[TradePlan]:
        return [p for p in self.t.strategies.plans.values() if p.status == "PROPOSED"]

    async def approve_plan(self, plan_id: str, actor: str, lots: int | None = None) -> str:
        plan = self.t.strategies.plans.get(plan_id)
        if plan is None or plan.status != "PROPOSED":
            raise ValueError("PLAN_NOT_PENDING")
        if time.time() - plan.created_at > 900:
            plan.status = "EXPIRED"
            self.t.db.save_plan(plan.model_dump(mode="json"))
            raise ValueError("PLAN_EXPIRED: re-run the council for fresh strikes")
        if lots and lots != plan.lots:
            plan = self.t.strategies.make_plan(plan.strategy, plan.underlying, lots, rationale=plan.rationale, confidence=plan.confidence, consensus=plan.consensus, votes=plan.agent_votes, source=OrderSource.MANUAL)
        else:
            # refresh prices at approval time so entry reflects current market
            fresh = self.t.strategies.make_plan(plan.strategy, plan.underlying, plan.lots, rationale=plan.rationale, confidence=plan.confidence, consensus=plan.consensus, votes=plan.agent_votes, source=OrderSource.MANUAL)
            self.t.strategies.plans[plan_id].status = "APPROVED"
            self.t.db.save_plan(self.t.strategies.plans[plan_id].model_dump(mode="json"))
            plan = fresh
        self.t.audit.record("PLAN_APPROVED", {"plan": plan_id, "lots": plan.lots}, actor)
        run = await self.t.strategies.deploy(plan, actor=actor, source=OrderSource.MANUAL)
        return run.id

    def reject_plan(self, plan_id: str, actor: str, reason: str = "") -> None:
        plan = self.t.strategies.plans.get(plan_id)
        if plan is None or plan.status != "PROPOSED":
            raise ValueError("PLAN_NOT_PENDING")
        plan.status = "REJECTED"
        self.t.db.save_plan(plan.model_dump(mode="json"))
        self.t.audit.record("PLAN_REJECTED", {"plan": plan_id, "reason": reason}, actor)

    # ------------------------------------------------------------- config
    def update_agent(self, name: str, patch: dict, actor: str) -> dict:
        agent = next((a for a in self.agents if a.name == name), None)
        if agent is None:
            raise ValueError("UNKNOWN_AGENT")
        if "enabled" in patch:
            agent.enabled = bool(patch["enabled"])
        if "weight" in patch:
            agent.weight = max(0.0, min(5.0, float(patch["weight"])))
        self._save()
        self.t.audit.record("AGENT_CONFIG_UPDATED", {"agent": name, "patch": patch}, actor)
        return agent.status()

    def set_focus(self, symbols: List[str], actor: str) -> List[str]:
        self.focus = [s.upper() for s in symbols if self.t.universe.has(s)]
        self._save()
        self.t.audit.record("COUNCIL_FOCUS_UPDATED", {"focus": self.focus}, actor)
        return self.focus

    def _save(self) -> None:
        self.t.db.set_setting("agent_config", {**{a.name: {"enabled": a.enabled, "weight": a.weight} for a in self.agents}, "focus": self.focus})

    def describe(self) -> dict:
        return {"agents": [a.status() for a in self.agents], "focus": self.focus, "cycle": self.cycle, "last_cycle_ts": self.last_cycle_ts, "busy": self.busy,
                "auto_trades_today": self.auto_trades_today, "llm": self.llm.status(), "briefs": self.last_brief,
                "decisions": [d.model_dump(mode="json") for d in self.decisions[-20:][::-1]], "pending_plans": [p.model_dump(mode="json") for p in self.pending_plans()]}
