"""Execution venues (live broker order clients and the paper simulator) share one interface.

Every venue verifies the Gateway-signed SubmissionTicket before acting: the
signature, that the ticket matches the request field by field, that it has not
expired or been used, and that the venue kind matches the ticket's mode and
environment. Holding a venue object is therefore not enough to place an order.
"""
from __future__ import annotations

import abc
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from amrt.core.enums import Environment, Mode, Side
from amrt.core.errors import PaperIsolationViolation, PermissionDenied
from amrt.execution.intents import BrokerOrderRequest, SubmissionTicket


class BrokerAck(BaseModel):
    model_config = ConfigDict(frozen=True)
    accepted: bool
    broker_order_id: str | None = None
    status: Literal["OPEN", "PARTIAL", "FILLED", "REJECTED", "CANCELLED", "UNKNOWN"] = "OPEN"
    filled_qty: int = 0
    avg_price: float = 0.0
    message: str = ""


class OrderStatusReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    broker_order_id: str
    client_tag: str | None
    trading_symbol: str
    side: Side | None
    quantity: int
    filled_qty: int
    avg_price: float
    status: Literal["OPEN", "PARTIAL", "FILLED", "REJECTED", "CANCELLED", "UNKNOWN"]
    message: str = ""


class PositionReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    trading_symbol: str
    instrument_key: str | None = None
    net_qty: int
    avg_price: float


class FundsReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    available: float | None
    margin_used: float | None
    total: float | None          # available + used (account funds), the margin-risk denominator source
    ts: float


class TicketChecker:
    def __init__(self, verifier, venue_kind: str, clock_ts=time.time) -> None:
        self._verifier = verifier
        self.venue_kind = venue_kind
        self._ts = clock_ts
        self._used: set[str] = set()

    def check(self, ticket: SubmissionTicket | None, req: BrokerOrderRequest | None = None) -> None:
        if not isinstance(ticket, SubmissionTicket):
            raise PermissionDenied("order submission without a gateway ticket")
        if not self._verifier.verify(ticket.payload(), ticket.signature):
            raise PermissionDenied("invalid gateway ticket signature")
        if ticket.expires_at < self._ts():
            raise PermissionDenied("gateway ticket expired")
        if ticket.ticket_id in self._used:
            raise PermissionDenied("gateway ticket already used")
        if self.venue_kind == "LIVE":
            if ticket.mode == Mode.PAPER or ticket.environment != Environment.LIVE_CAPABLE:
                raise PaperIsolationViolation("a PAPER ticket reached a live order client")
        elif ticket.mode != Mode.PAPER:
            raise PermissionDenied("live ticket sent to the paper venue")
        if req is not None and (req.client_tag != ticket.client_tag or req.instrument_key != ticket.instrument_key or req.side != ticket.side
                                or req.quantity != ticket.quantity or req.account_id != ticket.account_id):
            raise PermissionDenied("order request does not match the gateway ticket")
        self._used.add(ticket.ticket_id)


class Venue(abc.ABC):
    name: str
    kind: str  # LIVE | PAPER
    account_id: str

    @abc.abstractmethod
    async def submit(self, req: BrokerOrderRequest, ticket: SubmissionTicket) -> BrokerAck: ...

    @abc.abstractmethod
    async def cancel(self, broker_order_id: str, ticket: SubmissionTicket) -> BrokerAck: ...

    @abc.abstractmethod
    async def order_book(self) -> list[OrderStatusReport]: ...

    @abc.abstractmethod
    async def positions(self) -> list[PositionReport]: ...

    @abc.abstractmethod
    async def funds(self) -> FundsReport: ...
