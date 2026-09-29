from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from terminal.api.deps import ok, require, terminal
from terminal.core.models import OrderSource
from terminal.strategy.library import payoff_profile

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


class DeployBody(BaseModel):
    strategy: str
    underlying: str
    lots: int = Field(1, gt=0)
    expiry: str | None = None
    params: Dict[str, float] = Field(default_factory=dict)


class StrategyConfigBody(BaseModel):
    patch: Dict[str, Any]


@router.get("")
async def strategy_list(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).strategies.describe())


@router.post("/config/{key}")
async def strategy_config(key: str, body: StrategyConfigBody, request: Request, user: dict = Depends(require("admin"))):
    return ok(terminal(request).strategies.update_config(key, body.patch, user["username"]))


@router.post("/preview")
async def strategy_preview(body: DeployBody, request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    plan = t.strategies.make_plan(body.strategy, body.underlying.upper(), body.lots, body.expiry, body.params, rationale=["manual preview"], source=OrderSource.MANUAL)
    u = t.universe.get(body.underlying)
    prof = payoff_profile(plan.legs, t.chain_for(body.underlying.upper(), body.expiry).spot, u.lot_size)
    return ok({"plan": plan.model_dump(mode="json"), "payoff": prof, "risk_check": t.risk.check_plan(plan)})


@router.post("/deploy")
async def strategy_deploy(body: DeployBody, request: Request, user: dict = Depends(require("trader"))):
    t = terminal(request)
    plan = t.strategies.make_plan(body.strategy, body.underlying.upper(), body.lots, body.expiry, body.params, rationale=[f"manual deploy by {user['username']}"], source=OrderSource.MANUAL)
    run = await t.strategies.deploy(plan, user["username"], OrderSource.MANUAL)
    return ok(run.model_dump(mode="json"))


@router.post("/exit/{run_id}")
async def strategy_exit(run_id: str, request: Request, user: dict = Depends(require("trader"))):
    t = terminal(request)
    run = await t.strategies.exit_run(run_id, user["username"], "MANUAL_EXIT", OrderSource.MANUAL)
    return ok(run.model_dump(mode="json"))


@router.get("/payoff/{run_id}")
async def strategy_payoff(run_id: str, request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    run = t.strategies.runs[run_id]
    u = t.universe.get(run.underlying)
    spot = t.processor.last_price(run.underlying) or u.base_spot
    return ok({"run": run.model_dump(mode="json"), "payoff": payoff_profile(run.legs, spot, u.lot_size), "spot": spot})
