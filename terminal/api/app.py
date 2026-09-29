"""FastAPI application: REST + WebSocket gateway in front of the Terminal."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from terminal import PRODUCT, __version__
from terminal.analytics.backtest import run_backtest
from terminal.analytics.performance import summarize
from terminal.api.auth import AuthManager, current_user, require, ws_user
from terminal.app import Terminal, get_terminal, set_terminal
from terminal.core.models import OrderSource, OrderType, Side, TerminalMode
from terminal.strategy.library import SPECS, payoff_profile

log = logging.getLogger("terminal.api")
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


# ----------------------------------------------------------------------- schemas
class ModeBody(BaseModel):
    mode: str
    reason: str = ""


class GateBody(BaseModel):
    open: bool


class OrderBody(BaseModel):
    symbol: str
    side: str
    lots: int = Field(gt=0)
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    reason: str = "manual"


class DeployBody(BaseModel):
    strategy: str
    underlying: str
    lots: int = Field(1, gt=0)
    expiry: Optional[str] = None
    params: Dict[str, float] = Field(default_factory=dict)


class ApproveBody(BaseModel):
    lots: Optional[int] = None
    reason: str = ""


class LimitsBody(BaseModel):
    limits: Dict[str, float]


class StrategyConfigBody(BaseModel):
    patch: Dict[str, Any]


class AgentConfigBody(BaseModel):
    patch: Dict[str, Any]


class FocusBody(BaseModel):
    symbols: List[str]


class MarketToggleBody(BaseModel):
    market: str
    enabled: bool


class BacktestBody(BaseModel):
    strategy: str
    underlying: str = "NIFTY"
    days: int = Field(60, ge=5, le=500)
    lots: int = Field(1, gt=0)
    params: Dict[str, float] = Field(default_factory=dict)
    seed: int = 42


class LoginBody(BaseModel):
    username: str
    password: str


class ScheduleBody(BaseModel):
    entry_window_start: Optional[str] = None
    entry_window_end: Optional[str] = None
    square_off_time: Optional[str] = None
    mcx_square_off_time: Optional[str] = None


class ShockBody(BaseModel):
    symbol: str
    pct: float


class EventsBody(BaseModel):
    events: List[Dict[str, str]]


# ----------------------------------------------------------------------- app
def create_app(terminal: Optional[Terminal] = None) -> FastAPI:
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

    def T() -> Terminal:
        return app.state.terminal

    def ok(data: Any = None, **extra) -> dict:
        return {"ok": True, "data": data, **extra}

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError):
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})

    @app.exception_handler(RuntimeError)
    async def _runtime_error(_: Request, exc: RuntimeError):
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})

    @app.exception_handler(KeyError)
    async def _key_error(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"ok": False, "error": f"NOT_FOUND:{exc}"})

    # ------------------------------------------------------------------ ui
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        return FileResponse(UI_DIR / "index.html")

    # ------------------------------------------------------------------ auth
    @app.get("/api/auth/me")
    async def me(request: Request):
        user = app.state.auth.identify(request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token"))
        return ok({"user": user, **app.state.auth.describe()})

    @app.post("/api/auth/login")
    async def login(body: LoginBody):
        res = app.state.auth.login(body.username, body.password)
        if not res:
            raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
        T().audit.record("LOGIN", {"username": body.username}, body.username)
        return ok(res)

    @app.post("/api/auth/logout")
    async def logout(request: Request, user: dict = Depends(current_user)):
        app.state.auth.logout(request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        return ok()

    # ------------------------------------------------------------------ state
    @app.get("/api/health")
    async def health():
        t = T()
        return ok({"product": PRODUCT, "version": __version__, "mode": t.mode.value, "env": t.env.value, "engine_running": t.engine_running, "paused": t.paused,
                   "feed": t.feed.status(), "broker": t.broker.status(), "risk_level": t.risk.snapshot.level.value, "uptime": round(time.time() - t.started_at)})

    @app.get("/api/state")
    async def state(user: dict = Depends(require("viewer"))):
        return ok(T().snapshot())

    # ------------------------------------------------------------------ mode & safety
    @app.get("/api/mode")
    async def get_mode(user: dict = Depends(require("viewer"))):
        t = T()
        return ok({"mode": t.mode.value, "env": t.env.value, "paused": t.paused, "live_allowed": t.settings.live_allowed})

    @app.post("/api/mode")
    async def set_mode(body: ModeBody, user: dict = Depends(require("admin"))):
        t = T()
        mode = TerminalMode(body.mode.upper())
        await t.set_mode(mode, user["username"], body.reason)
        return ok({"mode": t.mode.value})

    @app.post("/api/safety/gate")
    async def gate(body: GateBody, user: dict = Depends(require("admin"))):
        T().risk.set_gate(body.open, user["username"])
        return ok({"safety_gate_open": T().risk.safety_gate_open})

    @app.post("/api/safety/kill")
    async def kill(user: dict = Depends(require("trader"))):
        await T().risk.kill(user["username"])
        return ok(T().risk.describe())

    @app.post("/api/safety/reset")
    async def reset_kill(user: dict = Depends(require("admin"))):
        T().risk.reset_kill(user["username"])
        return ok(T().risk.describe())

    @app.post("/api/safety/market")
    async def toggle_market(body: MarketToggleBody, user: dict = Depends(require("admin"))):
        T().risk.set_market(body.market, body.enabled, user["username"])
        return ok(T().risk.market_enabled)

    @app.post("/api/engine/{action}")
    async def engine(action: str, user: dict = Depends(require("admin"))):
        t = T()
        if action == "pause":
            t.paused = True
        elif action == "resume":
            if t.risk.kill_switch:
                raise ValueError("RESET_KILL_SWITCH_FIRST")
            t.paused = False
            t.risk.halted_reason = ""
        elif action == "cycle":
            asyncio.create_task(t.council.run_cycle(force=True))
        else:
            raise HTTPException(status_code=404, detail="UNKNOWN_ACTION")
        t.audit.record(f"ENGINE_{action.upper()}", {}, user["username"])
        return ok({"paused": t.paused})

    # ------------------------------------------------------------------ market
    @app.get("/api/market/overview")
    async def overview(user: dict = Depends(require("viewer"))):
        t = T()
        vix = t.processor.last_tick("INDIAVIX")
        return ok({"markets": t.market_overview(), "vix": vix.model_dump() if vix else None, "vix_rank": t.processor.vix_rank()})

    @app.get("/api/market/chain")
    async def chain(underlying: str = "NIFTY", expiry: Optional[str] = None, user: dict = Depends(require("viewer"))):
        t = T()
        if not t.universe.has(underlying):
            raise HTTPException(status_code=404, detail="UNKNOWN_UNDERLYING")
        ch = t.chain_for(underlying.upper(), expiry)
        u = t.universe.get(underlying)
        return ok({"chain": ch.model_dump(mode="json"), "expiries": t.chain_builder.expiries(u, 4), "indicators": t.processor.indicators(underlying.upper()),
                   "pcr_history": list(t.processor.pcr_history[underlying.upper()])[-120:]})

    @app.get("/api/market/candles")
    async def candles(symbol: str = "NIFTY", limit: int = 200, user: dict = Depends(require("viewer"))):
        t = T()
        return ok({"symbol": symbol.upper(), "candles": [c.model_dump() for c in t.processor.candles(symbol.upper(), limit)], "indicators": t.processor.indicators(symbol.upper())})

    @app.get("/api/market/expiries")
    async def expiries(underlying: str = "NIFTY", user: dict = Depends(require("viewer"))):
        t = T()
        return ok({"underlying": underlying.upper(), "expiries": t.chain_builder.expiries(t.universe.get(underlying), 6)})

    @app.get("/api/market/universe")
    async def universe(user: dict = Depends(require("viewer"))):
        return ok([u.model_dump(mode="json") for u in T().universe.all()])

    @app.post("/api/market/shock")
    async def shock(body: ShockBody, user: dict = Depends(require("admin"))):
        """Simulation-only: inject a spot/VIX shock to exercise risk controls."""
        t = T()
        if not hasattr(t.feed, "shock"):
            raise ValueError("SHOCK_ONLY_IN_SIMULATION")
        t.feed.shock(body.symbol, body.pct)
        t.audit.record("SIM_SHOCK", body.model_dump(), user["username"])
        return ok({"symbol": body.symbol, "pct": body.pct})

    # ------------------------------------------------------------------ strategy
    @app.get("/api/strategy")
    async def strategy_list(user: dict = Depends(require("viewer"))):
        return ok(T().strategies.describe())

    @app.post("/api/strategy/config/{key}")
    async def strategy_config(key: str, body: StrategyConfigBody, user: dict = Depends(require("admin"))):
        return ok(T().strategies.update_config(key, body.patch, user["username"]))

    @app.post("/api/strategy/preview")
    async def strategy_preview(body: DeployBody, user: dict = Depends(require("viewer"))):
        t = T()
        plan = t.strategies.make_plan(body.strategy, body.underlying.upper(), body.lots, body.expiry, body.params, rationale=["manual preview"], source=OrderSource.MANUAL)
        u = t.universe.get(body.underlying)
        prof = payoff_profile(plan.legs, t.chain_for(body.underlying.upper(), body.expiry).spot, u.lot_size)
        return ok({"plan": plan.model_dump(mode="json"), "payoff": prof, "risk_check": t.risk.check_plan(plan)})

    @app.post("/api/strategy/deploy")
    async def strategy_deploy(body: DeployBody, user: dict = Depends(require("trader"))):
        t = T()
        plan = t.strategies.make_plan(body.strategy, body.underlying.upper(), body.lots, body.expiry, body.params, rationale=[f"manual deploy by {user['username']}"], source=OrderSource.MANUAL)
        run = await t.strategies.deploy(plan, user["username"], OrderSource.MANUAL)
        return ok(run.model_dump(mode="json"))

    @app.post("/api/strategy/exit/{run_id}")
    async def strategy_exit(run_id: str, user: dict = Depends(require("trader"))):
        t = T()
        run = await t.strategies.exit_run(run_id, user["username"], "MANUAL_EXIT", OrderSource.MANUAL)
        return ok(run.model_dump(mode="json"))

    @app.get("/api/strategy/payoff/{run_id}")
    async def strategy_payoff(run_id: str, user: dict = Depends(require("viewer"))):
        t = T()
        run = t.strategies.runs[run_id]
        u = t.universe.get(run.underlying)
        spot = t.processor.last_price(run.underlying) or u.base_spot
        return ok({"run": run.model_dump(mode="json"), "payoff": payoff_profile(run.legs, spot, u.lot_size), "spot": spot})

    @app.get("/api/schedule")
    async def schedule_get(user: dict = Depends(require("viewer"))):
        return ok(T().scheduler.describe())

    @app.post("/api/schedule")
    async def schedule_set(body: ScheduleBody, user: dict = Depends(require("admin"))):
        t = T()
        for k, v in body.model_dump(exclude_none=True).items():
            t.scheduler.overrides[k] = v
        t.audit.record("SCHEDULE_UPDATED", body.model_dump(exclude_none=True), user["username"])
        return ok(t.scheduler.describe())

    # ------------------------------------------------------------------ orders & positions
    @app.get("/api/orders")
    async def orders(limit: int = 200, user: dict = Depends(require("viewer"))):
        return ok({"orders": T().orders.recent(limit), "pending_approval": [o.model_dump(mode="json") for o in T().orders.pending_approval()]})

    @app.post("/api/orders/place")
    async def place(body: OrderBody, user: dict = Depends(require("trader"))):
        t = T()
        order = t.orders.build(body.symbol.upper(), Side(body.side.upper()), body.lots, OrderSource.MANUAL, OrderType(body.order_type.upper()), body.limit_price, tag="manual", reason=body.reason)
        order = await t.orders.submit(order, user["username"])
        return ok(order.model_dump(mode="json"))

    @app.post("/api/orders/{order_id}/cancel")
    async def cancel(order_id: str, user: dict = Depends(require("trader"))):
        return ok((await T().orders.cancel(order_id, user["username"])).model_dump(mode="json"))

    @app.post("/api/orders/{order_id}/approve")
    async def approve_order(order_id: str, user: dict = Depends(require("trader"))):
        return ok((await T().orders.approve(order_id, user["username"])).model_dump(mode="json"))

    @app.post("/api/orders/{order_id}/reject")
    async def reject_order(order_id: str, body: ApproveBody, user: dict = Depends(require("trader"))):
        return ok((await T().orders.reject(order_id, user["username"], body.reason)).model_dump(mode="json"))

    @app.get("/api/positions")
    async def positions(user: dict = Depends(require("viewer"))):
        t = T()
        return ok({"positions": t.positions.snapshot(), "greeks": t.positions.greeks(), "pnl": {"daily": t.positions.daily_pnl(), "realized": round(t.positions.realized_today, 2), "unrealized": t.positions.unrealized()}})

    @app.post("/api/positions/flatten")
    async def flatten(user: dict = Depends(require("trader"))):
        out = await T().orders.flatten_all(OrderSource.MANUAL, user["username"], "MANUAL_FLATTEN")
        return ok([o.model_dump(mode="json") for o in out])

    @app.post("/api/positions/{symbol}/close")
    async def close_position(symbol: str, user: dict = Depends(require("trader"))):
        t = T()
        pos = t.positions.positions.get(symbol.upper())
        if not pos:
            raise HTTPException(status_code=404, detail="NO_POSITION")
        order = t.orders.build(symbol.upper(), Side.BUY if pos.net_qty < 0 else Side.SELL, pos.lots or 1, OrderSource.MANUAL, run_id=pos.strategy_run_id, tag="manual", reason="close position")
        return ok((await t.orders.submit(order, user["username"], protective=True)).model_dump(mode="json"))

    # ------------------------------------------------------------------ approvals (council plans)
    @app.get("/api/approvals")
    async def approvals(user: dict = Depends(require("viewer"))):
        t = T()
        return ok({"plans": [p.model_dump(mode="json") for p in t.council.pending_plans()], "orders": [o.model_dump(mode="json") for o in t.orders.pending_approval()]})

    @app.post("/api/approvals/{plan_id}/approve")
    async def approve_plan(plan_id: str, body: ApproveBody, user: dict = Depends(require("trader"))):
        run_id = await T().council.approve_plan(plan_id, user["username"], body.lots)
        return ok({"run_id": run_id})

    @app.post("/api/approvals/{plan_id}/reject")
    async def reject_plan(plan_id: str, body: ApproveBody, user: dict = Depends(require("trader"))):
        T().council.reject_plan(plan_id, user["username"], body.reason)
        return ok()

    # ------------------------------------------------------------------ agents
    @app.get("/api/agents")
    async def agents(user: dict = Depends(require("viewer"))):
        return ok(T().council.describe())

    @app.get("/api/agents/council")
    async def council_history(limit: int = 50, user: dict = Depends(require("viewer"))):
        return ok(T().db.council(limit))

    @app.post("/api/agents/run")
    async def agents_run(underlying: Optional[str] = None, user: dict = Depends(require("trader"))):
        t = T()
        decisions = await t.council.run_cycle([underlying.upper()] if underlying else None, force=True)
        return ok([d.model_dump(mode="json") for d in decisions])

    @app.post("/api/agents/{name}/config")
    async def agent_config(name: str, body: AgentConfigBody, user: dict = Depends(require("admin"))):
        return ok(T().council.update_agent(name, body.patch, user["username"]))

    @app.post("/api/agents/focus")
    async def agent_focus(body: FocusBody, user: dict = Depends(require("admin"))):
        return ok(T().council.set_focus(body.symbols, user["username"]))

    @app.post("/api/agents/events")
    async def agent_events(body: EventsBody, user: dict = Depends(require("admin"))):
        T().db.set_setting("events", body.events)
        return ok(body.events)

    # ------------------------------------------------------------------ risk
    @app.get("/api/risk")
    async def risk(user: dict = Depends(require("viewer"))):
        return ok(T().risk.describe())

    @app.post("/api/risk/limits")
    async def risk_limits(body: LimitsBody, user: dict = Depends(require("admin"))):
        return ok(T().risk.update_limits(body.limits, user["username"]))

    # ------------------------------------------------------------------ reports
    @app.get("/api/reports/performance")
    async def performance(days: int = 365, user: dict = Depends(require("viewer"))):
        return ok(summarize(T().db.trades(limit=5000, since=time.time() - days * 86400)))

    @app.get("/api/reports/trades")
    async def trades(limit: int = 500, user: dict = Depends(require("viewer"))):
        return ok(T().db.trades(limit))

    @app.get("/api/reports/runs")
    async def runs(limit: int = 200, user: dict = Depends(require("viewer"))):
        return ok(T().db.runs(limit))

    @app.get("/api/reports/export.csv")
    async def export_csv(user: dict = Depends(require("viewer"))):
        rows = T().db.trades(5000)
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=trades.csv"})

    @app.post("/api/reports/backtest")
    async def backtest(body: BacktestBody, user: dict = Depends(require("viewer"))):
        t = T()
        u = t.universe.get(body.underlying)
        loop = asyncio.get_running_loop()
        return ok(await loop.run_in_executor(None, lambda: run_backtest(u, body.strategy, body.days, body.lots, body.params, body.seed)))

    # ------------------------------------------------------------------ system
    @app.get("/api/system/health")
    async def system_health(user: dict = Depends(require("viewer"))):
        return ok(T().health.describe())

    @app.get("/api/system/logs")
    async def system_logs(limit: int = 300, level: Optional[str] = None, source: Optional[str] = None, user: dict = Depends(require("viewer"))):
        return ok(T().db.logs(limit, level, source))

    @app.get("/api/system/audit")
    async def system_audit(limit: int = 200, user: dict = Depends(require("viewer"))):
        return ok({"count": T().audit.count, "rows": T().audit.tail(limit)[::-1]})

    @app.get("/api/alerts")
    async def alerts(user: dict = Depends(require("viewer"))):
        return ok(T().alerts.describe())

    @app.post("/api/alerts/{alert_id}/ack")
    async def alert_ack(alert_id: str, user: dict = Depends(require("viewer"))):
        return ok(T().alerts.acknowledge(alert_id))

    @app.post("/api/alerts/test")
    async def alert_test(user: dict = Depends(require("admin"))):
        a = await T().alerts.emit("WARNING", "test", "Test alert", f"Triggered by {user['username']}", dedupe_seconds=0)
        return ok(a.model_dump(mode="json") if a else None)

    @app.get("/api/settings")
    async def settings(user: dict = Depends(require("viewer"))):
        s = T().settings
        masked = {k: ("••••" if any(x in k for x in ("key", "secret", "password", "token", "mpin")) and v else v) for k, v in s.model_dump().items()}
        masked["runtime_dir"] = str(s.runtime_dir)
        return ok({"settings": masked, "auth": app.state.auth.describe(), "strategies": T().strategies.describe()["strategies"], "risk_limits": T().risk.limits})

    # ------------------------------------------------------------------ live broker session (kotak / zerodha)
    def _live(t: Terminal):
        if t.live is None:
            raise ValueError("NO_LIVE_PROVIDER: set DATA_SOURCE=kotak|zerodha (and/or BROKER=... with TRADING_ENV=LIVE) plus credentials in .env")
        return t.live

    @app.get("/api/broker/live/status")
    @app.get("/api/broker/kotak/status", include_in_schema=False)
    @app.get("/api/broker/zerodha/status", include_in_schema=False)
    async def live_status(user: dict = Depends(require("viewer"))):
        t = T()
        if t.live is None:
            return ok({"configured": False, "provider": None, "hint": "Set DATA_SOURCE=kotak or DATA_SOURCE=zerodha plus credentials in .env"})
        return ok({"configured": True, **t.live.status(), "chain": t.live_chain_stats, "feed": t.feed.status()})

    @app.post("/api/broker/live/connect")
    @app.post("/api/broker/kotak/connect", include_in_schema=False)
    @app.post("/api/broker/zerodha/connect", include_in_schema=False)
    async def live_connect(user: dict = Depends(require("admin"))):
        t = T()
        live = _live(t)
        live.authenticated = False
        result = await live.connect()
        found = await live.resolve_index_tokens(t.universe.all(), include_vix=True)
        if t.feed.name in ("kotak_neo", "zerodha_kite"):
            await t.feed.reconnect()
        t.audit.record("LIVE_SESSION_RECONNECT", {"provider": live.provider, "tokens": list(found)}, user["username"])
        return ok({"provider": live.provider, "login": result, "tokens": found})

    @app.get("/api/broker/zerodha/login-url")
    async def zerodha_login_url(user: dict = Depends(require("admin"))):
        live = _live(T())
        if live.provider != "zerodha":
            raise ValueError("PROVIDER_IS_NOT_ZERODHA")
        return ok({"login_url": live.login_url()})

    class ZerodhaSessionBody(BaseModel):
        request_token: str

    @app.post("/api/broker/zerodha/session")
    async def zerodha_session(body: ZerodhaSessionBody, user: dict = Depends(require("admin"))):
        t = T()
        live = _live(t)
        if live.provider != "zerodha":
            raise ValueError("PROVIDER_IS_NOT_ZERODHA")
        result = await live.exchange_request_token(body.request_token.strip())
        found = await live.resolve_index_tokens(t.universe.all(), include_vix=True)
        if t.feed.name == "zerodha_kite":
            await t.feed.reconnect()
        t.audit.record("ZERODHA_SESSION_CREATED", {"user_id": result.get("user_id")}, user["username"])
        return ok({**result, "tokens": found})

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        user = ws_user(ws)
        if user is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        t = T()
        t.ws_clients.add(ws)
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)

        async def _fan(topic: str, payload: Any) -> None:
            try:
                queue.put_nowait({"type": "event", "topic": topic, "payload": payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload})
            except asyncio.QueueFull:
                pass

        t.bus.subscribe("*", _fan)
        try:
            await ws.send_text(json.dumps({"type": "snapshot", "data": t.snapshot()}, default=str))

            async def _pump():
                while True:
                    item = await queue.get()
                    await ws.send_text(json.dumps(item, default=str))

            pump = asyncio.create_task(_pump())
            try:
                while True:
                    msg = await ws.receive_text()
                    if msg == "ping":
                        await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
            finally:
                pump.cancel()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            t.bus.unsubscribe("*", _fan)
            t.ws_clients.discard(ws)

    return app
