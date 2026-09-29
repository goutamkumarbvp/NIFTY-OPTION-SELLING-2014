"""Journal, council evaluation and learned-model insight endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api", tags=["insights"])


@router.get("/journal")
async def journal(request: Request, limit: int = 30, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    today = t.journal.build_entry()
    return ok({"today": today, "entries": t.db.journal(limit), "similar": t.journal.similar_days(today["features"])})


@router.post("/journal/write")
async def journal_write(request: Request, user: dict = Depends(require("trader"))):
    return ok(terminal(request).journal.write_today())


@router.get("/agents/evaluation")
async def evaluation(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"scorecard": t.evaluator.scorecard(), "model": t.entry_model.describe(), "pending": len(t.db.unscored_decisions(9e12))})


@router.post("/agents/evaluation/score")
async def evaluation_score(request: Request, user: dict = Depends(require("trader"))):
    t = terminal(request)
    scored = t.evaluator.score_pending()
    trained = t.entry_model.train_pending()
    return ok({"scored": scored, "trained": trained, "scorecard": t.evaluator.scorecard(), "model": t.entry_model.describe()})
