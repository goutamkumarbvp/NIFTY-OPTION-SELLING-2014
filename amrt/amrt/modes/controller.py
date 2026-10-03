"""Operating modes: PAPER (default at every start), MANUAL (owner control), AUTOMATIC (bounded).

* The process ALWAYS starts in PAPER mode; a previously active MANUAL/AUTOMATIC
  mode is recorded but never restored. Recovery never changes mode.
* Only the OWNER (CHANGE_MODE + fresh step-up) can change mode, and every
  transition re-verifies state first: health, reconciliation, unknown orders,
  safety latches, approved policies. Ambiguity fails safe (transition refused).
* PAPER → AUTOMATIC is not a valid transition; AUTOMATIC requires MANUAL first,
  an ACTIVE unexpired automation policy, a healthy Master Agent and the explicit
  confirmation phrase.
* Leaving a live mode for PAPER is refused while live positions or working live
  orders exist (they would otherwise be unmanaged).
"""
from __future__ import annotations

from amrt.core.enums import MODE_BANNER, Mode
from amrt.core.errors import InvalidTransition, NotReady, PermissionDenied
from amrt.security.identity import Capability, Principal, require

CONFIRM_AUTOMATIC = "ENABLE AUTOMATIC MODE"
STATE_KEY = "mode_state"


class ModeController:
    def __init__(self, db, events, clock, settings, system_principal: Principal) -> None:
        self.db, self.events, self.clock, self.settings = db, events, clock, settings
        prev = db.kv_get(STATE_KEY) if db is not None else None
        self.mode = Mode.PAPER
        self.since = clock.ts()
        self.changed_by = "startup"
        self.paper_auto_approve = False
        self.paper_auto_by: str | None = None
        self.history: list[dict] = list((prev or {}).get("history", []))[-50:]
        if prev and prev.get("mode") and prev["mode"] != Mode.PAPER.value:
            events.append("MODE_RESET_ON_STARTUP", {"previous_mode": prev["mode"], "now": "PAPER", "rule": "startup never restores a live mode"}, system_principal)
        self._save("startup")
        self.verifier = None      # set by the composition root: callable(target_mode) -> verification dict
        self.exposure = None      # callable() -> {"live_positions": n, "live_working": n}

    def _save(self, actor: str) -> None:
        if self.db is not None:
            self.db.kv_set(STATE_KEY, {"mode": self.mode.value, "since": self.since, "by": self.changed_by, "history": self.history[-50:]}, actor)

    def describe(self) -> dict:
        return {"mode": self.mode.value, "banner": MODE_BANNER[self.mode], "since": self.since, "changed_by": self.changed_by,
                "environment": self.settings.environment.value, "live_capable": self.settings.live_capable, "paper_auto_approve": self.paper_auto_approve,
                "history": self.history[-10:]}

    def transition(self, principal: Principal, target: Mode, step_up_ok: bool, confirmation: str = "", reason: str = "") -> dict:
        require(principal, Capability.CHANGE_MODE, "change mode")
        if not step_up_ok:
            raise PermissionDenied("mode changes need a fresh password re-entry (step-up)")
        cur = self.mode
        if target == cur:
            return self.describe()
        checks: dict = {"from": cur.value, "to": target.value}
        if target == Mode.PAPER:
            exp = self.exposure() if self.exposure else {"live_positions": 0, "live_working": 0}
            checks["exposure"] = exp
            if exp["live_positions"] or exp["live_working"]:
                raise InvalidTransition("live positions or working live orders exist — exit them (Manual Quick Exit) before switching to PAPER", **exp)
        else:
            if not self.settings.live_capable:
                raise NotReady("deployment is not LIVE_CAPABLE (AMRT_ENVIRONMENT / AMRT_LIVE_ORDERS_ENABLED)")
            if target == Mode.AUTOMATIC:
                if cur != Mode.MANUAL:
                    raise InvalidTransition("AUTOMATIC can only be entered from MANUAL")
                if confirmation.strip() != CONFIRM_AUTOMATIC:
                    raise PermissionDenied(f"type the confirmation phrase '{CONFIRM_AUTOMATIC}'")
            ver = self.verifier(target) if self.verifier else {"passed": False, "failed": ["no verifier"]}
            checks["verification"] = ver
            if not ver.get("passed"):
                raise NotReady("readiness verification failed", failed=ver.get("failed"))
        self.history.append({"ts": self.clock.ts(), "from": cur.value, "to": target.value, "by": principal.id, "reason": reason})
        self.mode, self.since, self.changed_by = target, self.clock.ts(), principal.id
        self._save(principal.id)
        self.events.append("MODE_CHANGED", {**checks, "reason": reason, "banner": MODE_BANNER[target]}, principal)
        return self.describe()

    def demote_to_manual(self, principal: Principal, reason: str) -> bool:
        """De-escalation AUTOMATIC → MANUAL (owner only). Safety components freeze/suspend instead of changing mode."""
        require(principal, Capability.CHANGE_MODE, "demote to manual")
        if self.mode != Mode.AUTOMATIC:
            return False
        self.history.append({"ts": self.clock.ts(), "from": "AUTOMATIC", "to": "MANUAL", "by": principal.id, "reason": reason})
        self.mode, self.since, self.changed_by = Mode.MANUAL, self.clock.ts(), principal.id
        self._save(principal.id)
        self.events.append("MODE_CHANGED", {"from": "AUTOMATIC", "to": "MANUAL", "reason": reason}, principal)
        return True

    def set_paper_auto_approve(self, principal: Principal, on: bool) -> None:
        require(principal, Capability.APPROVE_ACTION, "paper auto-approve")
        self.paper_auto_approve = bool(on)
        self.paper_auto_by = principal.id if on else None
        self.events.append("PAPER_AUTO_APPROVE", {"on": self.paper_auto_approve, "scope": "PAPER accounts only"}, principal)
