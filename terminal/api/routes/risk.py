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
    return ok(terminal(request).risk.update_limits(body.limits, user["username"]))
