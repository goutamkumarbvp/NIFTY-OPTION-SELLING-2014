"""Health, state, mode, safety gate, kill switch, engine control, schedule."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from terminal import PRODUCT, __version__
from terminal.api.deps import ok, require, terminal
from terminal.core.models import TerminalMode

router = APIRouter(prefix="/api", tags=["control"])


class ModeBody(BaseModel):
    mode: str
    reason: str = ""


class GateBody(BaseModel):
    open: bool


class MarketToggleBody(BaseModel):
    market: str
    enabled: bool


class ScheduleBody(BaseModel):
    terminal_start_time: str | None = None
    terminal_end_time: str | None = None
    entry_window_start: str | None = None
    entry_window_end: str | None = None
    square_off_time: str | None = None
    mcx_square_off_time: str | None = None


@router.get("/health")
async def health(request: Request):
    t = terminal(request)
    return ok({"product": PRODUCT, "version": __version__, "mode": t.mode.value, "env": t.env.value, "engine_running": t.engine_running, "paused": t.paused,
               "feed": t.feed.status(), "broker": t.broker.status(), "risk_level": t.risk.snapshot.level.value, "uptime": round(time.time() - t.started_at)})


@router.get("/state")
async def state(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).snapshot())


@router.get("/mode")
async def get_mode(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"mode": t.mode.value, "env": t.env.value, "paused": t.paused, "live_allowed": t.settings.live_allowed})


@router.post("/mode")
async def set_mode(body: ModeBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    await t.set_mode(TerminalMode(body.mode.upper()), user["username"], body.reason)
    return ok({"mode": t.mode.value})


@router.post("/safety/gate")
async def gate(body: GateBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    t.risk.set_gate(body.open, user["username"])
    return ok({"safety_gate_open": t.risk.safety_gate_open})


@router.post("/safety/kill")
async def kill(request: Request, user: dict = Depends(require("trader"))):
    t = terminal(request)
    await t.risk.kill(user["username"])
    return ok(t.risk.describe())


@router.post("/safety/reset")
async def reset_kill(request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    t.risk.reset_kill(user["username"])
    return ok(t.risk.describe())


@router.post("/safety/market")
async def toggle_market(body: MarketToggleBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    t.risk.set_market(body.market, body.enabled, user["username"])
    return ok(t.risk.market_enabled)


@router.post("/engine/{action}")
async def engine(action: str, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    if action == "pause":
        t.paused = True
    elif action == "resume":
        if t.risk.kill_switch:
            raise ValueError("RESET_KILL_SWITCH_FIRST")
        t.paused = False
        t.risk.halted_reason = ""
    elif action == "cycle":
        asyncio.create_task(t.council.run_cycle(force=True))
    else:
        raise HTTPException(status_code=404, detail="UNKNOWN_ACTION")
    t.audit.record(f"ENGINE_{action.upper()}", {}, user["username"])
    return ok({"paused": t.paused})


@router.get("/schedule")
async def schedule_get(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).scheduler.describe())


@router.post("/schedule")
async def schedule_set(body: ScheduleBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    for k, v in body.model_dump(exclude_none=True).items():
        t.scheduler.overrides[k] = v
    t.audit.record("SCHEDULE_UPDATED", body.model_dump(exclude_none=True), user["username"])
    return ok(t.scheduler.describe())
