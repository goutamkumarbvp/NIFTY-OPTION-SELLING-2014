from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@router.get("")
async def copilot_status(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({**t.copilot.status(), "history": t.db.copilot_history(user["username"], 20), "tools": [d["name"] for d in t.copilot.tool_definitions()]})


@router.post("/chat")
async def copilot_chat(body: ChatBody, request: Request, user: dict = Depends(require("trader"))):
    return ok(await terminal(request).copilot.chat(body.message, user["username"]))
