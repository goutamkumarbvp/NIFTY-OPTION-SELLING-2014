"""Decision cycle: build the agent context, run the Master, persist the package, route it by mode.

Routing (never bypasses the Risk Kernel):
* PAPER      — REQUIRES APPROVAL packages go to the approval queue for the PAPER account; with
               paper auto-approve on they are accepted automatically and recorded as paper-auto.
* MANUAL     — REQUIRES APPROVAL packages go to the owner's approval queue for the live account.
* AUTOMATIC  — REQUIRES APPROVAL packages go to the Automatic controller, which executes only
               inside the ACTIVE automation policy, otherwise falls back (NO ACTION / approval queue).
* AI actions suspended — packages are stored as advisory; nothing is routed.
"""
from __future__ import annotations

import asyncio
import logging

from amrt.agents.base import AgentContext
from amrt.core.enums import DecisionStatus, HealthState, Mode
from amrt.marketdata.instruments import spec
from amrt.quant.strategies import REGISTRY
from amrt.security.identity import Principal, PrincipalKind
from amrt.storage.db import decisions

log = logging.getLogger("amrt.decisions")


class DecisionService:
    def __init__(self, app) -> None:
        self.app = app
        self.latest: dict[str, dict] = {}
        self.narratives: dict[str, dict] = {}
        self.enabled_strategies = [REGISTRY[("ROLLING_ATM_IRON_FLY", "1.0")], REGISTRY[("MCX_ATM_IRON_FLY", "1.0")]]

    def primary_account(self):
        a = self.app
        if a.mode_ctl.mode == Mode.PAPER:
            return a.accounts.paper()[0]
        live = [x for x in a.accounts.live() if x.enabled]
        return live[0] if live else None

    def context(self, account, underlying: str) -> AgentContext:
        a = self.app
        pol, meta = a.builder.policy_for(account.account_id)
        max_age = pol.max_data_age_ms if pol else 3000
        snap = a.hub.chain(underlying)
        val = a.ledger.valuation(account.account_id, lambda k: a.builder.mark(k, max_age), lambda k: a.builder.spot_for(k, max_age))
        sp = spec(underlying)
        funds = dict(account.funds)
        sod = funds.get("sod_funds")
        policy_label = (f"v{meta['version']} ACTIVE" if meta else ("DEFAULT PAPER POLICY" if pol else "NONE"))
        return AgentContext(
            now_ts=a.clock.ts(), now_ist=a.clock.ist(), mode=a.mode_ctl.mode.value, account_id=account.account_id, account_kind=account.kind,
            underlying=underlying, exchange=sp.exchange.value,
            lot_size=(a.instruments.get(snap.rows[0].ce.instrument_key).lot_size if snap and snap.rows and snap.rows[0].ce else sp.lot_size_on(a.clock.ist().date())),
            chain=snap, chain_age_ms=None if snap is None else (a.clock.ts() - snap.ts) * 1000.0, chain_label=a.hub.chain_label(snap, max_age).value,
            analytics=a.feed.analytics.get(underlying) if a.feed else None, windows=a.feed.windows.get(underlying, {}) if a.feed else {},
            spot_history=tuple(a.feed.spot_history.get(underlying, ())) if a.feed else (),
            positions=tuple(p.to_dict() for p in a.ledger.open_positions(account.account_id)), valuation=val,
            funds={"sod_funds": sod, "margin_used": funds.get("margin_used"), "available": funds.get("available")},
            policy=pol.model_dump(mode="json") if pol else None, policy_label=policy_label,
            safety={**a.safety.snapshot(), "blockers": a.safety.new_risk_blockers()}, health=tuple(a.health.describe()),
            brokers={s["broker"]: s for s in a.brokers.status()} if a.brokers else {}, reconcile=self._reconcile(account.account_id),
            fii_participant=tuple(a.fiidii.participant(30)), fii_cash=tuple(a.fiidii.cash(30)), news=(), news_source_configured=False,
            events_calendar=tuple(a.events_calendar), security=a.security_snapshot(), strategies=tuple(self.enabled_strategies),
            backtests=a.validated_backtests(), max_data_age_ms=max_age)

    def _reconcile(self, account_id: str) -> dict:
        st = self.app.reconciler.account_status(account_id)
        last = st.get("last_ok_ts")
        return {**st, "age_s": None if last is None else round(self.app.clock.ts() - last, 1)}

    async def cycle(self) -> list[dict]:
        a = self.app
        account = self.primary_account()
        if account is None:
            a.health.beat("master_agent", HealthState.DEGRADED, reason="no account for the current mode")
            return []
        out = []
        for und in a.settings.underlying_list:
            try:
                ctx = self.context(account, und)
                pkg = await asyncio.to_thread(a.master.decide, ctx)
            except Exception as e:  # noqa: BLE001
                log.exception("decision cycle failed for %s", und)
                a.health.fail("master_agent", f"{type(e).__name__}: {e}")
                continue
            routed = await self.route(pkg)
            self.persist(pkg, routed)
            self.latest[und] = {**pkg, "routing": routed}
            out.append(self.latest[und])
            if a.narrative.enabled and pkg["status"] != DecisionStatus.INSUFFICIENT_DATA.value:
                asyncio.create_task(self._narrate(pkg))
        a.health.beat("master_agent", HealthState.HEALTHY, reason=f"{len(out)} package(s)")
        return out

    async def _narrate(self, pkg: dict) -> None:
        res = await self.app.narrative.write(pkg)
        self.narratives[pkg["decision_id"]] = res
        self.app.events.append("DECISION_NARRATIVE", {"decision_id": pkg["decision_id"], **res}, self.app.master.principal, correlation_id=pkg["decision_id"])

    def persist(self, pkg: dict, routed: dict | None) -> None:
        a = self.app
        stored = {**pkg, "routing": routed}
        with a.db.tx() as conn:
            conn.execute(decisions.insert().values(decision_id=pkg["decision_id"], ts=pkg["created_at"], underlying=pkg["underlying"], status=pkg["status"],
                                                   package=stored))
        a.events.append("DECISION_PACKAGE", {k: v for k, v in stored.items() if k != "outputs"}, a.master.principal, correlation_id=pkg["decision_id"])

    async def route(self, pkg: dict) -> dict | None:
        a = self.app
        if pkg["status"] != DecisionStatus.REQUIRES_APPROVAL.value:
            return None
        if a.safety.state["ai_suspended"].get("active"):
            return {"routed": False, "reason": "AI-driven actions suspended"}
        mode = a.mode_ctl.mode
        acct = pkg["account_id"]
        if mode == Mode.AUTOMATIC:
            return {"routed": True, "to": "AUTOMATIC", **(await a.automatic.handle(pkg, acct))}
        ap = a.approvals.create(pkg, acct)
        if mode == Mode.PAPER and a.mode_ctl.paper_auto_approve and a.accounts.get(acct).kind == "PAPER":
            owner = Principal.of(PrincipalKind.OWNER, a.mode_ctl.paper_auto_by or "owner")
            res = await a.approvals.accept(ap["approval_id"], owner, step_up_at=None, auto=True)
            return {"routed": True, "to": "PAPER_AUTO_APPROVE", "approval": ap, "results": res}
        return {"routed": True, "to": "APPROVAL_QUEUE", "approval": ap}

