"""Versioned, owner-approved risk and automation policies, validated against immutable hard limits."""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from amrt.config import HardLimits
from amrt.core.enums import Exchange, OrderType, Product
from amrt.core.errors import HardLimitViolation, PermissionDenied
from amrt.core.ids import digest, new_id
from amrt.security.identity import Capability, Principal, require
from amrt.storage.db import Database, policies

RISK_POLICY_SCHEMA = "risk-policy/1"
AUTOMATION_POLICY_SCHEMA = "automation-policy/1"


class RiskPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = RISK_POLICY_SCHEMA
    account_id: str
    # absolute portfolio loss triggers (₹, net of charges, today) — triggers, NOT guaranteed maximum losses
    loss_level1_inr: float = 2000.0           # → freeze new risk
    loss_level2_inr: float = 4000.0           # → emergency + pre-authorized protective action
    # margin-risk triggers: loss ÷ denominator × 100
    margin_risk_level1_pct: float = 1.0
    margin_risk_level2_pct: float = 2.0
    margin_risk_denominator: Literal["SOD_ACCOUNT_FUNDS", "MARGIN_USED"] = "SOD_ACCOUNT_FUNDS"
    protective_action_level2: Literal["FLATTEN_ALL", "ALERT_ONLY"] = "FLATTEN_ALL"
    max_lots_per_order: int = 2
    max_lots_per_instrument: int = 4
    max_open_lots: int = 8
    max_gross_exposure_inr: float = 10_000_000.0
    max_margin_utilisation_pct: float = 60.0
    max_data_age_ms: int = 3000
    max_clock_drift_ms: int = 1500
    max_reconcile_age_s: int = 60
    price_collar_pct: float = 5.0
    allow_market_entries: bool = False
    allow_naked_short: bool = False
    permitted_underlyings: list[str] = Field(default_factory=lambda: ["NIFTY", "SENSEX", "CRUDEOIL"])
    permitted_products: list[Product] = Field(default_factory=lambda: [Product.NRML, Product.MIS])
    entry_windows: dict[str, list[str]] = Field(default_factory=lambda: {"NSE": ["09:20", "15:00"], "BSE": ["09:20", "15:00"], "MCX": ["09:05", "23:00"]})
    per_leg_stop_pct: float | None = 50.0     # short leg: exit when premium rises this % above entry
    ratchet_activation_inr: float | None = 1500.0
    ratchet_trail_inr: float | None = 750.0
    max_orders_per_minute: int = 20

    @model_validator(mode="after")
    def _order(self) -> RiskPolicy:
        if not (0 < self.loss_level1_inr < self.loss_level2_inr):
            raise ValueError("loss thresholds must satisfy 0 < level1 < level2")
        if not (0 < self.margin_risk_level1_pct < self.margin_risk_level2_pct):
            raise ValueError("margin-risk thresholds must satisfy 0 < level1 < level2")
        if self.max_lots_per_order <= 0 or self.max_open_lots <= 0 or self.max_lots_per_instrument <= 0:
            raise ValueError("lot ceilings must be positive")
        for ex, win in self.entry_windows.items():
            Exchange(ex)
            if len(win) != 2:
                raise ValueError("entry window needs [start, end]")
        return self

    def check_hard_limits(self, hard: HardLimits) -> list[str]:
        v = []
        if self.loss_level2_inr > hard.max_loss_threshold_inr:
            v.append(f"loss_level2_inr {self.loss_level2_inr} > hard limit {hard.max_loss_threshold_inr}")
        if self.margin_risk_level2_pct > hard.max_margin_risk_pct:
            v.append(f"margin_risk_level2_pct > hard limit {hard.max_margin_risk_pct}")
        if self.max_lots_per_order > hard.max_lots_per_order:
            v.append(f"max_lots_per_order > hard limit {hard.max_lots_per_order}")
        if self.max_open_lots > hard.max_open_lots:
            v.append(f"max_open_lots > hard limit {hard.max_open_lots}")
        if self.max_gross_exposure_inr > hard.max_gross_exposure_inr:
            v.append("max_gross_exposure_inr > hard limit")
        if self.max_margin_utilisation_pct > hard.max_margin_utilisation_pct:
            v.append("max_margin_utilisation_pct > hard limit")
        if self.max_orders_per_minute > hard.max_orders_per_minute:
            v.append("max_orders_per_minute > hard limit")
        if self.max_data_age_ms > hard.max_data_age_ms:
            v.append("max_data_age_ms may only be stricter than the hard limit")
        if self.max_clock_drift_ms > hard.max_clock_drift_ms:
            v.append("max_clock_drift_ms may only be stricter than the hard limit")
        if self.price_collar_pct > hard.max_price_collar_pct:
            v.append("price_collar_pct > hard limit")
        if self.allow_naked_short and not hard.allow_naked_short:
            v.append("naked shorts are disabled by hard limits")
        return v


class StrategyAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    strategy_id: str
    version: str
    params: dict = Field(default_factory=dict)
    max_lots: int = 1


class AutomationPolicy(BaseModel):
    """Everything Automatic Mode may do. Anything not listed here is REQUIRES APPROVAL or NO ACTION."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = AUTOMATION_POLICY_SCHEMA
    account_id: str
    broker: str
    underlyings: list[str]
    expiry_rule: Literal["NEAREST", "NEAREST_WEEKLY", "NEAREST_MONTHLY"] = "NEAREST"
    strategies: list[StrategyAuthorization]
    trading_windows: dict[str, list[str]]
    order_types: list[OrderType] = Field(default_factory=lambda: [OrderType.LIMIT])
    max_lots_per_order: int = 1
    max_open_lots: int = 4
    max_gross_exposure_inr: float = 5_000_000.0
    max_margin_utilisation_pct: float = 50.0
    loss_level1_inr: float = 2000.0
    loss_level2_inr: float = 4000.0
    entries_require_owner: bool = False
    permitted_protective_actions: list[Literal["FLATTEN_ALL", "EXIT_LEG_ON_STOP", "EXIT_ON_RATCHET"]] = Field(
        default_factory=lambda: ["FLATTEN_ALL", "EXIT_LEG_ON_STOP", "EXIT_ON_RATCHET"])
    fallback: Literal["NO_ACTION", "REQUIRE_APPROVAL"] = "NO_ACTION"
    valid_until: float                     # epoch seconds; automation expires and must be re-approved

    def check_against(self, risk: RiskPolicy, hard: HardLimits) -> list[str]:
        v = []
        if self.account_id != risk.account_id:
            v.append("automation policy account differs from risk policy account")
        if self.max_lots_per_order > risk.max_lots_per_order:
            v.append("automation max_lots_per_order exceeds the risk policy")
        if self.max_open_lots > risk.max_open_lots:
            v.append("automation max_open_lots exceeds the risk policy")
        if self.max_gross_exposure_inr > risk.max_gross_exposure_inr:
            v.append("automation exposure ceiling exceeds the risk policy")
        if self.max_margin_utilisation_pct > risk.max_margin_utilisation_pct:
            v.append("automation margin ceiling exceeds the risk policy")
        if self.loss_level1_inr > risk.loss_level1_inr or self.loss_level2_inr > risk.loss_level2_inr:
            v.append("automation loss thresholds may only be stricter than the risk policy")
        if any(u not in risk.permitted_underlyings for u in self.underlyings):
            v.append("automation underlyings must be permitted by the risk policy")
        if any(s.max_lots > self.max_lots_per_order for s in self.strategies):
            v.append("a strategy max_lots exceeds the automation max_lots_per_order")
        if self.max_lots_per_order > hard.max_lots_per_order:
            v.append("automation max_lots_per_order exceeds hard limit")
        return v


class PolicyStore:
    """kind ∈ {RISK, AUTOMATION}. DRAFT → ACTIVE (owner, step-up) → SUPERSEDED. Bodies are hashed and immutable."""

    def __init__(self, db: Database, hard: HardLimits, events, clock_ts=time.time) -> None:
        self.db = db
        self.hard = hard
        self.events = events
        self._ts = clock_ts

    def _next_version(self, conn, kind: str, account_id: str) -> int:
        rows = [r[0] for r in conn.execute(select(policies.c.version).where(policies.c.kind == kind, policies.c.account_id == account_id))]
        return (max(rows) if rows else 0) + 1

    def propose(self, kind: str, body: BaseModel, principal: Principal, note: str = "") -> dict:
        cap = Capability.CHANGE_RISK_POLICY if kind == "RISK" else Capability.APPROVE_AUTOMATION_POLICY
        require(principal, cap, f"propose {kind} policy")
        if kind == "RISK":
            problems = body.check_hard_limits(self.hard)  # type: ignore[attr-defined]
        else:
            risk = self.active_risk(body.account_id)  # type: ignore[attr-defined]
            if risk is None:
                raise PermissionDenied("an ACTIVE risk policy is required before an automation policy")
            problems = body.check_against(risk, self.hard)  # type: ignore[attr-defined]
        if problems:
            raise HardLimitViolation("; ".join(problems), problems=problems)
        data = body.model_dump(mode="json")
        pid = new_id("POL")
        with self.db.tx() as conn:
            ver = self._next_version(conn, kind, data["account_id"])
            conn.execute(policies.insert().values(policy_id=pid, kind=kind, account_id=data["account_id"], version=ver, body=data, hash=digest(data),
                                                  status="DRAFT", created_by=principal.id, created_at=self._ts(), note=note))
        self.events.append("POLICY_PROPOSED", {"policy_id": pid, "kind": kind, "version": ver, "hash": digest(data), "body": data}, principal, correlation_id=pid)
        return {"policy_id": pid, "version": ver, "status": "DRAFT"}

    def activate(self, policy_id: str, principal: Principal) -> dict:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(policies).where(policies.c.policy_id == policy_id)).first()
        if row is None:
            raise KeyError(f"UNKNOWN_POLICY:{policy_id}")
        cap = Capability.CHANGE_RISK_POLICY if row.kind == "RISK" else Capability.APPROVE_AUTOMATION_POLICY
        require(principal, cap, f"activate {row.kind} policy")
        if row.status != "DRAFT":
            raise PermissionDenied(f"policy is {row.status}")
        if digest(row.body) != row.hash:
            raise PermissionDenied("policy body hash mismatch")
        body = (RiskPolicy if row.kind == "RISK" else AutomationPolicy).model_validate(row.body)
        if row.kind == "RISK":
            problems = body.check_hard_limits(self.hard)
        else:
            risk = self.active_risk(row.account_id)
            problems = ["no ACTIVE risk policy"] if risk is None else body.check_against(risk, self.hard)
        if problems:
            raise HardLimitViolation("; ".join(problems), problems=problems)
        with self.db.tx() as conn:
            conn.execute(policies.update().where(policies.c.kind == row.kind, policies.c.account_id == row.account_id, policies.c.status == "ACTIVE")
                         .values(status="SUPERSEDED"))
            conn.execute(policies.update().where(policies.c.policy_id == policy_id).values(status="ACTIVE", approved_by=principal.id, approved_at=self._ts()))
        self.events.append("POLICY_ACTIVATED", {"policy_id": policy_id, "kind": row.kind, "version": row.version, "hash": row.hash}, principal, correlation_id=policy_id)
        return {"policy_id": policy_id, "status": "ACTIVE", "version": row.version}

    def revoke_automation(self, account_id: str, principal: Principal, reason: str) -> None:
        """Owner or safety principals may revoke (reduce) automation; never grant it."""
        if not (principal.can(Capability.APPROVE_AUTOMATION_POLICY) or principal.can(Capability.FREEZE_NEW_RISK)):
            require(principal, Capability.APPROVE_AUTOMATION_POLICY, "revoke automation")
        with self.db.tx() as conn:
            conn.execute(policies.update().where(policies.c.kind == "AUTOMATION", policies.c.account_id == account_id, policies.c.status == "ACTIVE")
                         .values(status="REVOKED", note=reason))
        self.events.append("AUTOMATION_POLICY_REVOKED", {"account_id": account_id, "reason": reason}, principal)

    def _active(self, kind: str, account_id: str):
        with self.db.engine.connect() as conn:
            return conn.execute(select(policies).where(policies.c.kind == kind, policies.c.account_id == account_id, policies.c.status == "ACTIVE")).first()

    def active_risk(self, account_id: str) -> RiskPolicy | None:
        row = self._active("RISK", account_id)
        return RiskPolicy.model_validate(row.body) if row else None

    def active_risk_meta(self, account_id: str) -> dict | None:
        row = self._active("RISK", account_id)
        return {"policy_id": row.policy_id, "version": row.version, "hash": row.hash, "approved_by": row.approved_by, "approved_at": row.approved_at} if row else None

    def active_automation(self, account_id: str) -> tuple[AutomationPolicy, dict] | None:
        row = self._active("AUTOMATION", account_id)
        if row is None:
            return None
        pol = AutomationPolicy.model_validate(row.body)
        return pol, {"policy_id": row.policy_id, "version": row.version, "hash": row.hash, "approved_by": row.approved_by}

    def list(self, kind: str | None = None, account_id: str | None = None) -> list[dict]:
        q = select(policies).order_by(policies.c.created_at.desc())
        if kind:
            q = q.where(policies.c.kind == kind)
        if account_id:
            q = q.where(policies.c.account_id == account_id)
        with self.db.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]


def default_paper_policy(account_id: str) -> RiskPolicy:
    """Conservative policy used only for PAPER accounts that have no approved policy (labelled DEFAULT)."""
    return RiskPolicy(account_id=account_id)
