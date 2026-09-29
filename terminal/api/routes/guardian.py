"""Guardian self-healing agent: incidents, operator permission, manual scan."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/guardian", tags=["guardian"])


class RejectBody(BaseModel):
    reason: str = ""


class PolicyBody(BaseModel):
    auto_apply: str


@router.get("")
async def guardian_status(request: Request, limit: int = 50, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).guardian.describe(limit))


@router.post("/scan")
async def guardian_scan(request: Request, user: dict = Depends(require("viewer"))):
    g = terminal(request).guardian
    new = await g.scan()
    return ok({"new_incidents": [i.model_dump(mode="json") for i in new], "pending": len(g.pending())})


@router.post("/{incident_id}/approve")
async def guardian_approve(incident_id: str, request: Request, user: dict = Depends(require("trader"))):
    g = terminal(request).guardian
    try:
        inc = await g.approve(incident_id, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ok(inc.model_dump(mode="json"))


@router.post("/{incident_id}/reject")
async def guardian_reject(incident_id: str, body: RejectBody, request: Request, user: dict = Depends(require("trader"))):
    g = terminal(request).guardian
    try:
        inc = g.reject(incident_id, user["username"], body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ok(inc.model_dump(mode="json"))


@router.post("/policy")
async def guardian_policy(body: PolicyBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    v = body.auto_apply.strip().lower()
    if v not in {"none", "low", "medium", "all"}:
        raise ValueError("AUTO_APPLY_MUST_BE none|low|medium|all")
    res = await t.governance.submit("guardian_policy", {"auto_apply": v}, user["username"])
    return ok({"auto_apply": t.guardian.auto_apply, "pending_approval": None if res["applied"] else res["request"]})
