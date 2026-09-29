"""Four-eyes governance for control-plane changes.

When enabled (default: on in LIVE), risk-limit edits, switching to AUTO, opening the
safety gate and Guardian policy changes are *proposed* by one operator and must be
approved by a different admin before they take effect. Every proposal, approval and
rejection is audited and a config-history row records old → new values.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, List

from pydantic import BaseModel, Field

from terminal.core.models import new_id

GOVERNED = {"risk_limits": "Risk limit change", "mode_auto": "Switch terminal to AUTO", "gate_open": "Open the safety gate", "guardian_policy": "Guardian auto-apply policy"}


class ChangeRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("CHG"))
    ts: float = Field(default_factory=time.time)
    action: str
    label: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    proposed_by: str
    status: str = "PENDING"  # PENDING | APPROVED | REJECTED | EXPIRED | FAILED
    decided_by: str = ""
    decided_at: float | None = None
    result: str = ""
    expires_at: float = Field(default_factory=lambda: time.time() + 3600)


class Governance:
    def __init__(self, terminal) -> None:
        self.t = terminal
        s = terminal.settings
        self.enabled = bool(s.four_eyes_required) if s.four_eyes_required is not None else (s.trading_env == "LIVE")
        self.requests: List[ChangeRequest] = []
        self._executors: Dict[str, Callable[[Dict[str, Any], str], Awaitable[Any]]] = {}

    def register(self, action: str, fn: Callable[[Dict[str, Any], str], Awaitable[Any]]) -> None:
        self._executors[action] = fn

    def requires_approval(self, action: str) -> bool:
        return self.enabled and action in GOVERNED

    async def submit(self, action: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Apply immediately when not governed, else park it for a second admin."""
        if action not in self._executors:
            raise KeyError(f"UNKNOWN_ACTION:{action}")
        if not self.requires_approval(action):
            res = await self._executors[action](payload, actor)
            return {"applied": True, "result": res}
        req = ChangeRequest(action=action, label=GOVERNED[action], payload=payload, proposed_by=actor)
        self.requests.append(req)
        self.t.audit.record("CHANGE_PROPOSED", {"id": req.id, "action": action, "payload": payload}, actor)
        await self.t.alerts.emit("WARNING", "governance", f"Approval needed: {req.label}", f"Proposed by {actor}. A different admin must approve in Approvals → Governance.", dedupe_seconds=0)
        return {"applied": False, "request": req.model_dump(mode="json")}

    async def approve(self, request_id: str, actor: str) -> ChangeRequest:
        req = self.get(request_id)
        if req.status != "PENDING":
            raise ValueError(f"REQUEST_NOT_PENDING:{req.status}")
        if time.time() > req.expires_at:
            req.status = "EXPIRED"
            raise ValueError("REQUEST_EXPIRED")
        if actor == req.proposed_by:
            raise ValueError("FOUR_EYES_SAME_USER")
        try:
            res = await self._executors[req.action](req.payload, actor)
            req.status, req.result = "APPROVED", str(res)[:200]
        except Exception as exc:
            req.status, req.result = "FAILED", f"{type(exc).__name__}: {exc}"[:200]
        req.decided_by, req.decided_at = actor, time.time()
        self.t.audit.record("CHANGE_" + req.status, {"id": req.id, "action": req.action, "payload": req.payload, "proposed_by": req.proposed_by}, actor)
        return req

    def reject(self, request_id: str, actor: str, reason: str = "") -> ChangeRequest:
        req = self.get(request_id)
        if req.status != "PENDING":
            raise ValueError(f"REQUEST_NOT_PENDING:{req.status}")
        req.status, req.decided_by, req.decided_at, req.result = "REJECTED", actor, time.time(), reason
        self.t.audit.record("CHANGE_REJECTED", {"id": req.id, "action": req.action, "reason": reason}, actor)
        return req

    def get(self, request_id: str) -> ChangeRequest:
        for r in self.requests:
            if r.id == request_id:
                return r
        raise KeyError(f"UNKNOWN_REQUEST:{request_id}")

    def pending(self) -> List[ChangeRequest]:
        now = time.time()
        for r in self.requests:
            if r.status == "PENDING" and now > r.expires_at:
                r.status = "EXPIRED"
        return [r for r in self.requests if r.status == "PENDING"]

    def describe(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "governed": GOVERNED, "pending": [r.model_dump(mode="json") for r in self.pending()], "history": [r.model_dump(mode="json") for r in self.requests[-20:][::-1]]}
