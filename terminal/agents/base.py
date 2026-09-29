"""Agent framework: every agent observes a shared MarketContext and returns an
Assessment (stance, score, confidence, findings, optional veto)."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from terminal.core.models import Assessment, OptionChain, RiskSnapshot, StrategyRun, TerminalMode


@dataclass
class MarketContext:
    underlying: str
    exchange: str
    spot: float
    vix: Optional[float]
    vix_rank: Dict[str, Any]
    chain: Optional[OptionChain]
    indicators: Dict[str, Any]
    pcr_history: List[dict]
    risk: RiskSnapshot
    active_runs: List[StrategyRun]
    mode: TerminalMode
    session: str
    can_enter: bool
    can_enter_reason: str
    is_expiry_day: bool
    days_to_expiry: float
    sigma_move_5m: Optional[float]
    feed_fresh: bool
    memory: Dict[str, Any] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


class Agent(ABC):
    name: str = "agent"
    role: str = ""
    weight: float = 1.0
    enabled: bool = True

    def __init__(self, terminal) -> None:
        self.t = terminal
        self.last: Optional[Assessment] = None
        self.runs = 0
        self.failures = 0

    async def run(self, ctx: MarketContext) -> Assessment:
        t0 = time.perf_counter()
        try:
            a = await self.assess(ctx)
        except Exception as exc:  # fail closed: an erroring agent abstains, never votes to trade
            self.failures += 1
            a = Assessment(agent=self.name, role=self.role, stance="ERROR", score=0.0, confidence=0.0, findings=[f"agent error: {exc}"])
        a.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        a.agent, a.role = self.name, self.role
        self.last = a
        self.runs += 1
        return a

    @abstractmethod
    async def assess(self, ctx: MarketContext) -> Assessment: ...

    def status(self) -> dict:
        return {"name": self.name, "role": self.role, "weight": self.weight, "enabled": self.enabled, "runs": self.runs, "failures": self.failures,
                "last": self.last.model_dump(mode="json") if self.last else None}
