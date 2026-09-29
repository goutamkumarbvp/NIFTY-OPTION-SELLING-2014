"""FastAPI application factory: routers + WebSocket + static dashboard."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from terminal import PRODUCT, __version__
from terminal.api.auth import AuthManager
from terminal.api.routes import agents, auth, broker, control, copilot, guardian, insights, institutional, market, metrics, reports, risk, strategy, system, trading, ws
from terminal.app import Terminal, get_terminal, set_terminal

log = logging.getLogger("terminal.api")
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
ROUTERS = [auth.router, control.router, market.router, strategy.router, trading.router, agents.router, risk.router, reports.router, system.router, broker.router, copilot.router, insights.router, metrics.router, ws.router, guardian.router, institutional.router]


def create_app(terminal: Terminal | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        t: Terminal = app.state.terminal
        await t.start()
        try:
            yield
        finally:
            await t.stop()

    app = FastAPI(title=PRODUCT, version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    t = terminal or get_terminal()
    set_terminal(t)
    app.state.terminal = t
    app.state.auth = AuthManager(t.settings)
    if UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError):
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})

    @app.exception_handler(RuntimeError)
    async def _runtime_error(_: Request, exc: RuntimeError):
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})

    @app.exception_handler(KeyError)
    async def _key_error(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"ok": False, "error": f"NOT_FOUND:{exc}"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        return FileResponse(UI_DIR / "index.html")

    for r in ROUTERS:
        app.include_router(r)
    extra = getattr(t, "extra_routers", None) or []
    for r in extra:
        app.include_router(r)
    return app
