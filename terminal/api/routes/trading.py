"""Orders, positions, approvals and exits."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from terminal.api.deps import ok, require, terminal
from terminal.core.models import OrderSource, OrderType, Side

router = APIRouter(prefix="/api", tags=["trading"])


class OrderBody(BaseModel):
    symbol: str
    side: str
    lots: int = Field(gt=0)
    order_type: str = "MARKET"
    limit_price: float | None = None
    reason: str = "manual"


class ApproveBody(BaseModel):
    lots: int | None = None
    reason: str = ""


@router.get("/orders")
async def orders(request: Request, limit: int = 200, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"orders": t.orders.recent(limit), "pending_approval": [o.model_dump(mode="json") for o in t.orders.pending_approval()]})


@router.post("/orders/place")
async def place(body: OrderBody, request: Request, user: dict = Depends(require("trader"))):
    t = terminal(request)
    order = t.orders.build(body.symbol.upper(), Side(body.side.upper()), body.lots, OrderSource.MANUAL, OrderType(body.order_type.upper()), body.limit_price, tag="manual", reason=body.reason)
    return ok((await t.orders.submit(order, user["username"])).model_dump(mode="json"))


@router.post("/orders/{order_id}/cancel")
async def cancel(order_id: str, request: Request, user: dict = Depends(require("trader"))):
    return ok((await terminal(request).orders.cancel(order_id, user["username"])).model_dump(mode="json"))


@router.post("/orders/{order_id}/approve")
async def approve_order(order_id: str, request: Request, user: dict = Depends(require("trader"))):
    return ok((await terminal(request).orders.approve(order_id, user["username"])).model_dump(mode="json"))


@router.post("/orders/{order_id}/reject")
async def reject_order(order_id: str, body: ApproveBody, request: Request, user: dict = Depends(require("trader"))):
    return ok((await terminal(request).orders.reject(order_id, user["username"], body.reason)).model_dump(mode="json"))


@router.get("/positions")
async def positions(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"positions": t.positions.snapshot(), "greeks": t.positions.greeks(), "pnl": {"daily": t.positions.daily_pnl(), "realized": round(t.positions.realized_today, 2), "unrealized": t.positions.unrealized()}})


@router.post("/positions/flatten")
async def flatten(request: Request, user: dict = Depends(require("trader"))):
    out = await terminal(request).orders.flatten_all(OrderSource.MANUAL, user["username"], "MANUAL_FLATTEN")
    return ok([o.model_dump(mode="json") for o in out])


@router.post("/positions/{symbol}/close")
async def close_position(symbol: str, request: Request, user: dict = Depends(require("trader"))):
    t = terminal(request)
    pos = t.positions.positions.get(symbol.upper())
    if not pos:
        raise HTTPException(status_code=404, detail="NO_POSITION")
    order = await t.exit_guard.request(symbol.upper(), "MANUAL_CLOSE", OrderSource.MANUAL, run_id=pos.strategy_run_id, actor=user["username"])
    if order is None:
        raise ValueError("EXIT_ALREADY_IN_FLIGHT_OR_FLAT")
    return ok(order.model_dump(mode="json"))


@router.get("/approvals")
async def approvals(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"plans": [p.model_dump(mode="json") for p in t.council.pending_plans()], "orders": [o.model_dump(mode="json") for o in t.orders.pending_approval()]})


@router.post("/approvals/{plan_id}/approve")
async def approve_plan(plan_id: str, body: ApproveBody, request: Request, user: dict = Depends(require("trader"))):
    run_id = await terminal(request).council.approve_plan(plan_id, user["username"], body.lots)
    return ok({"run_id": run_id})


@router.post("/approvals/{plan_id}/reject")
async def reject_plan(plan_id: str, body: ApproveBody, request: Request, user: dict = Depends(require("trader"))):
    terminal(request).council.reject_plan(plan_id, user["username"], body.reason)
    return ok()


@router.get("/exits")
async def exits(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).exit_guard.describe())
