"""Live broker session: status, reconnect, Zerodha daily login, reconciliation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/broker", tags=["broker"])
LIVE_FEEDS = ("kotak_neo", "zerodha_kite", "angel_smartapi")


class ZerodhaSessionBody(BaseModel):
    request_token: str


def _live(t):
    if t.live is None:
        raise ValueError("NO_LIVE_PROVIDER: set DATA_SOURCE=kotak|zerodha|angel (and/or BROKER=... with TRADING_ENV=LIVE) plus credentials in .env")
    return t.live


@router.get("/live/status")
@router.get("/kotak/status", include_in_schema=False)
@router.get("/zerodha/status", include_in_schema=False)
@router.get("/angel/status", include_in_schema=False)
async def live_status(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    if t.live is None:
        return ok({"configured": False, "provider": None, "hint": "Set DATA_SOURCE=kotak | zerodha | angel plus credentials in .env"})
    return ok({"configured": True, **t.live.status(), "chain": t.live_chain_stats, "feed": t.feed.status()})


@router.post("/live/connect")
@router.post("/kotak/connect", include_in_schema=False)
@router.post("/zerodha/connect", include_in_schema=False)
@router.post("/angel/connect", include_in_schema=False)
async def live_connect(request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    live = _live(t)
    live.authenticated = False
    result = await live.connect()
    found = await live.resolve_index_tokens(t.universe.all(), include_vix=True)
    if t.feed.name in LIVE_FEEDS:
        await t.feed.reconnect()
    t.audit.record("LIVE_SESSION_RECONNECT", {"provider": live.provider, "tokens": list(found)}, user["username"])
    return ok({"provider": live.provider, "login": result, "tokens": found})


@router.get("/reconcile")
async def reconcile_status(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"orders": t.order_reconciler.describe(), "positions": t.position_reconciler.describe(), "stream": t.stream_stats, "recovery": t.recovery})


@router.post("/reconcile")
async def reconcile_now(request: Request, adopt: bool = False, user: dict = Depends(require("admin"))):
    t = terminal(request)
    await t.order_reconciler.tick()
    res = await t.position_reconciler.tick(adopt=adopt)
    t.audit.record("RECONCILE_MANUAL", {"adopt": adopt, "ok": res.get("ok")}, user["username"])
    return ok({"positions": res, "orders": t.order_reconciler.describe()})


@router.get("/zerodha/login-url")
async def zerodha_login_url(request: Request, user: dict = Depends(require("admin"))):
    live = _live(terminal(request))
    if live.provider != "zerodha":
        raise ValueError("PROVIDER_IS_NOT_ZERODHA")
    return ok({"login_url": live.login_url()})


@router.post("/zerodha/session")
async def zerodha_session(body: ZerodhaSessionBody, request: Request, user: dict = Depends(require("admin"))):
    t = terminal(request)
    live = _live(t)
    if live.provider != "zerodha":
        raise ValueError("PROVIDER_IS_NOT_ZERODHA")
    result = await live.exchange_request_token(body.request_token.strip())
    found = await live.resolve_index_tokens(t.universe.all(), include_vix=True)
    if t.feed.name == "zerodha_kite":
        await t.feed.reconnect()
    t.audit.record("ZERODHA_SESSION_CREATED", {"user_id": result.get("user_id")}, user["username"])
    return ok({**result, "tokens": found})
