"""Persistent, latched safety state shared by the Risk Kernel, monitors, watchdog and gateway.

Dual kill switch
* Switch A lives in the database and is evaluated by the Risk Kernel.
* Switch B is a file on local disk checked by the Execution Gateway directly
  before every submission — it works even if the kernel, database, agents or
  supervisor fail, and the thread-based watchdog can set it without the event loop.
Engaging sets both. Releasing needs the OWNER, a fresh step-up and a passing
recovery verification; nothing automatic can release it.

Latches (persist across restarts; only the owner clears them):
* freeze_new_risk — reasons accumulate; reducing/protective orders stay allowed.
* emergency level — NONE → LEVEL1 → LEVEL2, only rises automatically.
* recovery_lock — set after a safety-critical incident until recovery is verified.
* ai_suspended — set when the Master Agent or a critical AI dependency fails.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from amrt.core.errors import NotReady, PermissionDenied
from amrt.security.identity import Capability, Principal, require

STATE_KEY = "safety_state"
LEVELS = ["NONE", "LEVEL1", "LEVEL2"]


def _empty() -> dict[str, Any]:
    return {"kill_switch": {"engaged": False}, "freeze": {"active": False, "reasons": []}, "emergency": {"level": "NONE"},
            "recovery_lock": {"active": False}, "ai_suspended": {"active": False}}


class SafetyState:
    def __init__(self, db, events, kill_file: Path, clock_ts=time.time) -> None:
        self.db = db
        self.events = events
        self.kill_file = Path(kill_file)
        self._ts = clock_ts
        self._lock = threading.RLock()
        stored = db.kv_get(STATE_KEY) if db is not None else None
        self.state: dict[str, Any] = stored or _empty()
        # reconcile the two switches at startup: if either is engaged, both are
        if self.kill_file_engaged() and not self.state["kill_switch"]["engaged"]:
            self.state["kill_switch"] = {"engaged": True, "by": "kill-file", "at": self._ts(), "reason": "kill file present at startup", "variant": "FREEZE"}
            self._save("startup")
        elif self.state["kill_switch"]["engaged"] and not self.kill_file_engaged():
            self._write_kill_file(self.state["kill_switch"])

    # ---------------------------------------------------------------- io
    def _save(self, actor: str) -> None:
        if self.db is not None:
            self.db.kv_set(STATE_KEY, self.state, actor)

    def _write_kill_file(self, info: dict) -> None:
        self.kill_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.kill_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, default=str))
        os.replace(tmp, self.kill_file)

    def kill_file_engaged(self) -> bool:
        return self.kill_file.exists()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = json.loads(json.dumps(self.state, default=str))
        s["kill_switch"]["file_engaged"] = self.kill_file_engaged()
        return s

    # ------------------------------------------------------------ kill switch
    def engage_kill(self, principal: Principal, reason: str, variant: str = "FREEZE") -> dict:
        require(principal, Capability.ENGAGE_KILL_SWITCH, "engage kill switch")
        info = {"engaged": True, "by": principal.id, "kind": principal.kind.value, "at": self._ts(), "reason": reason, "variant": variant}
        self._write_kill_file(info)          # switch B first: it must hold even if the database write fails
        with self._lock:
            self.state["kill_switch"] = info
            self._add_freeze("KILL_SWITCH", principal, reason)
            try:
                self._save(principal.id)
            finally:
                self._event("KILL_SWITCH_ENGAGED", info, principal)
        return info

    def release_kill(self, principal: Principal, verification: dict) -> None:
        require(principal, Capability.RELEASE_KILL_SWITCH, "release kill switch")
        if not verification.get("passed"):
            raise NotReady("recovery verification did not pass", failed=verification.get("failed"))
        with self._lock:
            self.state["kill_switch"] = {"engaged": False, "released_by": principal.id, "released_at": self._ts()}
            self._remove_freeze({"KILL_SWITCH"})
            self._save(principal.id)
        if self.kill_file.exists():
            self.kill_file.unlink()
        self._event("KILL_SWITCH_RELEASED", {"verification": verification}, principal)

    # ---------------------------------------------------------------- freeze
    def _add_freeze(self, code: str, principal: Principal, detail: str) -> bool:
        f = self.state["freeze"]
        if any(r["code"] == code for r in f["reasons"]):
            return False
        f["reasons"].append({"code": code, "by": principal.id, "kind": principal.kind.value, "at": self._ts(), "detail": detail})
        if not f["active"]:
            f["active"], f["since"] = True, self._ts()
        return True

    def _remove_freeze(self, codes: set[str] | None) -> None:
        f = self.state["freeze"]
        f["reasons"] = [] if codes is None else [r for r in f["reasons"] if r["code"] not in codes]
        if not f["reasons"]:
            f["active"] = False
            f.pop("since", None)

    def freeze(self, principal: Principal, code: str, detail: str) -> bool:
        require(principal, Capability.FREEZE_NEW_RISK, "freeze new risk")
        with self._lock:
            added = self._add_freeze(code, principal, detail)
            if added:
                self._save(principal.id)
        if added:
            self._event("NEW_RISK_FROZEN", {"code": code, "detail": detail}, principal)
        return added

    def release_freeze(self, principal: Principal, codes: list[str] | None, verification: dict) -> None:
        require(principal, Capability.RELEASE_FREEZE, "release freeze")
        if not verification.get("passed"):
            raise NotReady("recovery verification did not pass", failed=verification.get("failed"))
        with self._lock:
            if self.state["kill_switch"]["engaged"] and (codes is None or "KILL_SWITCH" in codes):
                raise PermissionDenied("release the kill switch through its own control")
            self._remove_freeze(set(codes) if codes else {r["code"] for r in self.state["freeze"]["reasons"] if r["code"] != "KILL_SWITCH"})
            if not self.state["freeze"]["active"] and self.state["emergency"]["level"] != "NONE":
                self.state["emergency"] = {"level": "NONE", "cleared_by": principal.id, "cleared_at": self._ts()}
            self._save(principal.id)
        self._event("NEW_RISK_UNFROZEN", {"codes": codes, "verification": verification}, principal)

    # -------------------------------------------------------------- emergency
    def raise_emergency(self, principal: Principal, level: str, reason: str) -> bool:
        require(principal, Capability.FREEZE_NEW_RISK, "raise emergency")
        with self._lock:
            cur = self.state["emergency"].get("level", "NONE")
            if LEVELS.index(level) <= LEVELS.index(cur):
                return False
            self.state["emergency"] = {"level": level, "since": self._ts(), "reason": reason, "by": principal.id}
            self._add_freeze(f"EMERGENCY_{level}", principal, reason)
            self._save(principal.id)
        self._event("EMERGENCY_RAISED", {"level": level, "reason": reason}, principal)
        return True

    # ---------------------------------------------------------- recovery lock
    def set_recovery_lock(self, principal: Principal, incident_id: str, reason: str) -> None:
        require(principal, Capability.FREEZE_NEW_RISK, "recovery lock")
        with self._lock:
            if self.state["recovery_lock"].get("active"):
                return
            self.state["recovery_lock"] = {"active": True, "incident_id": incident_id, "since": self._ts(), "reason": reason, "by": principal.id}
            self._add_freeze("RECOVERY_LOCK", principal, reason)
            self._save(principal.id)
        self._event("RECOVERY_LOCK_SET", {"incident_id": incident_id, "reason": reason}, principal)

    def release_recovery_lock(self, principal: Principal, verification: dict) -> None:
        require(principal, Capability.RELEASE_FREEZE, "release recovery lock")
        if not verification.get("passed"):
            raise NotReady("recovery verification did not pass", failed=verification.get("failed"))
        with self._lock:
            self.state["recovery_lock"] = {"active": False, "released_by": principal.id, "released_at": self._ts()}
            self._remove_freeze({"RECOVERY_LOCK"})
            self._save(principal.id)
        self._event("RECOVERY_LOCK_RELEASED", {"verification": verification}, principal)

    # -------------------------------------------------------------- AI suspend
    def suspend_ai(self, principal: Principal, reason: str) -> bool:
        require(principal, Capability.FREEZE_NEW_RISK, "suspend AI actions")
        with self._lock:
            if self.state["ai_suspended"].get("active"):
                return False
            self.state["ai_suspended"] = {"active": True, "since": self._ts(), "reason": reason, "by": principal.id}
            self._save(principal.id)
        self._event("AI_ACTIONS_SUSPENDED", {"reason": reason}, principal)
        return True

    def resume_ai(self, principal: Principal) -> None:
        require(principal, Capability.RELEASE_FREEZE, "resume AI actions")
        with self._lock:
            self.state["ai_suspended"] = {"active": False, "resumed_by": principal.id, "resumed_at": self._ts()}
            self._save(principal.id)
        self._event("AI_ACTIONS_RESUMED", {}, principal)

    # ------------------------------------------------------------------ views
    def new_risk_blockers(self) -> list[str]:
        s = self.state
        out = []
        if s["kill_switch"].get("engaged") or self.kill_file_engaged():
            out.append("KILL_SWITCH")
        if s["freeze"].get("active"):
            out += [f"FREEZE:{r['code']}" for r in s["freeze"]["reasons"]]
        if s["recovery_lock"].get("active"):
            out.append("RECOVERY_LOCK")
        return sorted(set(out))

    def _event(self, type_: str, payload: dict, principal: Principal) -> None:
        if self.events is not None:
            try:
                self.events.append(type_, payload, principal)
            except Exception:
                pass
