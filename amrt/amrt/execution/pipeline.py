"""Order pipeline: build the Risk Kernel context from live state, evaluate, hand to the gateway.

Used by the mode controllers (manual approvals, automation) and the protective
workflow — each with its own principal. Every decision is written to the audit
trail with all rule results before the gateway sees it.
"""
from __future__ import annotations

from amrt.core.enums import USABLE_HEALTH, DataLabel, HealthState, Mode
from amrt.core.ids import idempotency_key, new_id
from amrt.execution.intents import AuthorizationContext, OrderIntent
from amrt.marketdata.instruments import InstrumentMaster
from amrt.marketdata.models import Instrument
from amrt.risk.kernel import RiskContext
from amrt.risk.margin import estimate_option_margin
from amrt.risk.policy import default_paper_policy

INTENT_TTL = 60.0


class RiskContextBuilder:
    def __init__(self, *, clock, settings, mode_ctl, accounts, policies, safety, ledger, book, reconciler, health, hub, instruments) -> None:
        self.clock, self.settings, self.mode_ctl, self.accounts, self.policies = clock, settings, mode_ctl, accounts, policies
        self.safety, self.ledger, self.book, self.reconciler, self.health, self.hub, self.instruments = safety, ledger, book, reconciler, health, hub, instruments

    def policy_for(self, account_id: str):
        acct = self.accounts.get(account_id)
        pol = self.policies.active_risk(account_id)
        meta = self.policies.active_risk_meta(account_id)
        if pol is None and acct.kind == "PAPER":
            return default_paper_policy(account_id), None
        return pol, meta

    def mark(self, key: str, max_age_ms: float) -> float | None:
        f = self.hub.freshness(key, max_age_ms)
        if not f.fresh:
            return None
        q = self.hub.quotes[key]
        return q.mid or q.ltp

    def spot_for(self, key: str, max_age_ms: float) -> float | None:
        k = InstrumentMaster.parse_key(key)
        return self.mark(Instrument.underlying_key(k["exchange"], k["underlying"]), max_age_ms)

    def build(self, intent: OrderIntent) -> RiskContext:
        acct = self.accounts.get(intent.account_id)
        pol, meta = self.policy_for(intent.account_id)
        max_age = pol.max_data_age_ms if pol else 3000
        val = self.ledger.valuation(intent.account_id, lambda k: self.mark(k, max_age), lambda k: self.spot_for(k, max_age))
        pos = self.ledger.position(intent.account_id, intent.instrument_key)
        inst = self.instruments.get(intent.instrument_key)
        und_key = Instrument.underlying_key(inst.exchange, inst.underlying)
        fi = self.hub.freshness(intent.instrument_key, max_age)
        fu = self.hub.freshness(und_key, max_age)
        ref = self.mark(intent.instrument_key, max_age)
        spot = self.mark(und_key, max_age)
        working = [{"instrument_key": r["instrument_key"], "side": r["side"], "remaining": r["quantity"] - r["filled_qty"], "state": r["state"]}
                   for r in self.book.working(intent.account_id) if r["state"] != "ORDER STATE UNKNOWN"]
        unknown = [{"instrument_key": r["instrument_key"], "intent_id": r["intent_id"], "since": r["unknown_since"]} for r in self.book.unknown(intent.account_id)]
        src = self.hub.sources.get(acct.broker) or (self.hub.sources.get(self.settings.data_broker) if self.settings.data_broker else None)
        auto = self.policies.active_automation(intent.account_id)
        crit = {n: self.health.state(n) for n in ("gateway", "event_store", "portfolio_risk_monitor", "safety_monitor", "reconciler", "master_agent")}
        funds = dict(acct.funds)
        funds["age_s"] = (self.clock.ts() - funds["ts"]) if funds.get("ts") else None
        same = []
        for p in self.ledger.open_positions(intent.account_id):
            k = InstrumentMaster.parse_key(p.instrument_key)
            if k.get("underlying") == inst.underlying and "option_type" in k:
                same.append({"option_type": k["option_type"], "expiry": k["expiry"].isoformat(), "net_qty": p.net_qty})
        broker_state = self.health.state(f"broker:{intent.account_id}") if acct.kind == "LIVE" else HealthState.HEALTHY
        verified = self.instruments.loaded_from.get(intent.instrument_key, "").startswith(acct.broker) if acct.kind == "LIVE" else True
        return RiskContext(
            now_ts=self.clock.ts(), mode=self.mode_ctl.mode, live_capable=self.settings.live_capable, account_kind=acct.kind, account_enabled=acct.enabled,
            policy=pol, policy_meta=meta, hard=self.settings.hard_limits(), safety=self.safety.snapshot(), kill_file_engaged=self.safety.kill_file_engaged(),
            valuation=val, position_qty=pos.net_qty if pos else 0, inflight_reducing_qty=self.book.inflight_reducing_qty(intent.account_id, intent.instrument_key),
            working_orders=working, unknown_orders=unknown, reconcile=self.reconciler.account_status(intent.account_id) if self.reconciler else {},
            broker_health=broker_state, critical_health=crit, instrument_age_ms=fi.age_ms, instrument_label=fi.label if fi.fresh else DataLabel.UNAVAILABLE,
            underlying_age_ms=fu.age_ms, underlying_label=fu.label if fu.fresh else DataLabel.UNAVAILABLE, ref_price=ref, underlying_spot=spot,
            instrument_verified=verified, instrument_underlying=inst.underlying, instrument_exchange=inst.exchange.value,
            instrument_option_type=inst.option_type.value if inst.option_type else None, instrument_expiry=inst.expiry.isoformat() if inst.expiry else None,
            clock_drift_ms=src.drift_ms if src else None, orders_last_minute=self.book.orders_since(intent.account_id, self.clock.ts() - 60),
            funds=funds, est_margin_for_intent=estimate_option_margin(intent.side, intent.quantity, spot, ref) if not intent.reduce_only else 0.0,
            automation=auto[0] if auto else None, automation_meta=auto[1] if auto else None,
            master_healthy=crit.get("master_agent") in USABLE_HEALTH, same_underlying_positions=same,
            allow_simulated=bool(self.settings.simulated_market) and not self.settings.live_capable)


