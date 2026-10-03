"""Agent contracts.

Agents are advisory. They receive an immutable AgentContext — plain data copied
out of the services — and return an AgentOutput. They hold no handle to the
gateway, brokers, kill switch, policies or database, and an AST test enforces
that no module under amrt.agents imports amrt.execution, amrt.brokers,
amrt.risk.emergency, amrt.risk.kernel or amrt.storage.

Every output separates observations (numbers read from data, with source and
time) from inferences (labelled interpretations), states its confidence method,
and lists the conditions that would invalidate it.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from amrt.core.enums import DataLabel, Stance
from amrt.core.ids import digest
from amrt.security.identity import Capability, Principal, PrincipalKind, require
from amrt.security.untrusted import UntrustedText

OUTPUT_SCHEMA = "agent-output/1"
AgentStatus = Literal["OK", "DEGRADED", "INSUFFICIENT_DATA", "ERROR", "QUARANTINED", "TIMEOUT"]


class ProposedLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    instrument_key: str
    side: Literal["BUY", "SELL"]
    lots: int = Field(gt=0)
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    limit_price: float | None = None
    purpose: Literal["ENTRY", "EXIT", "PROTECTIVE", "ADJUSTMENT", "HEDGE"] = "ENTRY"
    reduce_only: bool = False
    strike: float | None = None
    option_type: Literal["CE", "PE"] | None = None
    bid: float | None = None
    ask: float | None = None
    ltp: float | None = None


class ProposedAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["ENTRY", "EXIT", "ROLL"]
    strategy_id: str
    strategy_version: str
    underlying: str
    exchange: str
    expiry: str
    legs: list[ProposedLeg]
    metrics: dict[str, Any] = Field(default_factory=dict)
    est_charges_inr: float | None = None
    rationale: list[str] = Field(default_factory=list)

    @field_validator("legs")
    @classmethod
    def _legs(cls, v: list[ProposedLeg]) -> list[ProposedLeg]:
        if not v:
            raise ValueError("a proposed action needs at least one leg")
        return v


class AgentOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: str = OUTPUT_SCHEMA
    agent: str
    version: str
    principal_id: str
    status: AgentStatus
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_method: str
    observations: dict[str, Any] = Field(default_factory=dict)
    inferences: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    invalidation: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    data_as_of: float | None = None
    data_label: str = DataLabel.UNAVAILABLE.value
    input_digest: str = ""
    proposed_action: ProposedAction | None = None
    errors: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0
    created_at: float = 0.0


@dataclass(frozen=True)
class AgentContext:
    """Read-only snapshot for one decision cycle on one underlying and one account."""
    now_ts: float
    now_ist: dt.datetime
    mode: str
    account_id: str
    account_kind: str                         # PAPER | LIVE
    underlying: str
    exchange: str
    lot_size: int
    chain: Any = None                         # ChainSnapshot | None
    chain_age_ms: float | None = None
    chain_label: str = DataLabel.UNAVAILABLE.value
    analytics: Any = None                     # ChainAnalytics | None
    windows: dict = field(default_factory=dict)
    spot_history: tuple = ()                  # ((ts, spot), ...) oldest first
    positions: tuple = ()                     # position dicts for this account
    valuation: dict | None = None
    funds: dict = field(default_factory=dict)
    policy: dict | None = None                # active risk policy (model_dump)
    policy_label: str = ""
    safety: dict = field(default_factory=dict)
    health: tuple = ()
    brokers: dict = field(default_factory=dict)
    reconcile: dict = field(default_factory=dict)
    fii_participant: tuple = ()
    fii_cash: tuple = ()
    news: tuple[UntrustedText, ...] = ()
    news_source_configured: bool = False
    events_calendar: tuple = ()               # ({"date","name","source"}, ...) from a configured file only
    security: dict = field(default_factory=dict)
    strategies: tuple = ()                    # StrategySpec enabled for proposals
    backtests: dict = field(default_factory=dict)   # strategy_id -> validated summary
    max_data_age_ms: float = 3000.0

    def digest(self) -> str:
        return digest({"now": round(self.now_ts, 3), "mode": self.mode, "acct": self.account_id, "und": self.underlying,
                       "chain_ts": getattr(self.chain, "ts", None), "spot": getattr(self.chain, "spot", None),
                       "positions": list(self.positions), "pnl": (self.valuation or {}).get("net_pnl_today"),
                       "safety": self.safety, "policy": self.policy_label})


class Agent:
    name: str = "agent"
    version: str = "1.0"
    kind: PrincipalKind = PrincipalKind.SPECIALIST_AGENT
    critical: bool = False                   # a failure of a critical agent makes the package INSUFFICIENT DATA
    uses_market_data: bool = True

    def __init__(self) -> None:
        self.principal = Principal.of(self.kind, f"agent:{self.name}")

    def analyze(self, ctx: AgentContext, peers: dict[str, AgentOutput] | None = None) -> dict:
        raise NotImplementedError

    def _base(self, ctx: AgentContext) -> dict:
        return {"agent": self.name, "version": self.version, "principal_id": self.principal.id, "input_digest": ctx.digest(),
                "data_label": ctx.chain_label if self.uses_market_data else DataLabel.UNAVAILABLE.value,
                "data_as_of": getattr(ctx.chain, "ts", None) if self.uses_market_data else None,
                "data_sources": [getattr(ctx.chain, "source", "")] if self.uses_market_data and ctx.chain is not None else []}

    def run(self, ctx: AgentContext, peers: dict[str, AgentOutput] | None = None) -> AgentOutput:
        require(self.principal, Capability.PUBLISH_ADVICE, f"{self.name} publish advice")
        t0 = time.perf_counter()
        base = self._base(ctx)
        try:
            body = self.analyze(ctx, peers)
            if body.get("proposed_action") is not None:
                require(self.principal, Capability.PROPOSE_STRATEGY_ACTION, f"{self.name} propose action")
            out = AgentOutput(**{**base, **body, "latency_ms": round((time.perf_counter() - t0) * 1000, 3), "created_at": ctx.now_ts})
        except Exception as e:  # an agent failure is contained and reported, never raised into the Master
            out = AgentOutput(**base, status="ERROR", stance=Stance.INSUFFICIENT_DATA, confidence=0.0, confidence_method="agent failed",
                              errors=[f"{type(e).__name__}: {e}"], latency_ms=round((time.perf_counter() - t0) * 1000, 3), created_at=ctx.now_ts)
        return out


def insufficient(reason: str, **extra) -> dict:
    return {"status": "INSUFFICIENT_DATA", "stance": Stance.INSUFFICIENT_DATA, "confidence": 0.0, "confidence_method": "no usable data",
            "warnings": [reason], **extra}


def synthetic(agent: Agent, ctx: AgentContext, status: AgentStatus, reason: str) -> AgentOutput:
    """Output recorded for an agent that did not run (quarantined or timed out)."""
    return AgentOutput(agent=agent.name, version=agent.version, principal_id=agent.principal.id, status=status, stance=Stance.INSUFFICIENT_DATA,
                       confidence=0.0, confidence_method="agent did not run", errors=[reason], input_digest=ctx.digest(), created_at=ctx.now_ts)
