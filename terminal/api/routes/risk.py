from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/risk", tags=["risk"])


class LimitsBody(BaseModel):
    limits: Dict[str, float]


@router.get("")
async def risk(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).risk.describe())


@router.post("/limits")
async def risk_limits(body: LimitsBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    res = await t.governance.submit("risk_limits", {"limits": body.limits}, user["username"])
    return ok(res["result"] if res["applied"] else {"pending_approval": res["request"], "limits": t.risk.limits})


@router.get("/stress")
async def risk_stress(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok(t.portfolio_risk.stress())


@router.get("/pretrade")
async def risk_pretrade(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).pretrade.describe())
