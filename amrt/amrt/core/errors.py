"""Typed failures. Every safety refusal is an exception with a stable code the audit trail records."""
from __future__ import annotations


class AmrtError(Exception):
    code = "AMRT_ERROR"

    def __init__(self, message: str = "", **detail) -> None:
        super().__init__(message or self.code)
        self.detail = detail


class PermissionDenied(AmrtError):
    code = "PERMISSION_DENIED"


class PaperIsolationViolation(PermissionDenied):
    code = "PAPER_ISOLATION_VIOLATION"


class AuthenticationRequired(AmrtError):
    code = "AUTHENTICATION_REQUIRED"


class StepUpRequired(AmrtError):
    code = "STEP_UP_REQUIRED"


class InvalidTransition(AmrtError):
    code = "INVALID_TRANSITION"


class HardLimitViolation(AmrtError):
    code = "HARD_LIMIT_VIOLATION"


class RiskRejected(AmrtError):
    code = "RISK_REJECTED"


class DataUnavailable(AmrtError):
    code = "DATA UNAVAILABLE"


class SchemaViolation(AmrtError):
    code = "SCHEMA_VIOLATION"


class Quarantined(AmrtError):
    code = "QUARANTINED"


class DuplicateOrderRisk(AmrtError):
    code = "DUPLICATE_ORDER_RISK"


class NotReady(AmrtError):
    code = "NOT READY"


class BrokerError(AmrtError):
    code = "BROKER_ERROR"


class BrokerTimeout(BrokerError):
    """The broker did not answer in time: the order outcome is UNKNOWN, never assumed."""
    code = "BROKER_TIMEOUT"


class BrokerRejected(BrokerError):
    code = "BROKER_REJECTED"


class CapabilityUnavailable(BrokerError):
    code = "CAPABILITY_UNAVAILABLE"
