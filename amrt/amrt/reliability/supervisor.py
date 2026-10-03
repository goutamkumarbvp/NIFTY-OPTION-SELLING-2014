"""Reliability Control Plane: detect, contain, recover (allowlist only), verify, escalate.

The supervisor may only perform the actions in ALLOWLIST, each bound to the
component kinds it applies to. It can never submit, modify or cancel an order,
change mode, policy or limits, release a freeze, kill switch or recovery lock,
unquarantine a component, edit positions or delete records. Those are the
owner's (FORBIDDEN, enforced by the capability matrix: the RELIABILITY_PLANE
principal holds none of those capabilities).

Escalation ladder per failing component (one open incident per failure):
  attempt the component's allowlisted recovery actions with backoff (5 s, 15 s, 45 s);
  verify after each attempt (state HEALTHY and ready);
  exhausted → critical component: ENTER_READ_ONLY (freeze + recovery lock) + NOTIFY;
              non-critical component: QUARANTINE_COMPONENT + NOTIFY.
A critical component failure always sets the recovery lock, even if the
component recovers by itself: new risk stays blocked until the owner reviews
and releases it with a passing verify_recovery().
"""
from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from amrt.core.enums import HealthState
from amrt.core.ids import new_id
from amrt.security.identity import Capability, Principal, require
from amrt.storage.db import recovery_actions

log = logging.getLogger("amrt.supervisor")

ALLOWLIST: dict[str, frozenset[str]] = {
    "RECONNECT_STREAM": frozenset({"datasource", "broker"}),
    "RENEW_SESSION": frozenset({"broker", "datasource"}),
    "RESTART_WORKER": frozenset({"agent", "worker", "datasource", "service"}),
    "REBUILD_CACHE": frozenset({"cache"}),
    "REPLAY_CONSUMER": frozenset({"consumer"}),
    "RESTORE_CONFIG": frozenset({"config"}),
    "QUARANTINE_COMPONENT": frozenset({"agent", "datasource", "worker", "consumer", "notifier"}),
    "RECOMPUTE_ANALYTICS": frozenset({"analytics", "worker"}),
    "ENTER_READ_ONLY": frozenset({"*"}),
    "NOTIFY": frozenset({"*"}),
}
FORBIDDEN = ("SUBMIT_ORDER", "CANCEL_ORDER", "MODIFY_ORDER", "CHANGE_MODE", "CHANGE_RISK_POLICY", "RAISE_LIMIT", "RELEASE_FREEZE", "RELEASE_KILL_SWITCH",
             "RELEASE_RECOVERY_LOCK", "UNQUARANTINE", "EDIT_POSITIONS", "DELETE_RECORDS", "CHANGE_CREDENTIALS")
BACKOFF_S = (5.0, 15.0, 45.0)
FAILING = (HealthState.FAILED, HealthState.STALE)

Handler = Callable[[str], bool | Awaitable[bool]]


@dataclass
class Tracker:
    incident_id: str
    attempts: int = 0
    next_at: float = 0.0
    exhausted: bool = False
    actions: list[str] = field(default_factory=list)


