"""PWA: manifest / service worker at the root scope, and Web Push subscription management."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from terminal.api.deps import ok, require, terminal

UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
router = APIRouter(tags=["pwa"])


class SubscribeBody(BaseModel):
    endpoint: str
    keys: Dict[str, str] = {}
    expirationTime: float | None = None
    ua: str = ""


class UnsubscribeBody(BaseModel):
    endpoint: str


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(UI_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    return FileResponse(UI_DIR / "sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@router.get("/api/push/config")
async def push_config(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).push.describe())


@router.post("/api/push/subscribe")
async def push_subscribe(body: SubscribeBody, request: Request, user: dict = Depends(require("viewer"))):
    n = terminal(request).push.subscribe(body.model_dump(), user["username"])
    return ok({"subscriptions": n})


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(body: UnsubscribeBody, request: Request, user: dict = Depends(require("viewer"))):
    return ok({"subscriptions": terminal(request).push.unsubscribe(body.endpoint)})


@router.post("/api/push/test")
async def push_test(request: Request, user: dict = Depends(require("viewer"))):
    res: Dict[str, Any] = await terminal(request).push.send("Test notification", f"Sent by {user['username']}", "WARNING", "test", "settings")
    return ok(res)
