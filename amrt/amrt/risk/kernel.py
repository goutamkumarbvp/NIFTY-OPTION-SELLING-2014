"""Deterministic Risk Kernel.

`evaluate(intent, ctx)` is a pure function of the intent and a RiskContext
snapshot: the same inputs always produce the same decision. Every rule runs and
its result is recorded (rule id, pass/fail, detail). The decision is approved
only if every applicable rule passes, and it is HMAC-signed and bound to the
intent hash with a short expiry. The Execution Gateway rejects anything that is
unsigned, rejected, expired or for a different intent — there is no override.

Rules distinguish NEW RISK from REDUCE-ONLY orders: freezes, kill switch,
emergencies, stale data and missing evidence block new risk but never block a
genuine, correctly sized exit.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from amrt.config import HardLimits
from amrt.core.clock import IST, parse_hhmm
from amrt.core.enums import USABLE_HEALTH, AuthorizationKind, DataLabel, HealthState, Mode, OrderType, Origin, Side
from amrt.core.ids import digest, new_id
from amrt.risk.policy import AutomationPolicy, RiskPolicy
from amrt.security.identity import Capability, Principal, require
from amrt.security.signing import Signer

KERNEL_VERSION = "risk-kernel/1.0"
DECISION_TTL_SECONDS = 5.0


class RuleResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    rule_id: str
    name: str
    applicable: bool
    passed: bool
    detail: str = ""


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    decision_id: str
    intent_id: str
    intent_hash: str
    approved: bool
    new_risk: bool
    rules: list[RuleResult]
    failed_rules: list[str]
    policy_version: int | None
    policy_hash: str | None
    policy_label: str
    evaluated_at: float
    expires_at: float
    kernel_version: str = KERNEL_VERSION
    context_digest: str
    signature: str = ""

    def signed_payload(self) -> dict:
        return {"decision_id": self.decision_id, "intent_id": self.intent_id, "intent_hash": self.intent_hash, "approved": self.approved,
                "new_risk": self.new_risk, "failed_rules": self.failed_rules, "policy_version": self.policy_version, "policy_hash": self.policy_hash,
                "evaluated_at": self.evaluated_at, "expires_at": self.expires_at, "kernel_version": self.kernel_version, "context_digest": self.context_digest}


@dataclass
class RiskContext:
    now_ts: float
    mode: Mode
    live_capable: bool
    account_kind: str                         # LIVE | PAPER
    account_enabled: bool
    policy: RiskPolicy | None
    policy_meta: dict | None                  # None → DEFAULT (paper only)
    hard: HardLimits
    safety: dict
    kill_file_engaged: bool
    valuation: dict                           # Ledger.valuation output for the account
    position_qty: int                         # signed units held in the intent's instrument
    inflight_reducing_qty: int
    working_orders: list[dict]                # {instrument_key, side, remaining, state}
    unknown_orders: list[dict]
    reconcile: dict                           # {last_ok_ts, divergence}
    broker_health: HealthState
    critical_health: dict[str, HealthState]
    instrument_age_ms: float | None
    instrument_label: DataLabel
    underlying_age_ms: float | None
    underlying_label: DataLabel
    ref_price: float | None                   # mid, else ltp, of the instrument (fresh only)
    underlying_spot: float | None
    instrument_verified: bool
    instrument_underlying: str
    instrument_exchange: str
    instrument_option_type: str | None
    instrument_expiry: str | None
    clock_drift_ms: float | None
    orders_last_minute: int
    funds: dict                               # {sod_funds, available, margin_used, age_s}
    est_margin_for_intent: float | None
    automation: AutomationPolicy | None = None
    automation_meta: dict | None = None
    master_healthy: bool = False
    same_underlying_positions: list[dict] = field(default_factory=list)   # [{option_type, expiry, net_qty}]

    def digest(self) -> str:
        d = {k: v for k, v in self.__dict__.items() if k not in ("policy", "hard", "automation")}
        d["policy"] = self.policy.model_dump(mode="json") if self.policy else None
        return digest(d)


class RiskKernel:
    def __init__(self, principal: Principal, signer: Signer, clock) -> None:
        require(principal, Capability.SIGN_RISK_DECISION, "construct risk kernel")
        self.principal = principal
        self._signer = signer
        self.clock = clock
        self.evaluations = 0
        self.rejections = 0
        self.errors = 0
        self.last_eval_ts = 0.0

    def verifier(self):
        return self._signer.verifier()

    # ------------------------------------------------------------------ api
    def evaluate(self, intent, ctx: RiskContext) -> RiskDecision:
        self.evaluations += 1
        self.last_eval_ts = self.clock.ts()
        try:
            rules = self._rules(intent, ctx)
        except Exception as exc:  # fail closed
            self.errors += 1
            rules = [RuleResult(rule_id="RK-000", name="KERNEL_ERROR", applicable=True, passed=False, detail=f"{type(exc).__name__}: {exc}")]
        failed = [r.rule_id for r in rules if r.applicable and not r.passed]
        approved = not failed
        if not approved:
            self.rejections += 1
        now = self.clock.ts()
        meta = ctx.policy_meta or {}
        dec = RiskDecision(decision_id=new_id("RD"), intent_id=intent.intent_id, intent_hash=intent.intent_hash(), approved=approved,
                           new_risk=not intent.reduce_only, rules=rules, failed_rules=failed, policy_version=meta.get("version"),
                           policy_hash=meta.get("hash"), policy_label="ACTIVE" if ctx.policy_meta else "DEFAULT",
                           evaluated_at=now, expires_at=now + DECISION_TTL_SECONDS, context_digest=ctx.digest())
        return dec.model_copy(update={"signature": self._signer.sign(dec.signed_payload())})

    # ---------------------------------------------------------------- rules
    def _rules(self, it, c: RiskContext) -> list[RuleResult]:
        out: list[RuleResult] = []
        new = not it.reduce_only
        live = c.account_kind == "LIVE"
        pol = c.policy

        def add(rid: str, name: str, applicable: bool, ok: bool, detail: str = "") -> None:
            out.append(RuleResult(rule_id=rid, name=name, applicable=applicable, passed=ok if applicable else True, detail=detail))

        blockers = []
        if c.safety.get("kill_switch", {}).get("engaged") or c.kill_file_engaged:
            blockers.append("KILL_SWITCH")
        add("RK-001", "KILL_SWITCH", new, not blockers, "kill switch engaged — only reduce-only orders" if blockers else "")
        fr = c.safety.get("freeze", {})
        add("RK-002", "NEW_RISK_FREEZE", new, not fr.get("active"), ", ".join(r["code"] for r in fr.get("reasons", [])))
        rl = c.safety.get("recovery_lock", {})
        add("RK-003", "RECOVERY_LOCK", new, not rl.get("active"), rl.get("reason", ""))

        # mode / environment / account routing
        mode_ok = it.mode == c.mode or (it.origin == Origin.PROTECTIVE_WORKFLOW and it.mode != Mode.PAPER and c.mode != Mode.PAPER)
        route_ok = (it.mode == Mode.PAPER) == (c.account_kind == "PAPER")
        add("RK-004", "MODE_AND_ROUTE", True, mode_ok and route_ok,
            f"intent mode {it.mode}, current mode {c.mode}, account {c.account_kind}" if not (mode_ok and route_ok) else "")
        add("RK-005", "LIVE_ENVIRONMENT", live, c.live_capable, "deployment is not LIVE_CAPABLE with live orders enabled" if not c.live_capable else "")
        add("RK-006", "ACCOUNT_ENABLED", True, c.account_enabled, "" if c.account_enabled else "account disabled")
        add("RK-007", "POLICY_APPROVED", live, c.policy_meta is not None and pol is not None, "no owner-approved ACTIVE risk policy" if c.policy_meta is None else "")
        if pol is None:
            add("RK-007b", "POLICY_PRESENT", True, False, "no risk policy")
            return out

        # authorization by origin
        auth = it.authorization
        if it.origin == Origin.OWNER_MANUAL:
            ok = auth.kind == AuthorizationKind.OWNER_APPROVAL and auth.approved_at <= c.now_ts and c.now_ts - auth.approved_at <= 600
            add("RK-008", "AUTHORIZATION", True, ok, "" if ok else "owner approval missing or older than 10 minutes")
        elif it.origin == Origin.AUTOMATION:
            reasons = []
            if c.mode != Mode.AUTOMATIC:
                reasons.append("not in AUTOMATIC mode")
            if c.safety.get("ai_suspended", {}).get("active"):
                reasons.append("AI-driven actions suspended")
            if not c.master_healthy:
                reasons.append("Master Agent not healthy")
            ap = c.automation
            if ap is None or c.automation_meta is None:
                reasons.append("no ACTIVE automation policy")
            else:
                if ap.valid_until < c.now_ts:
                    reasons.append("automation policy expired")
                if auth.approval_id != c.automation_meta.get("policy_id") or auth.policy_version != c.automation_meta.get("version"):
                    reasons.append("authorization references a different automation policy version")
                strat = next((s for s in ap.strategies if s.strategy_id == it.strategy_id and s.version == it.strategy_version), None)
                if new and strat is None:
                    reasons.append(f"strategy {it.strategy_id}@{it.strategy_version} not pre-authorized")
                if new and strat is not None and it.lots > min(strat.max_lots, ap.max_lots_per_order):
                    reasons.append("lots exceed the automation ceiling")
                if c.instrument_underlying not in ap.underlyings:
                    reasons.append("underlying not pre-authorized")
                if it.order_type not in ap.order_types and new:
                    reasons.append("order type not pre-authorized")
                if new and ap.entries_require_owner:
                    reasons.append("automation policy requires owner approval for entries")
                if new and not _in_window(c.now_ts, ap.trading_windows.get(c.instrument_exchange)):
                    reasons.append("outside the automation trading window")
                if new and c.valuation.get("open_lots", 0) + it.lots > ap.max_open_lots:
                    reasons.append("automation max_open_lots")
            add("RK-008", "AUTHORIZATION", True, not reasons, "; ".join(reasons))
        else:  # protective workflow
            ok = it.reduce_only and auth.kind == AuthorizationKind.PROTECTIVE_PREAUTH
            add("RK-008", "AUTHORIZATION", True, ok, "" if ok else "protective intents must be reduce-only and pre-authorized")
        add("RK-009", "INTENT_NOT_EXPIRED", True, c.now_ts <= it.expires_at, "intent expired" if c.now_ts > it.expires_at else "")

        # data integrity
        max_age = pol.max_data_age_ms
        good_labels = {DataLabel.LIVE_VERIFIED} if live else {DataLabel.LIVE_VERIFIED, DataLabel.LIVE_UNVERIFIED, DataLabel.HISTORICAL_REPLAY}
        inst_fresh = c.instrument_age_ms is not None and c.instrument_age_ms <= max_age and c.instrument_label in good_labels
        und_fresh = c.underlying_age_ms is not None and c.underlying_age_ms <= max_age and c.underlying_label in good_labels
        needs_fresh = new or it.order_type == OrderType.LIMIT
        add("RK-010", "DATA_FRESHNESS", needs_fresh, inst_fresh and (und_fresh or not new),
            f"instrument {c.instrument_label} age {c.instrument_age_ms} ms, underlying {c.underlying_label} age {c.underlying_age_ms} ms (max {max_age})")
        add("RK-011", "BROKER_HEALTH", live, c.broker_health == HealthState.HEALTHY if new else c.broker_health not in (HealthState.QUARANTINED,),
            f"broker {c.broker_health}")
        rec = c.reconcile
        rec_fresh = rec.get("last_ok_ts") is not None and c.now_ts - rec["last_ok_ts"] <= pol.max_reconcile_age_s
        add("RK-012", "RECONCILIATION", live and new, rec_fresh and not rec.get("divergence"),
            "positions/orders not reconciled recently" if not rec_fresh else ("broker state diverges from ledger" if rec.get("divergence") else ""))
        same_unknown = [u for u in c.unknown_orders if u["instrument_key"] == it.instrument_key]
        add("RK-013", "UNKNOWN_ORDERS", True, not same_unknown and (not new or not c.unknown_orders),
            f"{len(c.unknown_orders)} order(s) in ORDER STATE UNKNOWN" if c.unknown_orders else "")
        dup = [w for w in c.working_orders if w["instrument_key"] == it.instrument_key and w["side"] == it.side.value]
        add("RK-014", "DUPLICATE_WORKING_ORDER", True, not dup, "a working order on the same instrument and side exists" if dup else "")

        # instrument & reduce-only correctness
        perm = c.instrument_underlying in pol.permitted_underlyings and it.product in pol.permitted_products
        add("RK-015", "INSTRUMENT_PERMITTED", new, perm and (c.instrument_verified or not live),
            "" if perm else "underlying/product not permitted" + ("" if c.instrument_verified or not live else "; instrument not from the broker master"))
        if it.reduce_only:
            pos = c.position_qty
            opposite = (pos > 0 and it.side == Side.SELL) or (pos < 0 and it.side == Side.BUY)
            room = abs(pos) - c.inflight_reducing_qty
            ok = opposite and it.quantity <= room
            add("RK-016", "REDUCE_ONLY_SIZE", True, ok, f"position {pos}, in-flight exits {c.inflight_reducing_qty}, order {it.side} {it.quantity}")
        else:
            add("RK-016", "REDUCE_ONLY_SIZE", False, True)
        add("RK-017", "ENTRY_WINDOW", new, _in_window(c.now_ts, pol.entry_windows.get(c.instrument_exchange)),
            f"entries allowed {pol.entry_windows.get(c.instrument_exchange)} IST")

        # sizing / exposure / margin
        signed = it.quantity if it.side == Side.BUY else -it.quantity
        after_lots = abs(c.position_qty + signed) // max(1, it.lot_size)
        lots_ok = it.lots <= pol.max_lots_per_order and after_lots <= pol.max_lots_per_instrument and c.valuation.get("open_lots", 0) + it.lots <= pol.max_open_lots
        add("RK-018", "LOT_LIMITS", new, lots_ok, f"order {it.lots} (max {pol.max_lots_per_order}), instrument after {after_lots} (max {pol.max_lots_per_instrument}), "
                                                  f"open {c.valuation.get('open_lots', 0)}+{it.lots} (max {pol.max_open_lots})")
        gross = c.valuation.get("gross_notional")
        exp_ok = gross is not None and c.underlying_spot is not None and gross + it.quantity * c.underlying_spot <= pol.max_gross_exposure_inr
        add("RK-019", "GROSS_EXPOSURE", new, exp_ok, "exposure unknown (DATA UNAVAILABLE)" if gross is None or c.underlying_spot is None else
            f"{gross:.0f} + {it.quantity * c.underlying_spot:.0f} vs {pol.max_gross_exposure_inr:.0f}")
        f = c.funds
        denom = f.get("sod_funds")
        margin_ok, margin_detail = False, "funds / margin unknown (DATA UNAVAILABLE)"
        if denom and c.est_margin_for_intent is not None and f.get("margin_used") is not None and (f.get("age_s") is None or f["age_s"] <= 3 * pol.max_reconcile_age_s):
            util = 100.0 * (f["margin_used"] + c.est_margin_for_intent) / denom
            margin_ok = util <= pol.max_margin_utilisation_pct
            margin_detail = f"utilisation after order {util:.1f}% (max {pol.max_margin_utilisation_pct}%)"
        add("RK-020", "MARGIN_UTILISATION", new, margin_ok, margin_detail)

        # loss & margin-risk thresholds
        pnl = c.valuation.get("net_pnl_today")
        loss = max(0.0, -pnl) if pnl is not None else None
        add("RK-021", "LOSS_THRESHOLD", new, loss is not None and loss < pol.loss_level1_inr,
            "P&L unavailable (missing marks)" if loss is None else f"loss {loss:.0f} vs level1 {pol.loss_level1_inr:.0f}")
        mden = denom if pol.margin_risk_denominator == "SOD_ACCOUNT_FUNDS" else f.get("margin_used")
        if loss is None or not mden:
            add("RK-022", "MARGIN_RISK", new, False, f"margin-risk unavailable (loss {loss}, {pol.margin_risk_denominator} {mden})")
        else:
            mr = 100.0 * loss / mden
            add("RK-022", "MARGIN_RISK", new, mr < pol.margin_risk_level1_pct, f"{mr:.2f}% of {pol.margin_risk_denominator} vs level1 {pol.margin_risk_level1_pct}%")

        # price
        if it.order_type == OrderType.LIMIT:
            ref = c.ref_price
            ok = ref is not None and abs(it.limit_price - ref) / ref * 100 <= pol.price_collar_pct
            add("RK-023", "PRICE_COLLAR", True, ok, "no fresh reference price" if ref is None else f"limit {it.limit_price} vs ref {ref:.2f} (±{pol.price_collar_pct}%)")
        else:
            add("RK-023", "PRICE_COLLAR", new, pol.allow_market_entries, "MARKET entries disabled by policy")
        if new and it.side == Side.SELL and c.instrument_option_type:
            long_cover = sum(p["net_qty"] for p in c.same_underlying_positions if p["option_type"] == c.instrument_option_type and p["expiry"] == c.instrument_expiry and p["net_qty"] > 0)
            short_after = -sum(p["net_qty"] for p in c.same_underlying_positions if p["option_type"] == c.instrument_option_type and p["expiry"] == c.instrument_expiry and p["net_qty"] < 0) + it.quantity
            naked = short_after > long_cover
            add("RK-024", "NAKED_SHORT", True, pol.allow_naked_short or not naked, f"short {short_after} vs long cover {long_cover}")
        else:
            add("RK-024", "NAKED_SHORT", False, True)

        # infrastructure / time / rate / hard limits
        crit = {k: v for k, v in c.critical_health.items() if k in ("gateway", "event_store", "portfolio_risk_monitor", "safety_monitor") or (live and k == "reconciler")}
        bad = [f"{k}={v}" for k, v in crit.items() if v not in USABLE_HEALTH]
        add("RK-025", "CRITICAL_SERVICES", new, not bad, ", ".join(bad))
        drift = c.clock_drift_ms
        add("RK-026", "CLOCK_DRIFT", new and live, drift is not None and abs(drift) <= pol.max_clock_drift_ms,
            "clock drift unverifiable" if drift is None else f"{drift:.0f} ms (max {pol.max_clock_drift_ms})")
        lim = pol.max_orders_per_minute * (2 if it.reduce_only else 1)
        add("RK-027", "ORDER_RATE", True, c.orders_last_minute < lim, f"{c.orders_last_minute} orders in the last minute (limit {lim})")
        hard = c.hard
        hard_ok = it.lots <= hard.max_lots_per_order and c.valuation.get("open_lots", 0) + it.lots <= hard.max_open_lots and \
            (gross is None or c.underlying_spot is None or gross + it.quantity * c.underlying_spot <= hard.max_gross_exposure_inr)
        add("RK-028", "HARD_LIMITS", new, hard_ok, "exceeds deployment hard limits" if not hard_ok else "")
        return out


def _in_window(ts: float, window: list[str] | None) -> bool:
    if not window:
        return False
    t = dt.datetime.fromtimestamp(ts, IST)
    if t.weekday() >= 5:
        return False
    start, end = parse_hhmm(window[0]), parse_hhmm(window[1])
    return start <= t.time() <= end


def explain(decision: RiskDecision) -> dict[str, Any]:
    return {"decision_id": decision.decision_id, "approved": decision.approved, "failed": [r.model_dump() for r in decision.rules if r.applicable and not r.passed],
            "policy": {"version": decision.policy_version, "label": decision.policy_label}, "kernel": decision.kernel_version}