class ReliabilitySupervisor:
    def __init__(self, principal: Principal, health, incidents, alerts, safety, events, db, clock, verifier: Callable[[], dict] | None = None) -> None:
        require(principal, Capability.RECOVERY_ALLOWLIST, "construct reliability supervisor")
        self.principal = principal
        self.health, self.incidents, self.alerts, self.safety, self.events, self.db, self.clock = health, incidents, alerts, safety, events, db, clock
        self.verifier = verifier
        self.handlers: dict[tuple[str, str], Handler] = {}
        self.plans: dict[str, list[str]] = {}
        self.trackers: dict[str, Tracker] = {}
        self.history: list[dict] = []

    # ---------------------------------------------------------------- setup
    def register(self, component: str, action: str, handler: Handler) -> None:
        if action not in ALLOWLIST:
            raise ValueError(f"{action} is not an allowlisted recovery action")
        c = self.health.components.get(component)
        if c is None:
            raise ValueError(f"unknown component {component}")
        kinds = ALLOWLIST[action]
        if "*" not in kinds and c.kind not in kinds:
            raise ValueError(f"{action} does not apply to {c.kind} components")
        if action == "RESTART_WORKER" and not c.restartable:
            raise ValueError(f"{component} is not declared restartable")
        self.handlers[(component, action)] = handler
        self.plans.setdefault(component, []).append(action)

    # ---------------------------------------------------------------- loop
    async def tick(self) -> list[dict]:
        done = []
        now = self.clock.ts()
        for c in list(self.health.components.values()):
            st = self.health.state(c.name)
            tr = self.trackers.get(c.name)
            if st in FAILING and not c.quarantined:
                if tr is None:
                    inc = self.incidents.open(c.name, "SEV2" if c.critical else "SEV3", f"{c.name} {st.value}",
                                              {"state": st.value, "last_error": c.last_error, "reason": c.reason}, self.principal)
                    tr = self.trackers[c.name] = Tracker(incident_id=inc["incident_id"], next_at=now)
                    self.alerts.emit("CRITICAL" if c.critical else "WARNING", "reliability", f"{c.name} is {st.value}", c.last_error or c.reason)
                    if c.critical:
                        self._read_only(c.name, tr, f"critical component {c.name} {st.value}")
                if tr.exhausted or now < tr.next_at:
                    continue
                plan = [a for a in self.plans.get(c.name, []) if a not in ("QUARANTINE_COMPONENT",)]
                if tr.attempts < min(len(BACKOFF_S), max(1, len(plan))) and plan:
                    action = plan[tr.attempts % len(plan)]
                    done.append(await self._attempt(c.name, action, tr))
                    tr.next_at = now + BACKOFF_S[min(tr.attempts, len(BACKOFF_S) - 1)]
                    tr.attempts += 1
                else:
                    tr.exhausted = True
                    if c.critical:
                        self._read_only(c.name, tr, f"recovery exhausted for {c.name}")
                        self.incidents.update(tr.incident_id, self.principal, "ESCALATED_TO_OWNER", status="ESCALATED", escalation="OWNER")
                    elif "QUARANTINE_COMPONENT" in self.plans.get(c.name, []) or c.kind in ALLOWLIST["QUARANTINE_COMPONENT"]:
                        done.append(await self._attempt(c.name, "QUARANTINE_COMPONENT", tr))
                        self.incidents.update(tr.incident_id, self.principal, "QUARANTINED", status="ESCALATED", escalation="OWNER")
                    self.alerts.emit("CRITICAL", "reliability", f"Owner action needed: {c.name}", f"automatic recovery exhausted after {tr.attempts} attempt(s)")
            elif tr is not None and st in (HealthState.HEALTHY, HealthState.DEGRADED) and self.health.readiness(c.name):
                self.incidents.update(tr.incident_id, self.principal, "COMPONENT_RECOVERED", status="RECOVERING",
                                      note="owner review required to resolve; latched safety state is unchanged")
                self._record(tr.incident_id, "VERIFY", c.name, {"state": st.value, "ready": True, "result": "RECOVERED"})
                del self.trackers[c.name]
        return done

    async def _attempt(self, component: str, action: str, tr: Tracker) -> dict:
        require(self.principal, Capability.RECOVERY_ALLOWLIST, f"recovery {action}")
        ok, err = False, ""
        try:
            if action == "QUARANTINE_COMPONENT":
                require(self.principal, Capability.QUARANTINE_COMPONENT, "quarantine")
                self.health.quarantine(component, "automatic recovery exhausted")
                ok = True
            else:
                res = self.handlers[(component, action)](component)
                ok = bool(await res) if inspect.isawaitable(res) else bool(res)
        except Exception as e:  # noqa: BLE001 - a failed recovery is recorded, never raised
            err = f"{type(e).__name__}: {e}"
        tr.actions.append(action)
        rec = self._record(tr.incident_id, action, component, {"attempt": tr.attempts + 1, "handler_ok": ok, "error": err})
        self.incidents.update(tr.incident_id, self.principal, f"RECOVERY_{action}", status="RECOVERING", handler_ok=ok, error=err)
        return rec

    def _read_only(self, component: str, tr: Tracker, reason: str) -> None:
        self.safety.freeze(self.principal, f"COMPONENT:{component}", reason)
        self.safety.set_recovery_lock(self.principal, tr.incident_id, reason)
        self._record(tr.incident_id, "ENTER_READ_ONLY", component, {"reason": reason})

    def _record(self, incident_id: str, action: str, component: str, record: dict) -> dict:
        rid = new_id("RA")
        row = {"action_id": rid, "incident_id": incident_id, "ts": self.clock.ts(), "action": action, "component": component, "actor": self.principal.id,
               "idempotency_key": f"{incident_id}:{action}:{rid}", "record": record}
        with self.db.tx() as conn:
            conn.execute(recovery_actions.insert().values(**row))
        self.events.append("RECOVERY_ACTION", {k: row[k] for k in ("action_id", "incident_id", "action", "component")} | {"record": record},
                           self.principal, correlation_id=incident_id)
        self.history.append(row)
        self.history = self.history[-500:]
        return row

    def describe(self) -> dict:
        return {"allowlist": {k: sorted(v) for k, v in ALLOWLIST.items()}, "forbidden": list(FORBIDDEN), "plans": self.plans,
                "active": {k: {"incident_id": t.incident_id, "attempts": t.attempts, "exhausted": t.exhausted, "actions": t.actions} for k, t in self.trackers.items()},
                "recent": self.history[-50:]}
