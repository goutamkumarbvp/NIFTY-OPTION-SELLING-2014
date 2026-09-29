from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from terminal.api.deps import ok, require, terminal

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system/health")
async def system_health(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).health.describe())


@router.get("/system/logs")
async def system_logs(request: Request, limit: int = 300, level: str | None = None, source: str | None = None, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).db.logs(limit, level, source))


@router.get("/system/audit")
async def system_audit(request: Request, limit: int = 200, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    return ok({"count": t.audit.count, "rows": t.audit.tail(limit)[::-1]})


@router.get("/alerts")
async def alerts(request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).alerts.describe())


@router.post("/alerts/{alert_id}/ack")
async def alert_ack(alert_id: str, request: Request, user: dict = Depends(require("viewer"))):
    return ok(terminal(request).alerts.acknowledge(alert_id))


@router.post("/alerts/test")
async def alert_test(request: Request, user: dict = Depends(require("admin"))):
    a = await terminal(request).alerts.emit("WARNING", "test", "Test alert", f"Triggered by {user['username']}", dedupe_seconds=0)
    return ok(a.model_dump(mode="json") if a else None)


@router.get("/settings")
async def settings(request: Request, user: dict = Depends(require("viewer"))):
    t = terminal(request)
    s = t.settings
    masked = {k: ("••••" if any(x in k for x in ("key", "secret", "password", "token", "mpin", "pin")) and v else v) for k, v in s.model_dump().items()}
    masked["runtime_dir"] = str(s.runtime_dir)
    return ok({"settings": masked, "auth": request.app.state.auth.describe(), "strategies": t.strategies.describe()["strategies"], "risk_limits": t.risk.limits})
