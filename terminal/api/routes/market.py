from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/market", tags=["market"])


class ShockBody(BaseModel):
    symbol: str
    pct: float


@router.get("/overview")
async def overview(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    vix = t.processor.last_tick("INDIAVIX")
    return ok({"markets": t.market_overview(), "vix": vix.model_dump() if vix else None, "vix_rank": t.processor.vix_rank()})


@router.get("/chain")
async def chain(request: Request, underlying: str = "NIFTY", expiry: str | None = None, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    if not t.universe.has(underlying):
        raise HTTPException(status_code=404, detail="UNKNOWN_UNDERLYING")
    ch = t.chain_for(underlying.upper(), expiry)
    u = t.universe.get(underlying)
    return ok({"chain": ch.model_dump(mode="json"), "expiries": t.chain_builder.expiries(u, 4), "indicators": t.processor.indicators(underlying.upper()),
               "pcr_history": list(t.processor.pcr_history[underlying.upper()])[-120:]})


@router.get("/candles")
async def candles(request: Request, symbol: str = "NIFTY", limit: int = 200, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"symbol": symbol.upper(), "candles": [c.model_dump() for c in t.processor.candles(symbol.upper(), limit)], "indicators": t.processor.indicators(symbol.upper())})


@router.get("/expiries")
async def expiries(request: Request, underlying: str = "NIFTY", user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"underlying": underlying.upper(), "expiries": t.chain_builder.expiries(t.universe.get(underlying), 6)})


@router.get("/universe")
async def universe(request: Request, user: dict = Depends(require("viewer"))):
    return ok([u.model_dump(mode="json") for u in terminal(request).universe.all()])


@router.post("/shock")
async def shock(body: ShockBody, request: Request, user: dict = Depends(require("admin"))):
    """Simulation-only: inject a spot/VIX shock to exercise risk controls."""
    t = terminal(request)
    if not hasattr(t.feed, "shock"):
        raise ValueError("SHOCK_ONLY_IN_SIMULATION")
    t.feed.shock(body.symbol, body.pct)
    t.audit.record("SIM_SHOCK", body.model_dump(), user["username"])
    return ok({"symbol": body.symbol, "pct": body.pct})
