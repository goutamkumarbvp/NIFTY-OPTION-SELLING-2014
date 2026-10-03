"""Component health registry.

Each component reports a heartbeat (liveness), readiness (can serve) and safety
readiness (safe to rely on for new risk) separately — a running process is not
necessarily ready, and a ready one is not necessarily safe. Quarantine overrides
everything and only the owner clears it.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from amrt.core.enums import HealthState


@dataclass
class Component:
    name: str
    critical: bool
    period_s: float                       # expected heartbeat period
    kind: str = "service"                 # service | agent | broker | datasource | infra
    restartable: bool = False             # isolated, stateless worker the supervisor may restart
    last_beat: float = 0.0
    state: HealthState = HealthState.UNKNOWN
    ready: bool = False
    safety_ready: bool = False
    reason: str = ""
    last_error: str = ""
    quarantined: bool = False
    quarantine_reason: str = ""
    failures: int = 0
    meta: dict = field(default_factory=dict)


class HealthRegistry:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.components: dict[str, Component] = {}
        self._lock = threading.RLock()

    def register(self, name: str, critical: bool, period_s: float, kind: str = "service", restartable: bool = False) -> Component:
        with self._lock:
            c = self.components.get(name) or Component(name=name, critical=critical, period_s=period_s, kind=kind, restartable=restartable)
            self.components[name] = c
            return c

    def beat(self, name: str, state: HealthState = HealthState.HEALTHY, ready: bool = True, safety_ready: bool = True, reason: str = "", **meta) -> None:
        with self._lock:
            c = self.components.get(name) or self.register(name, False, 10.0)
            c.last_beat = self.clock.ts()
            c.state, c.ready, c.safety_ready, c.reason = state, ready, safety_ready, reason
            if state == HealthState.HEALTHY:
                c.failures = 0
            if meta:
                c.meta.update(meta)

    def fail(self, name: str, error: str, state: HealthState = HealthState.FAILED) -> None:
        with self._lock:
            c = self.components.get(name) or self.register(name, False, 10.0)
            c.state, c.ready, c.safety_ready, c.last_error = state, False, False, error[:500]
            c.failures += 1
            c.last_beat = self.clock.ts()

    def quarantine(self, name: str, reason: str) -> None:
        with self._lock:
            c = self.components[name]
            c.quarantined, c.quarantine_reason = True, reason

    def unquarantine(self, name: str) -> None:
        with self._lock:
            c = self.components[name]
            c.quarantined, c.quarantine_reason, c.failures = False, "", 0

    def state(self, name: str) -> HealthState:
        c = self.components.get(name)
        if c is None:
            return HealthState.UNKNOWN
        if c.quarantined:
            return HealthState.QUARANTINED
        if c.last_beat == 0.0:
            return HealthState.UNKNOWN
        age = self.clock.ts() - c.last_beat
        if c.state in (HealthState.HEALTHY, HealthState.DEGRADED) and age > max(3 * c.period_s, 5.0):
            return HealthState.STALE if age < max(10 * c.period_s, 30.0) else HealthState.FAILED
        return c.state

    def liveness(self, name: str) -> bool:
        c = self.components.get(name)
        return bool(c and c.last_beat and self.clock.ts() - c.last_beat <= max(3 * c.period_s, 5.0))

    def readiness(self, name: str) -> bool:
        c = self.components.get(name)
        return bool(c and c.ready and self.state(name) in (HealthState.HEALTHY, HealthState.DEGRADED))

    def safety_readiness(self, name: str) -> bool:
        c = self.components.get(name)
        return bool(c and c.safety_ready and self.state(name) == HealthState.HEALTHY)

    def describe(self) -> list[dict]:
        out = []
        for c in self.components.values():
            out.append({"name": c.name, "kind": c.kind, "critical": c.critical, "state": self.state(c.name).value, "liveness": self.liveness(c.name),
                        "readiness": self.readiness(c.name), "safety_readiness": self.safety_readiness(c.name), "reason": c.reason,
                        "last_error": c.last_error, "quarantined": c.quarantined, "quarantine_reason": c.quarantine_reason, "failures": c.failures,
                        "last_beat_age_s": round(self.clock.ts() - c.last_beat, 1) if c.last_beat else None, "restartable": c.restartable, "meta": c.meta})
        return sorted(out, key=lambda d: (not d["critical"], d["name"]))
