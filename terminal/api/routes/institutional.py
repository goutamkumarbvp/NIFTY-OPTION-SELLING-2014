"""Institutional controls: four-eyes governance, TCA, attribution, data quality, latency, backups, model governance."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api", tags=["institutional"])


class ReasonBody(BaseModel):
    reason: str = ""


class VersionBody(BaseModel):
    version: int


@router.get("/governance")
async def governance(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({**t.governance.describe(), "config_history": t.db.config_history(50)})


@router.post("/governance/{request_id}/approve")
async def governance_approve(request_id: str, request: Request, user: dict = Depends(require("admin"))):
    try:
        req = await terminal(request).governance.approve(request_id, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ok(req.model_dump(mode="json"))


@router.post("/governance/{request_id}/reject")
async def governance_reject(request_id: str, body: ReasonBody, request: Request, user: dict = Depends(require("admin"))):
    try:
        req = terminal(request).governance.reject(request_id, user["username"], body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ok(req.model_dump(mode="json"))


@router.get("/reports/tca")
async def tca(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).tca.summary())


@router.get("/reports/attribution")
async def attribution(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).attribution.describe())


@router.get("/system/data-quality")
async def data_quality(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({**t.dq.describe(), "days": t.dq.days()})


@router.get("/system/latency")
async def latency(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).latency.stats())


@router.get("/system/backups")
async def backups(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).backups.describe())


@router.post("/system/backups/run")
async def backups_run(request: Request, user: dict = Depends(require("admin"))):
    return ok(terminal(request).backups.run("manual"))


@router.get("/model/versions")
async def model_versions(request: Request, user: dict = Depends(require("viewer"))):
    m = terminal(request).entry_model
    return ok({"current": m.describe(), "versions": [{k: v for k, v in x.items() if k != "model"} for x in m.versions()]})


@router.post("/model/rollback")
async def model_rollback(body: VersionBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    try:
        v = t.entry_model.rollback(body.version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    t.audit.record("MODEL_ROLLBACK", {"version": body.version}, user["username"])
    return ok({k: x for k, x in v.items() if k != "model"})
