"""Capability-aware broker integration.

Separation of powers inside every broker integration:
* `BrokerSession` holds the SDK client and credentials privately. It is built
  only by the BrokerManager under the Execution Gateway's identity.
* `MarketDataReader` / `AccountReader` are read-only facades given to the market
  data loop and the reconciler. They expose no order methods.
* `LiveBrokerVenue` is the only object that can call order endpoints. It is
  constructed only when the deployment is LIVE_CAPABLE and verifies a
  Gateway-signed SubmissionTicket on every call.
Responses are normalised by pure parse functions per broker (unit-tested with
documented response shapes). Unknown statuses map to UNKNOWN, never to a guess.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from amrt.core.enums import FeatureStatus
from amrt.core.errors import BrokerRejected, BrokerTimeout, CapabilityUnavailable
from amrt.execution.intents import BrokerOrderRequest, SubmissionTicket
from amrt.execution.venue import BrokerAck, FundsReport, OrderStatusReport, PositionReport, TicketChecker, Venue
from amrt.marketdata.models import ChainSnapshot, Quote
from amrt.security.identity import Capability, Principal, require

log = logging.getLogger("amrt.brokers")


class CapabilityEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    feature: str
    status: FeatureStatus
    evidence: str
    notes: str = ""


class BrokerProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    display: str
    sdk_package: str
    sdk_version_tested: str
    official_docs: str
    api_version: str
    auth: str
    instruments: list[str]
    order_types: list[str]
    market_data: str
    option_chain: str
    historical_data: str
    websocket: str
    rate_limits: str
    sandbox: str
    tag_field: str
    tag_max_len: int
    known_limitations: list[str] = Field(default_factory=list)
    capabilities: list[CapabilityEntry]
    verification_date: str
    overall_status: FeatureStatus


def num(v: Any, default: float | None = None) -> float | None:
    if v in (None, "", "NA", "-", "null"):
        return default
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def pick(d: dict, *names: str, default: Any = None) -> Any:
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", "NA"):
            return v
    return default


class BrokerSession(abc.ABC):
    """Owns the SDK client and the credentials. Never handed to agents or the API layer."""
    name: str
    profile: BrokerProfile

    def __init__(self, settings, clock, sdk_factory: Callable[..., Any] | None = None) -> None:
        self.settings = settings
        self.clock = clock
        self._sdk_factory = sdk_factory
        self.__client: Any = None
        self.authenticated = False
        self.last_error = ""
        self.logged_in_at: float | None = None
        self.calls = 0
        self.errors = 0

    # subclasses use these two accessors; nothing outside the session sees the client
    def _set_client(self, c: Any) -> None:
        self.__client = c

    def _client(self) -> Any:
        if self.__client is None:
            raise CapabilityUnavailable(f"{self.name}: not logged in")
        return self.__client

    @abc.abstractmethod
    def credentials_present(self) -> bool: ...

    @abc.abstractmethod
    def _login_sync(self) -> dict: ...

    async def login(self, timeout: float = 20.0) -> dict:
        try:
            res = await asyncio.wait_for(asyncio.to_thread(self._login_sync), timeout)
            self.authenticated, self.logged_in_at, self.last_error = True, self.clock.ts(), ""
            return res
        except Exception as exc:  # noqa: BLE001
            self.authenticated = False
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise

    async def call(self, fn_name: str, *args, timeout: float = 10.0, **kwargs) -> Any:
        """Run an SDK method in a worker thread with a timeout. Timeouts raise BrokerTimeout (outcome unknown)."""
        client = self._client()
        fn = getattr(client, fn_name)
        self.calls += 1
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout)
        except TimeoutError as exc:
            self.errors += 1
            raise BrokerTimeout(f"{self.name}.{fn_name} timed out after {timeout}s") from exc
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise

    def status(self) -> dict:
        return {"broker": self.name, "authenticated": self.authenticated, "credentials_present": self.credentials_present(), "last_error": self.last_error,
                "logged_in_at": self.logged_in_at, "calls": self.calls, "errors": self.errors}

    # ---- read API implemented per broker (pure parsing in module functions) ----
    @abc.abstractmethod
    async def fetch_chain(self, underlying: str, expiry_iso: str, instruments) -> tuple[ChainSnapshot, Quote | None]: ...

    @abc.abstractmethod
    async def fetch_expiries(self, underlying: str) -> list[str]: ...

    @abc.abstractmethod
    async def fetch_quotes(self, instruments: list) -> list[Quote]: ...

    @abc.abstractmethod
    async def fetch_order_book(self) -> list[OrderStatusReport]: ...

    @abc.abstractmethod
    async def fetch_positions(self) -> list[PositionReport]: ...

    @abc.abstractmethod
    async def fetch_funds(self) -> FundsReport: ...

    # ---- order API (called only through LiveBrokerVenue) ----
    @abc.abstractmethod
    async def _place(self, req: BrokerOrderRequest) -> BrokerAck: ...

    @abc.abstractmethod
    async def _cancel(self, broker_order_id: str) -> BrokerAck: ...


class MarketDataReader:
    """Read-only facade for the market data loop."""

    def __init__(self, session: BrokerSession) -> None:
        self.name = session.name
        self.fetch_chain = session.fetch_chain
        self.fetch_expiries = session.fetch_expiries
        self.fetch_quotes = session.fetch_quotes
        self.status = session.status
        self.login = session.login

    @property
    def authenticated(self) -> bool:
        return self._auth()

    def bind_auth(self, fn) -> None:
        self._auth = fn


class LiveBrokerVenue(Venue):
    kind = "LIVE"

    def __init__(self, principal: Principal, session: BrokerSession, account_id: str, gateway_verifier, clock) -> None:
        require(principal, Capability.HOLD_BROKER_CREDENTIALS, "construct live venue")
        self.name = session.name
        self.account_id = account_id
        self._session = session
        self.tag_len = session.profile.tag_max_len
        self.checker = TicketChecker(gateway_verifier, "LIVE", clock.ts)

    async def submit(self, req: BrokerOrderRequest, ticket: SubmissionTicket) -> BrokerAck:
        self.checker.check(ticket, req)
        return await self._session._place(req)

    async def cancel(self, broker_order_id: str, ticket: SubmissionTicket) -> BrokerAck:
        self.checker.check(ticket)
        return await self._session._cancel(broker_order_id)

    async def order_book(self) -> list[OrderStatusReport]:
        return await self._session.fetch_order_book()

    async def positions(self) -> list[PositionReport]:
        return await self._session.fetch_positions()

    async def funds(self) -> FundsReport:
        return await self._session.fetch_funds()


def classify_place_error(exc: Exception) -> Exception:
    """A broker exception that clearly says the order was refused is a rejection; anything else is UNKNOWN."""
    msg = str(exc).lower()
    definitive = ("insufficient", "rejected", "invalid", "not allowed", "rms", "margin", "blocked", "freeze quantity", "price band", "outside")
    if any(w in msg for w in definitive) and not any(w in msg for w in ("timeout", "timed out", "connection", "gateway", "503", "502", "504")):
        return BrokerRejected(str(exc)[:300])
    return BrokerTimeout(f"ambiguous broker error: {str(exc)[:300]}")


def normalise_status(raw: str, filled: int, qty: int, table: dict[str, str]) -> str:
    s = (raw or "").strip().lower()
    st = table.get(s)
    if st is None:
        for k, v in table.items():
            if k and k in s:
                st = v
                break
    if st is None:
        return "UNKNOWN"
    if st == "OPEN" and 0 < filled < qty:
        return "PARTIAL"
    if st == "OPEN" and qty and filled >= qty:
        return "FILLED"
    return st
