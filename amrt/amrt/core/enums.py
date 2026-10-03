"""Shared vocabulary. String values are what the dashboard and audit trail show verbatim."""
from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    PAPER = "PAPER"
    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"


MODE_BANNER = {
    Mode.PAPER: "PAPER MODE — NO REAL ORDERS",
    Mode.MANUAL: "MANUAL MODE — OWNER CONTROL",
    Mode.AUTOMATIC: "AUTOMATIC MODE — MASTER AI COORDINATION — RISK ENGINE ENFORCED",
}


class Environment(StrEnum):
    """Deployment-level capability. PAPER_ONLY processes never construct live order clients."""
    PAPER_ONLY = "PAPER_ONLY"
    LIVE_CAPABLE = "LIVE_CAPABLE"


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"
    QUARANTINED = "QUARANTINED"
    UNKNOWN = "UNKNOWN"


GOOD_HEALTH = {HealthState.HEALTHY}
USABLE_HEALTH = {HealthState.HEALTHY, HealthState.DEGRADED}


class DecisionStatus(StrEnum):
    ADVISORY = "ADVISORY"
    REQUIRES_APPROVAL = "REQUIRES APPROVAL"
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    NO_ACTION = "NO ACTION"
    INSUFFICIENT_DATA = "INSUFFICIENT DATA"


class Stance(StrEnum):
    SUPPORTIVE = "SUPPORTIVE"
    NEUTRAL = "NEUTRAL"
    CAUTION = "CAUTION"
    BLOCK = "BLOCK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    MCX = "MCX"


class Segment(StrEnum):
    NSE_FO = "NSE_FO"
    BSE_FO = "BSE_FO"
    MCX_FO = "MCX_FO"
    NSE_CM = "NSE_CM"
    BSE_CM = "BSE_CM"
    NSE_INDEX = "NSE_INDEX"
    BSE_INDEX = "BSE_INDEX"


class InstrumentKind(StrEnum):
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    EQUITY = "EQUITY"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Product(StrEnum):
    NRML = "NRML"
    MIS = "MIS"


class Validity(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    PROTECTIVE = "PROTECTIVE"
    ADJUSTMENT = "ADJUSTMENT"
    HEDGE = "HEDGE"


class Origin(StrEnum):
    OWNER_MANUAL = "OWNER_MANUAL"          # owner-entered ticket or owner-approved proposal
    AUTOMATION = "AUTOMATION"              # automatic mode, inside a pre-authorized policy
    PROTECTIVE_WORKFLOW = "PROTECTIVE_WORKFLOW"  # pre-authorized protective action (stop / flatten)


class AuthorizationKind(StrEnum):
    OWNER_APPROVAL = "OWNER_APPROVAL"
    AUTOMATION_POLICY = "AUTOMATION_POLICY"
    PROTECTIVE_PREAUTH = "PROTECTIVE_PREAUTH"


class OrderState(StrEnum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    REJECTED_PRE_TRADE = "REJECTED_PRE_TRADE"   # validation / risk kernel / gateway refused; never sent
    SUBMITTING = "SUBMITTING"                   # persisted, handed to the venue
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"                       # rejected by broker / exchange
    CANCELLED = "CANCELLED"
    UNKNOWN = "ORDER STATE UNKNOWN"             # timeout / ambiguous: unresolved until reconciled
    NOT_FOUND_AT_BROKER = "NOT_FOUND_AT_BROKER"  # reconciliation proved the venue never received it


FINAL_ORDER_STATES = {OrderState.REJECTED_PRE_TRADE, OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.NOT_FOUND_AT_BROKER}
WORKING_ORDER_STATES = {OrderState.SUBMITTING, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.UNKNOWN}


class DataLabel(StrEnum):
    LIVE_VERIFIED = "LIVE DATA VERIFIED"
    LIVE_UNVERIFIED = "LIVE (UNVERIFIED)"
    HISTORICAL_REPLAY = "HISTORICAL REPLAY"
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "DATA UNAVAILABLE"


class FeatureStatus(StrEnum):
    """Broker capability matrix status."""
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"


class ImplStatus(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    TESTED = "TESTED"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT RUN"
    DEGRADED = "DEGRADED"
    QUARANTINED = "QUARANTINED"
    RECOVERING = "RECOVERING"


class Readiness(StrEnum):
    READY = "READY"
    NOT_READY = "NOT READY"
    BLOCKED = "BLOCKED"
