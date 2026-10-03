"""Principals, capabilities and the permission matrix.

Every actor — human or service — is a Principal with a fixed, frozen set of
capabilities derived from its kind. Capabilities are never granted at runtime:
the matrix below is the single source of truth and is covered by tests
(tests/test_security_permissions.py). A principal cannot add capabilities to
itself or another principal; `Principal` is immutable.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from amrt.core.errors import PermissionDenied


class Capability(StrEnum):
    # read
    READ_MARKET_DATA = "READ_MARKET_DATA"
    READ_PORTFOLIO = "READ_PORTFOLIO"
    READ_HEALTH = "READ_HEALTH"
    READ_AUDIT = "READ_AUDIT"
    # intelligence
    PUBLISH_ADVICE = "PUBLISH_ADVICE"                # specialist agents
    PROPOSE_DECISION = "PROPOSE_DECISION"            # master agent
    PROPOSE_STRATEGY_ACTION = "PROPOSE_STRATEGY_ACTION"  # quantitative strategy agent only
    # human authority
    APPROVE_ACTION = "APPROVE_ACTION"
    CREATE_MANUAL_INTENT = "CREATE_MANUAL_INTENT"
    CHANGE_MODE = "CHANGE_MODE"
    CHANGE_RISK_POLICY = "CHANGE_RISK_POLICY"
    APPROVE_AUTOMATION_POLICY = "APPROVE_AUTOMATION_POLICY"
    RELEASE_FREEZE = "RELEASE_FREEZE"
    RELEASE_KILL_SWITCH = "RELEASE_KILL_SWITCH"
    UNQUARANTINE = "UNQUARANTINE"
    MANAGE_USERS = "MANAGE_USERS"
    MANUAL_QUICK_EXIT = "MANUAL_QUICK_EXIT"
    ACK_INCIDENT = "ACK_INCIDENT"
    RUN_BACKUP = "RUN_BACKUP"
    IMPORT_DATA = "IMPORT_DATA"
    # safety (reduce authority only)
    ENGAGE_KILL_SWITCH = "ENGAGE_KILL_SWITCH"
    FREEZE_NEW_RISK = "FREEZE_NEW_RISK"
    REQUEST_PROTECTIVE_ACTION = "REQUEST_PROTECTIVE_ACTION"
    # risk kernel
    EVALUATE_RISK = "EVALUATE_RISK"
    SIGN_RISK_DECISION = "SIGN_RISK_DECISION"
    # execution
    REQUEST_EXECUTION = "REQUEST_EXECUTION"          # mode controllers / protective workflow ask the gateway
    SUBMIT_LIVE_ORDER = "SUBMIT_LIVE_ORDER"
    CANCEL_LIVE_ORDER = "CANCEL_LIVE_ORDER"
    SUBMIT_PAPER_ORDER = "SUBMIT_PAPER_ORDER"
    RECONCILE = "RECONCILE"
    HOLD_BROKER_CREDENTIALS = "HOLD_BROKER_CREDENTIALS"
    # reliability
    RECOVERY_ALLOWLIST = "RECOVERY_ALLOWLIST"
    QUARANTINE_COMPONENT = "QUARANTINE_COMPONENT"
    CREATE_INCIDENT = "CREATE_INCIDENT"


class PrincipalKind(StrEnum):
    OWNER = "OWNER"
    OPERATOR = "OPERATOR"
    READ_ONLY = "READ_ONLY"
    SPECIALIST_AGENT = "SPECIALIST_AGENT"
    STRATEGY_AGENT = "STRATEGY_AGENT"
    MASTER_AGENT = "MASTER_AGENT"
    RISK_KERNEL = "RISK_KERNEL"
    PORTFOLIO_RISK_MONITOR = "PORTFOLIO_RISK_MONITOR"   # protective path A
    SAFETY_MONITOR = "SAFETY_MONITOR"                   # protective path B
    MODE_CONTROLLER = "MODE_CONTROLLER"
    PROTECTIVE_WORKFLOW = "PROTECTIVE_WORKFLOW"
    EXECUTION_GATEWAY = "EXECUTION_GATEWAY"
    RECONCILER = "RECONCILER"
    RELIABILITY_PLANE = "RELIABILITY_PLANE"
    WATCHDOG = "WATCHDOG"
    MONITORING = "MONITORING"
    DEPLOYMENT = "DEPLOYMENT"


C = Capability
_READ = {C.READ_MARKET_DATA, C.READ_PORTFOLIO, C.READ_HEALTH}

PERMISSION_MATRIX: dict[PrincipalKind, frozenset[Capability]] = {
    PrincipalKind.OWNER: frozenset(_READ | {C.READ_AUDIT, C.APPROVE_ACTION, C.CREATE_MANUAL_INTENT, C.CHANGE_MODE, C.CHANGE_RISK_POLICY,
                                            C.APPROVE_AUTOMATION_POLICY, C.RELEASE_FREEZE, C.RELEASE_KILL_SWITCH, C.UNQUARANTINE,
                                            C.MANAGE_USERS, C.MANUAL_QUICK_EXIT, C.ACK_INCIDENT, C.RUN_BACKUP, C.IMPORT_DATA,
                                            C.ENGAGE_KILL_SWITCH, C.FREEZE_NEW_RISK}),
    # an operator may reduce risk and run operations, never extend authority
    PrincipalKind.OPERATOR: frozenset(_READ | {C.READ_AUDIT, C.ENGAGE_KILL_SWITCH, C.FREEZE_NEW_RISK, C.MANUAL_QUICK_EXIT, C.ACK_INCIDENT,
                                               C.RUN_BACKUP, C.IMPORT_DATA}),
    PrincipalKind.READ_ONLY: frozenset(_READ | {C.READ_AUDIT}),
    PrincipalKind.SPECIALIST_AGENT: frozenset({C.READ_MARKET_DATA, C.READ_PORTFOLIO, C.READ_HEALTH, C.PUBLISH_ADVICE}),
    PrincipalKind.STRATEGY_AGENT: frozenset({C.READ_MARKET_DATA, C.READ_PORTFOLIO, C.READ_HEALTH, C.PUBLISH_ADVICE, C.PROPOSE_STRATEGY_ACTION}),
    PrincipalKind.MASTER_AGENT: frozenset({C.READ_MARKET_DATA, C.READ_PORTFOLIO, C.READ_HEALTH, C.PROPOSE_DECISION}),
    PrincipalKind.RISK_KERNEL: frozenset(_READ | {C.EVALUATE_RISK, C.SIGN_RISK_DECISION, C.FREEZE_NEW_RISK, C.ENGAGE_KILL_SWITCH}),
    PrincipalKind.PORTFOLIO_RISK_MONITOR: frozenset(_READ | {C.FREEZE_NEW_RISK, C.ENGAGE_KILL_SWITCH, C.REQUEST_PROTECTIVE_ACTION, C.CREATE_INCIDENT}),
    PrincipalKind.SAFETY_MONITOR: frozenset(_READ | {C.FREEZE_NEW_RISK, C.ENGAGE_KILL_SWITCH, C.CREATE_INCIDENT}),
    PrincipalKind.MODE_CONTROLLER: frozenset(_READ | {C.REQUEST_EXECUTION}),
    PrincipalKind.PROTECTIVE_WORKFLOW: frozenset(_READ | {C.REQUEST_EXECUTION}),
    PrincipalKind.EXECUTION_GATEWAY: frozenset(_READ | {C.SUBMIT_LIVE_ORDER, C.CANCEL_LIVE_ORDER, C.SUBMIT_PAPER_ORDER, C.HOLD_BROKER_CREDENTIALS}),
    PrincipalKind.RECONCILER: frozenset(_READ | {C.RECONCILE, C.FREEZE_NEW_RISK, C.CREATE_INCIDENT}),
    PrincipalKind.RELIABILITY_PLANE: frozenset({C.READ_HEALTH, C.RECOVERY_ALLOWLIST, C.QUARANTINE_COMPONENT, C.CREATE_INCIDENT, C.FREEZE_NEW_RISK}),
    PrincipalKind.WATCHDOG: frozenset({C.READ_HEALTH, C.ENGAGE_KILL_SWITCH, C.FREEZE_NEW_RISK, C.CREATE_INCIDENT}),
    PrincipalKind.MONITORING: frozenset({C.READ_HEALTH}),
    PrincipalKind.DEPLOYMENT: frozenset(),
}

# Capabilities that may never be held by any non-human principal except the listed holders.
EXCLUSIVE = {
    C.SUBMIT_LIVE_ORDER: {PrincipalKind.EXECUTION_GATEWAY},
    C.CANCEL_LIVE_ORDER: {PrincipalKind.EXECUTION_GATEWAY},
    C.HOLD_BROKER_CREDENTIALS: {PrincipalKind.EXECUTION_GATEWAY},
    C.SIGN_RISK_DECISION: {PrincipalKind.RISK_KERNEL},
    C.CHANGE_MODE: {PrincipalKind.OWNER},
    C.CHANGE_RISK_POLICY: {PrincipalKind.OWNER},
    C.APPROVE_AUTOMATION_POLICY: {PrincipalKind.OWNER},
    C.RELEASE_FREEZE: {PrincipalKind.OWNER},
    C.RELEASE_KILL_SWITCH: {PrincipalKind.OWNER},
    C.UNQUARANTINE: {PrincipalKind.OWNER},
    C.MANAGE_USERS: {PrincipalKind.OWNER},
}

HUMAN_KINDS = {PrincipalKind.OWNER, PrincipalKind.OPERATOR, PrincipalKind.READ_ONLY}


@dataclass(frozen=True)
class Principal:
    id: str
    kind: PrincipalKind
    display: str = ""
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    @staticmethod
    def of(kind: PrincipalKind, id: str, display: str = "") -> Principal:
        return Principal(id=id, kind=kind, display=display or id, capabilities=PERMISSION_MATRIX[kind])

    @property
    def is_human(self) -> bool:
        return self.kind in HUMAN_KINDS

    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities and cap in PERMISSION_MATRIX[self.kind]

    def audit_identity(self) -> dict:
        return {"id": self.id, "kind": self.kind.value}


AuditHook = Callable[[str, dict, "Principal"], None]
_audit_hook: AuditHook | None = None


def set_denial_audit_hook(hook: AuditHook | None) -> None:
    """The composition root installs a hook so every denial lands in the event store."""
    global _audit_hook
    _audit_hook = hook


def require(principal: Principal, cap: Capability, action: str = "") -> None:
    if not isinstance(principal, Principal) or not principal.can(cap):
        detail = {"capability": cap.value, "action": action, "principal": principal.audit_identity() if isinstance(principal, Principal) else str(principal)}
        if _audit_hook is not None and isinstance(principal, Principal):
            try:
                _audit_hook("PERMISSION_DENIED", detail, principal)
            except Exception:
                pass
        raise PermissionDenied(f"{getattr(principal, 'id', principal)} lacks {cap.value} for {action or 'this action'}", **detail)


def validate_matrix() -> list[str]:
    """Static invariants on the matrix (run at startup and in tests)."""
    problems: list[str] = []
    for cap, holders in EXCLUSIVE.items():
        for kind, caps in PERMISSION_MATRIX.items():
            if cap in caps and kind not in holders:
                problems.append(f"{kind.value} must not hold {cap.value}")
    for kind in (PrincipalKind.SPECIALIST_AGENT, PrincipalKind.STRATEGY_AGENT, PrincipalKind.MASTER_AGENT, PrincipalKind.RELIABILITY_PLANE):
        for forbidden in (C.SUBMIT_LIVE_ORDER, C.SUBMIT_PAPER_ORDER, C.REQUEST_EXECUTION, C.CHANGE_MODE, C.CHANGE_RISK_POLICY,
                          C.RELEASE_FREEZE, C.RELEASE_KILL_SWITCH, C.HOLD_BROKER_CREDENTIALS, C.SIGN_RISK_DECISION, C.APPROVE_ACTION):
            if forbidden in PERMISSION_MATRIX[kind]:
                problems.append(f"{kind.value} must not hold {forbidden.value}")
    return problems
