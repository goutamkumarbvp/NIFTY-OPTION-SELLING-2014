from __future__ import annotations

import asyncio
import csv
import io
import time
from typing import Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from terminal.analytics.backtest import run_backtest
from terminal.analytics.performance import summarize
from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api/reports", tags=["reports"])


class BacktestBody(BaseModel):
    strategy: str
    underlying: str = "NIFTY"
    days: int = Field(60, ge=5, le=500)
    lots: int = Field(1, gt=0)
    params: Dict[str, float] = Field(default_factory=dict)
    seed: int = 42
    iv_premium: float = 1.10


@router.get("/performance")
async def performance(request: Request, days: int = 365, user: dict = Depends(require("viewer"))):
    return ok(summarize(terminal(request).db.trades(limit=5000, since=time.time() - days * 86400)))


@router.get("/trades")
async def trades(request: Request, limit: int = 500, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).db.trades(limit))


@router.get("/runs")
async def runs(request: Request, limit: int = 200, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).db.runs(limit))


@router.get("/export.csv")
async def export_csv(request: Request, user: dict = Depends(require("viewer"))):
    rows = terminal(request).db.trades(5000)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=trades.csv"})


@router.post("/backtest")
async def backtest(body: BacktestBody, request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    u = t.universe.get(body.underlying)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, lambda: run_backtest(u, body.strategy, body.days, body.lots, body.params, body.seed, iv_premium=body.iv_premium)))
