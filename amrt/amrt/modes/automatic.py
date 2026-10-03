"""Automatic Mode controller: executes only what the owner pre-authorized.

A decision package is executed automatically only if the account is in an
ACTIVE, unexpired automation policy, the strategy/version, underlying, order
type, lots and trading window are all inside it, AI actions are not suspended,
and the Master Agent is healthy. Everything else follows the policy fallback
(NO ACTION, or route to the owner's approval queue). The Risk Kernel still
evaluates every order and its rejection is final.
"""
from __future__ import annotations

from amrt.core.enums import AuthorizationKind, DecisionStatus, Mode, OrderPurpose, OrderType, Origin, Side
from amrt.execution.intents import AuthorizationContext
from amrt.risk.kernel import _in_window


class AutomaticController:
    def __init__(self, pipeline, policies, safety, health, mode_ctl, approvals, accounts, events, clock) -> None:
        self.pipeline, self.policies, self.safety, self.health, self.mode_ctl = pipeline, policies, safety, health, mode_ctl
        self.approvals, self.accounts, self.events, self.clock = approvals, accounts, events, clock
        self.executed = 0
        self.declined = 0

    def check(self, package: dict, account_id: str) -> tuple[bool, list[str], dict | None]:
        reasons = []
        if self.mode_ctl.mode != Mode.AUTOMATIC:
            reasons.append("not in AUTOMATIC mode")
        if self.safety.state["ai_suspended"].get("active"):
            reasons.append("AI-driven actions suspended")
        if self.health.state("master_agent").value not in ("HEALTHY", "DEGRADED"):
            reasons.append("Master Agent unhealthy")
        auto = self.policies.active_automation(account_id)
        if auto is None:
            return False, reasons + ["no ACTIVE automation policy"], None
        pol, meta = auto
        act = package.get("proposed_action") or {}
        if pol.valid_until < self.clock.ts():
            reasons.append("automation policy expired")
        strat = next((s for s in pol.strategies if s.strategy_id == act.get("strategy_id") and s.version == act.get("strategy_version")), None)
        if strat is None:
            reasons.append("strategy/version not pre-authorized")
        if act.get("underlying") not in pol.underlyings:
            reasons.append("underlying not pre-authorized")
        acct = self.accounts.get(account_id)
        ex = (package.get("instrument") or {}).get("exchange")
        if not _in_window(self.clock.ts(), pol.trading_windows.get(ex)):
            reasons.append("outside the automation trading window")
        for leg in act.get("legs", []):
            if OrderType(leg.get("order_type", "LIMIT")) not in pol.order_types:
                reasons.append("order type not pre-authorized")
            if strat is not None and leg["lots"] > min(strat.max_lots, pol.max_lots_per_order):
                reasons.append("lots exceed the automation ceiling")
        if pol.entries_require_owner:
            reasons.append("policy requires owner approval for entries")
        if acct.kind != "LIVE" or pol.broker != acct.broker:
            reasons.append("automation applies to the policy's live account only")
        return not reasons, sorted(set(reasons)), meta

    async def handle(self, package: dict, account_id: str) -> dict:
        ok, reasons, meta = self.check(package, account_id)
        if not ok:
            self.declined += 1
            auto = self.policies.active_automation(account_id)
            fallback = auto[0].fallback if auto else "NO_ACTION"
            self.events.append("AUTOMATION_DECLINED", {"decision_id": package["decision_id"], "reasons": reasons, "fallback": fallback},
                               self.pipeline.principal, correlation_id=package["decision_id"])
            if fallback == "REQUIRE_APPROVAL" and self.mode_ctl.mode != Mode.PAPER:
                ap = self.approvals.create(package, account_id)
                return {"status": DecisionStatus.REQUIRES_APPROVAL.value, "approval": ap, "reasons": reasons}
            return {"status": DecisionStatus.NO_ACTION.value, "reasons": reasons}
        acct = self.accounts.get(account_id)
        auth = AuthorizationContext(kind=AuthorizationKind.AUTOMATION_POLICY, approved_by=f"automation:{meta['policy_id']}", approval_id=meta["policy_id"],
                                    policy_version=meta["version"], approved_at=self.clock.ts())
        act = package["proposed_action"]
        results = []
        for i, leg in enumerate(sorted(act["legs"], key=lambda leg_: 0 if leg_["side"] == "BUY" else 1)):
            otype = OrderType(leg.get("order_type", "LIMIT"))
            intent = self.pipeline.make_intent(account_id=account_id, broker=acct.broker, mode=Mode.AUTOMATIC, instrument_key=leg["instrument_key"],
                                               side=Side(leg["side"]), lots=leg["lots"], order_type=otype, limit_price=leg.get("limit_price") if otype == OrderType.LIMIT else None,
                                               purpose=OrderPurpose(leg.get("purpose", "ENTRY")), reduce_only=bool(leg.get("reduce_only", False)),
                                               origin=Origin.AUTOMATION, authorization=auth, correlation_id=package["decision_id"], decision_id=package["decision_id"],
                                               strategy_id=act.get("strategy_id"), strategy_version=act.get("strategy_version"), idem_parts=("auto", i))
            res = await self.pipeline.submit(intent)
            results.append(res)
            if res["order"].get("state") in ("REJECTED_PRE_TRADE", "REJECTED", "ORDER STATE UNKNOWN"):
                break
        approved = all(r["decision"]["approved"] for r in results)
        self.executed += 1
        status = DecisionStatus.AUTHORIZED if approved else DecisionStatus.REJECTED
        return {"status": status.value, "results": results}
