from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/market", tags=["market"])


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
async def candles(request: Request, symbol: str = "NIFTY", limit: int = 200, seconds: int | None = None, user: dict = Depends(require("viewer"))):
    """Candles at the base resolution or rebuilt from raw ticks at `seconds` (1 = one-second bars)."""
    t = terminal(request)
    return ok({"symbol": symbol.upper(), "seconds": seconds or t.processor.candle_seconds, "candles": [c.model_dump() for c in t.processor.candles(symbol.upper(), limit, seconds)],
               "indicators": t.processor.indicators(symbol.upper()), "resolution": t.feed.tick_resolution()})


@router.get("/ticks")
async def ticks(request: Request, symbol: str = "NIFTY", limit: int = 500, user: dict = Depends(require("viewer"))):
    """Raw tick stream as received from the broker (newest last)."""
    t = terminal(request)
    st = t.processor.symbols.get(symbol.upper())
    return ok({"symbol": symbol.upper(), "ticks": [x.model_dump() for x in list(st.ticks)[-limit:]] if st else [], "resolution": t.feed.tick_resolution()})


@router.get("/expiries")
async def expiries(request: Request, underlying: str = "NIFTY", user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"underlying": underlying.upper(), "expiries": t.chain_builder.expiries(t.universe.get(underlying), 6)})


@router.get("/universe")
async def universe(request: Request, user: dict = Depends(require("viewer"))):
    return ok([u.model_dump(mode="json") for u in terminal(request).universe.all()])
