"""Order intents, authorization context and submission tickets.

An OrderIntent is a proposal to trade, persisted before anything is sent. It is
immutable; its hash binds the Risk Kernel's signed decision and the Gateway's
submission ticket to exactly these fields.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from amrt.core.enums import AuthorizationKind, Environment, Mode, OrderPurpose, OrderType, Origin, Product, Side, Validity
from amrt.core.ids import digest

INTENT_SCHEMA = "intent/1"


class AuthorizationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: AuthorizationKind
    approved_by: str                      # username, "automation:<policy_id>" or "protective:<policy_id>"
    approval_id: str | None = None        # approval request id / automation policy id
    policy_version: int | None = None
    step_up_at: float | None = None       # when the human re-authenticated
    approved_at: float


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str
    schema_version: str = INTENT_SCHEMA
    created_at: float
    mode: Mode
    environment: Environment
    account_id: str
    broker: str                           # "paper" for paper accounts
    instrument_key: str
    trading_symbol: str
    lot_size: int
    side: Side
    lots: int
    order_type: OrderType
    limit_price: float | None = None
    product: Product = Product.NRML
    validity: Validity = Validity.DAY
    purpose: OrderPurpose
    reduce_only: bool = False
    origin: Origin
    authorization: AuthorizationContext
    decision_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    correlation_id: str
    idempotency_key: str
    expires_at: float
    note: str = ""

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @model_validator(mode="after")
    def _check(self) -> OrderIntent:
        if self.lots <= 0 or self.lot_size <= 0:
            raise ValueError("lots and lot_size must be positive")
        if self.order_type == OrderType.LIMIT and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("LIMIT orders need a positive limit_price")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET orders must not carry a limit_price")
        if self.purpose in (OrderPurpose.EXIT, OrderPurpose.PROTECTIVE) and not self.reduce_only:
            raise ValueError("EXIT/PROTECTIVE intents must be reduce_only")
        if self.origin == Origin.PROTECTIVE_WORKFLOW and (not self.reduce_only or self.authorization.kind != AuthorizationKind.PROTECTIVE_PREAUTH):
            raise ValueError("protective workflow intents must be reduce_only with PROTECTIVE_PREAUTH")
        if self.origin == Origin.AUTOMATION and self.authorization.kind != AuthorizationKind.AUTOMATION_POLICY:
            raise ValueError("automation intents need an AUTOMATION_POLICY authorization")
        if self.origin == Origin.OWNER_MANUAL and self.authorization.kind != AuthorizationKind.OWNER_APPROVAL:
            raise ValueError("owner intents need an OWNER_APPROVAL authorization")
        if self.mode == Mode.PAPER and self.broker != "paper":
            raise ValueError("PAPER mode intents must target a paper account")
        return self

    def intent_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class SubmissionTicket(BaseModel):
    """Minted only by the Execution Gateway (HMAC); verified by every order client before contacting a venue."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    intent_id: str
    intent_hash: str
    idempotency_key: str
    client_tag: str
    account_id: str
    broker: str
    mode: Mode
    environment: Environment
    instrument_key: str
    side: Side
    quantity: int
    issued_at: float
    expires_at: float
    signature: str = ""

    def payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"signature"})


class BrokerOrderRequest(BaseModel):
    """Venue-neutral order request derived from an intent; adapters map it to their SDK."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    instrument_key: str
    trading_symbol: str
    broker_ref: dict[str, str] = Field(default_factory=dict)
    exchange: str
    segment: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: float | None
    product: Product
    validity: Validity
    client_tag: str
