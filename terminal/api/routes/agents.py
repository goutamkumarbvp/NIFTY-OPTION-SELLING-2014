from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentConfigBody(BaseModel):
    patch: Dict[str, Any]


class FocusBody(BaseModel):
    symbols: List[str]


class EventsBody(BaseModel):
    events: List[Dict[str, str]]


@router.get("")
async def agents(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).council.describe())


@router.get("/council")
async def council_history(request: Request, limit: int = 50, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).db.council(limit))


@router.post("/run")
async def agents_run(request: Request, underlying: str | None = None, user: dict = Depends(require("trader"))):
    decisions = await terminal(request).council.run_cycle([underlying.upper()] if underlying else None, force=True)
    return ok([d.model_dump(mode="json") for d in decisions])


@router.post("/focus")
async def agent_focus(body: FocusBody, request: Request, user: dict = Depends(require("admin"))):
    return ok(terminal(request).council.set_focus(body.symbols, user["username"]))


@router.post("/events")
async def agent_events(body: EventsBody, request: Request, user: dict = Depends(require("admin"))):
    terminal(request).db.set_setting("events", body.events)
    return ok(body.events)


@router.post("/{name}/config")
async def agent_config(name: str, body: AgentConfigBody, request: Request, user: dict = Depends(require("admin"))):
    return ok(terminal(request).council.update_agent(name, body.patch, user["username"]))