class OrderPipeline:
    def __init__(self, principal, kernel, gateway, builder: RiskContextBuilder, events, instruments, settings, clock) -> None:
        self.principal = principal
        self.kernel, self.gateway, self.builder, self.events, self.instruments, self.settings, self.clock = kernel, gateway, builder, events, instruments, settings, clock

    def make_intent(self, *, account_id: str, broker: str, mode: Mode, instrument_key: str, side, lots: int, order_type, limit_price, purpose, reduce_only: bool,
                    origin, authorization: AuthorizationContext, correlation_id: str, product="NRML", validity="DAY", decision_id=None,
                    strategy_id=None, strategy_version=None, note: str = "", idem_parts: tuple = ()) -> OrderIntent:
        inst = self.instruments.get(instrument_key)
        now = self.clock.ts()
        iid = new_id("OI")
        key = idempotency_key(account_id, instrument_key, str(side), lots, str(order_type), limit_price, str(purpose), correlation_id, *idem_parts) if idem_parts or decision_id \
            else idempotency_key(iid)
        return OrderIntent(intent_id=iid, created_at=now, mode=mode, environment=self.settings.environment, account_id=account_id, broker=broker,
                           instrument_key=instrument_key, trading_symbol=inst.trading_symbol, lot_size=inst.lot_size, side=side, lots=lots, order_type=order_type,
                           limit_price=limit_price, product=product, validity=validity, purpose=purpose, reduce_only=reduce_only, origin=origin,
                           authorization=authorization, decision_id=decision_id, strategy_id=strategy_id, strategy_version=strategy_version,
                           correlation_id=correlation_id, idempotency_key=key, expires_at=now + INTENT_TTL, note=note)

    def precheck_structure(self, account_id: str, action: dict) -> dict:
        """Non-binding kernel evaluation of every leg of a proposed structure, in execution order (hedges first).

        Later legs are evaluated as if earlier legs had filled (their positions are added to the context);
        authorization (RK-008) is not judged here because it is granted later by the owner or the automation policy.
        Nothing is persisted or sent.
        """
        from dataclasses import replace

        from amrt.core.enums import AuthorizationKind, OrderPurpose, OrderType, Origin, Side
        acct = self.builder.accounts.get(account_id)
        mode = self.builder.mode_ctl.mode
        now = self.clock.ts()
        auth = AuthorizationContext(kind=AuthorizationKind.OWNER_APPROVAL, approved_by="precheck", approval_id="precheck", step_up_at=now, approved_at=now)
        legs = sorted(action["legs"], key=lambda leg_: 0 if leg_["side"] == "BUY" else 1)
        hypo: list[dict] = []
        extra_lots = 0
        out, failed = [], []
        for i, leg in enumerate(legs):
            otype = OrderType(leg.get("order_type", "LIMIT"))
            intent = self.make_intent(account_id=account_id, broker=acct.broker, mode=mode, instrument_key=leg["instrument_key"], side=Side(leg["side"]),
                                      lots=int(leg["lots"]), order_type=otype, limit_price=leg.get("limit_price") if otype == OrderType.LIMIT else None,
                                      purpose=OrderPurpose(leg.get("purpose", "ENTRY")), reduce_only=bool(leg.get("reduce_only", False)), origin=Origin.OWNER_MANUAL,
                                      authorization=auth, correlation_id="precheck", idem_parts=("precheck", i))
            ctx = self.builder.build(intent)
            ctx = replace(ctx, same_underlying_positions=ctx.same_underlying_positions + hypo,
                          valuation={**ctx.valuation, "open_lots": ctx.valuation.get("open_lots", 0) + extra_lots})
            d = self.kernel.evaluate(intent, ctx)
            bad = [r for r in d.rules if r.applicable and not r.passed and r.rule_id not in ("RK-008",)]
            out.append({"instrument_key": leg["instrument_key"], "side": leg["side"], "lots": leg["lots"], "approved": not bad,
                        "failed_rules": [f"{r.rule_id} {r.name}: {r.detail}" for r in bad]})
            failed += [f"leg {i + 1} {r.rule_id} {r.name}: {r.detail}" for r in bad]
            inst = self.instruments.get(leg["instrument_key"])
            if inst.option_type is not None and not leg.get("reduce_only"):
                signed = intent.quantity if intent.side == Side.BUY else -intent.quantity
                hypo.append({"option_type": inst.option_type.value, "expiry": inst.expiry.isoformat(), "net_qty": signed})
                extra_lots += int(leg["lots"])
        return {"approved": not failed, "legs": out, "failed": failed, "policy_label": d.policy_label if legs else None,
                "note": "non-binding: every order is evaluated again by the Risk Kernel at submission; authorization is checked then"}

    def precheck(self, intent: OrderIntent) -> dict:
        """Non-binding evaluation for decision packages (nothing is persisted or sent)."""
        d = self.kernel.evaluate(intent, self.builder.build(intent))
        return {"approved": d.approved, "failed_rules": [r.model_dump() for r in d.rules if r.applicable and not r.passed], "policy_label": d.policy_label}

    async def submit(self, intent: OrderIntent) -> dict:
        ctx = self.builder.build(intent)
        decision = self.kernel.evaluate(intent, ctx)
        self.events.append("RISK_DECISION", {"decision_id": decision.decision_id, "intent_id": intent.intent_id, "approved": decision.approved,
                                             "failed_rules": decision.failed_rules, "rules": [r.model_dump() for r in decision.rules],
                                             "policy_version": decision.policy_version, "policy_label": decision.policy_label, "context_digest": decision.context_digest},
                           self.kernel.principal, correlation_id=intent.decision_id or intent.correlation_id, causation_id=intent.intent_id)
        order = await self.gateway.execute(self.principal, intent, decision)
        return {"intent_id": intent.intent_id, "decision": {"approved": decision.approved, "failed_rules": decision.failed_rules,
                                                            "rules": [r.model_dump() for r in decision.rules if r.applicable and not r.passed]},
                "order": {k: order.get(k) for k in ("intent_id", "state", "broker_order_id", "filled_qty", "avg_price", "last_error", "duplicate") if k in order}}
