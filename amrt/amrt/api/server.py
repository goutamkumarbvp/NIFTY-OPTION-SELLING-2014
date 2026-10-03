"""HTTP API + WebSocket for the dashboard.

Security:
* Session cookie (HttpOnly, SameSite=Strict; Secure when AMRT_COOKIE_SECURE=true) and a per-session CSRF
  token that every state-changing request must echo in the X-AMRT-CSRF header.
* Capability checks use the session principal (OWNER / OPERATOR / READ_ONLY); the API never
  acts with a component principal on the user's behalf except where noted (order cancel goes
  through the mode-controller pipeline after the owner's APPROVE_ACTION check).
* Sensitive actions need a fresh step-up (password re-entry within AMRT_STEP_UP_TTL_SECONDS).
  Engaging the kill switch and Manual Quick Exit deliberately do not, so protection is never slowed.
* Rate limits on login and on mutating calls; strict security headers; no-store caching.
* Errors are typed and never include secrets (the redactor runs on every log record).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from amrt.analytics.fii_dii import CLIENT_TYPES, METHODOLOGY, aggregate_cash, participant_series
from amrt.analytics.option_chain import analyze_chain
from amrt.brokers.registry import capability_matrix
from amrt.core.enums import DataLabel, Mode
from amrt.core.errors import AmrtError, AuthenticationRequired, PermissionDenied
from amrt.core.metrics import METRICS
from amrt.marketdata.instruments import spec as und_spec
from amrt.marketdata.models import ChainRow, ChainSnapshot
from amrt.quant import backtest as bt
from amrt.quant.strategies import REGISTRY
from amrt.risk.policy import AutomationPolicy, RiskPolicy
from amrt.security.auth import Session
from amrt.security.identity import Capability, require
from amrt.storage.cache import RateLimiter
from amrt.storage.db import chain_snapshots, decisions

log = logging.getLogger("amrt.api")
COOKIE = "amrt_session"
STATUS_CODES = {"PERMISSION_DENIED": 403, "PAPER_ISOLATION_VIOLATION": 403, "AUTHENTICATION_REQUIRED": 401, "STEP_UP_REQUIRED": 428,
                "INVALID_TRANSITION": 409, "NOT READY": 409, "HARD_LIMIT_VIOLATION": 422, "SCHEMA_VIOLATION": 422, "RISK_REJECTED": 422,
                "DATA UNAVAILABLE": 503, "QUARANTINED": 503, "DUPLICATE_ORDER_RISK": 409}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "connect-src 'self' ws: wss:; media-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()", "Cross-Origin-Opener-Policy": "same-origin",
}


# ------------------------------------------------------------------ bodies
class SetupBody(BaseModel):
    code: str
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=200)
    totp_secret: str | None = None


class LoginBody(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=200)
    totp: str | None = Field(None, max_length=10)


class StepUpBody(BaseModel):
    password: str = Field(max_length=200)
    totp: str | None = Field(None, max_length=10)


class ReasonBody(BaseModel):
    reason: str = Field("", max_length=500)


class ModeBody(BaseModel):
    target: Mode
    confirmation: str = Field("", max_length=60)
    reason: str = Field("", max_length=500)


class ToggleBody(BaseModel):
    on: bool


class AcceptBody(BaseModel):
    modifications: dict[str, Any] | None = None


class ManualOrderBody(BaseModel):
    account_id: str
    instrument_key: str
    side: str = Field(pattern="^(BUY|SELL)$")
    lots: int = Field(gt=0, le=1000)
    order_type: str = Field("LIMIT", pattern="^(LIMIT|MARKET)$")
    limit_price: float | None = Field(None, gt=0)
    reduce_only: bool = False
    note: str = Field("", max_length=300)


class QuickExitBody(BaseModel):
    account_id: str
    confirmation: str = Field(max_length=20)


class FreezeReleaseBody(BaseModel):
    codes: list[str] | None = None


class PolicyBody(BaseModel):
    kind: str = Field(pattern="^(risk|automation)$")
    body: dict[str, Any]
    note: str = Field("", max_length=500)


class ImportBody(BaseModel):
    kind: str = Field(pattern="^(participant_oi|fii_dii_cash)$")
    content: str = Field(max_length=2_000_000)
    source: str = Field(max_length=300)
    file_date: str | None = None


class ResolveBody(BaseModel):
    resolution: str = Field(min_length=3, max_length=2000)


class BacktestBody(BaseModel):
    underlying: str = "NIFTY"
    strategy_id: str = "ROLLING_ATM_IRON_FLY"
    version: str = "1.0"
    start: str | None = None
    end: str | None = None
    walk_forward_folds: int = Field(4, ge=2, le=12)


# ------------------------------------------------------------------ helpers
def _json(o: Any) -> Any:
    return json.loads(json.dumps(o, default=str))


def chain_payload(snap: ChainSnapshot | None, analytics, label: str, age_ms: float | None) -> dict:
    if snap is None:
        return {"label": DataLabel.UNAVAILABLE.value, "status": "DATA UNAVAILABLE"}
    return {"label": label, "age_ms": age_ms, "underlying": snap.underlying, "exchange": snap.exchange.value, "expiry": snap.expiry.isoformat(),
            "spot": snap.spot, "ts": snap.ts, "source": snap.source, "strike_step": snap.strike_step,
            "rows": [r.model_dump(mode="json") for r in snap.sorted_rows()], "analytics": analytics.model_dump(mode="json") if analytics else None}


def load_snapshots(app, underlying: str, start: str | None, end: str | None) -> list[ChainSnapshot]:
    q = select(chain_snapshots).where(chain_snapshots.c.underlying == underlying.upper()).order_by(chain_snapshots.c.ts)
    out = []
    sp = und_spec(underlying)
    with app.db.engine.connect() as conn:
        for r in conn.execute(q):
            day = dt.datetime.fromtimestamp(r.ts, dt.UTC).date().isoformat()
            if (start and day < start) or (end and day > end):
                continue
            out.append(ChainSnapshot(underlying=r.underlying, exchange=sp.exchange, expiry=dt.date.fromisoformat(r.expiry), spot=(r.analytics or {}).get("spot"),
                                     ts=r.ts, source=r.source, label=DataLabel(r.label), rows=[ChainRow.model_validate(x) for x in r.rows],
                                     strike_step=(r.analytics or {}).get("strike_step")))
    return out


def create_api(app, static_dir: str | Path | None = None, start_loops: bool = True) -> FastAPI:
    s = app.settings
    csrf: dict[str, str] = {}
    login_limiter = RateLimiter(app.cache, limit=10, window_seconds=60)
    write_limiter = RateLimiter(app.cache, limit=120, window_seconds=60)
    sockets: set[WebSocket] = set()
    queue: asyncio.Queue | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal queue
        queue = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()

        def listener(topic: str, payload: dict) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, {"topic": topic, "payload": _json(payload)})
            except Exception:  # noqa: BLE001 - a full queue drops dashboard pushes, never blocks the engine
                pass
        app.ws_listeners.append(listener)
        pump = asyncio.create_task(_pump())
        if start_loops:
            await app.start()
        if app.auth.user_count() == 0:
            code = app.auth.issue_setup_code()
            log.warning("No users yet. One-time owner setup code (console only): %s", code)
            print(f"\n=== AMRT owner setup code (one-time, expires on restart): {code} ===\n", flush=True)
        try:
            yield
        finally:
            pump.cancel()
            if start_loops:
                await app.stop()

    async def _pump() -> None:
        while True:
            msg = await queue.get()
            dead = []
            for ws in list(sockets):
                try:
                    await ws.send_json(msg)
                except Exception:  # noqa: BLE001
                    dead.append(ws)
            for ws in dead:
                sockets.discard(ws)

    api = FastAPI(title="AI Market Risk Terminal", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")

    @api.middleware("http")
    async def headers(request: Request, call_next):
        try:
            resp = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            resp = JSONResponse({"error": "INTERNAL_ERROR", "message": "internal error (see server log)"}, status_code=500)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        METRICS.inc("http_requests_total", method=request.method, status=str(resp.status_code))
        return resp

    @api.exception_handler(AmrtError)
    async def amrt_error(_: Request, exc: AmrtError):
        code = STATUS_CODES.get(exc.code, 400)
        detail = {k: v for k, v in (exc.detail or {}).items() if k not in ("principal",)}
        return JSONResponse({"error": exc.code, "message": str(exc), "detail": _json(detail)}, status_code=code)

    @api.exception_handler(KeyError)
    async def key_error(_: Request, exc: KeyError):
        return JSONResponse({"error": "NOT_FOUND", "message": str(exc).strip("'\"")}, status_code=404)

    # ------------------------------------------------------------ auth deps
    def session(request: Request) -> Session:
        return app.auth.session(request.cookies.get(COOKIE))

    def writer(request: Request, sess: Session = Depends(session)) -> Session:
        if not secrets.compare_digest(request.headers.get("X-AMRT-CSRF", ""), csrf.get(sess.token, "-")):
            raise PermissionDenied("missing or invalid CSRF token")
        if not write_limiter.allow(f"w:{sess.username}"):
            raise PermissionDenied("rate limit exceeded")
        return sess

    def stepped(sess: Session = Depends(writer)) -> Session:
        app.auth.require_step_up(sess)
        return sess

    def can(sess: Session, cap: Capability, action: str) -> None:
        require(sess.principal, cap, action)

    # ------------------------------------------------------------ health (unauthenticated, minimal)
    @api.get("/api/health/live")
    def live():
        return {"ok": True}

    @api.get("/api/health/ready")
    def ready():
        crit = {n: app.health.state(n).value for n in ("database", "event_store", "gateway", "portfolio_risk_monitor", "safety_monitor")}
        ok = all(v in ("HEALTHY", "DEGRADED") for v in crit.values())
        return JSONResponse({"ready": ok, "components": crit}, status_code=200 if ok else 503)

    # ------------------------------------------------------------ auth
    @api.get("/api/auth/state")
    def auth_state(request: Request):
        try:
            sess = session(request)
            user = {"username": sess.username, "role": sess.role, "step_up_age_s": round(app.clock.ts() - sess.step_up_at, 1), "csrf": csrf.get(sess.token)}
        except AuthenticationRequired:
            user = None
        return {"setup_required": app.auth.user_count() == 0, "user": user, "step_up_ttl_s": s.step_up_ttl_seconds}

    @api.post("/api/auth/setup")
    def setup(body: SetupBody, request: Request):
        if not login_limiter.allow(f"setup:{request.client.host if request.client else '-'}"):
            raise PermissionDenied("rate limit exceeded")
        app.auth.complete_setup(body.code, body.username, body.password, body.totp_secret)
        app.events.append("OWNER_CREATED", {"username": body.username}, app.p_system)
        return {"ok": True}

    @api.post("/api/auth/login")
    def login(body: LoginBody, request: Request):
        ip = request.client.host if request.client else "-"
        if not login_limiter.allow(f"login:{ip}"):
            raise PermissionDenied("too many login attempts; wait a minute")
        try:
            sess = app.auth.login(body.username, body.password, body.totp)
        except AuthenticationRequired:
            app.events.append("LOGIN_FAILED", {"username": body.username[:40], "ip": ip}, app.p_system)
            raise
        token = secrets.token_urlsafe(24)
        csrf[sess.token] = token
        app.events.append("LOGIN", {"username": sess.username, "role": sess.role, "ip": ip}, sess.principal)
        resp = JSONResponse({"username": sess.username, "role": sess.role, "csrf": token})
        resp.set_cookie(COOKIE, sess.token, httponly=True, samesite="strict", secure=s.cookie_secure, max_age=s.session_ttl_minutes * 60, path="/")
        return resp

    @api.post("/api/auth/logout")
    def logout(request: Request):
        tok = request.cookies.get(COOKIE)
        if tok:
            app.auth.logout(tok)
            csrf.pop(tok, None)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @api.post("/api/auth/step-up")
    def step_up(body: StepUpBody, sess: Session = Depends(writer)):
        app.auth.step_up(sess.token, body.password, body.totp)
        app.events.append("STEP_UP", {"username": sess.username}, sess.principal)
        return {"ok": True, "valid_for_s": s.step_up_ttl_seconds}

    # ------------------------------------------------------------ status & market
    @api.get("/api/status")
    def status(sess: Session = Depends(session)):
        can(sess, Capability.READ_HEALTH, "status")
        return _json(app.status())

    @api.get("/api/metrics", response_class=PlainTextResponse)
    def metrics(sess: Session = Depends(session)):
        can(sess, Capability.READ_HEALTH, "metrics")
        return METRICS.render()

    @api.get("/api/market/underlyings")
    def underlyings(sess: Session = Depends(session)):
        can(sess, Capability.READ_MARKET_DATA, "market")
        return {"underlyings": s.underlying_list}

    @api.get("/api/market/chain/{und}")
    def chain(und: str, sess: Session = Depends(session)):
        can(sess, Capability.READ_MARKET_DATA, "chain")
        und = und.upper()
        snap = app.hub.chain(und)
        label = app.hub.chain_label(snap, 3000).value
        a = app.feed.analytics.get(und) if app.feed else None
        return _json({**chain_payload(snap, a, label, app.hub.chain_age_ms(und)), "windows": (app.feed.windows.get(und) if app.feed else {}),
                      "feed_error": (app.feed.errors.get(und) if app.feed else "no market data source configured")})

    @api.get("/api/market/pcr/{und}")
    def pcr(und: str, sess: Session = Depends(session)):
        can(sess, Capability.READ_MARKET_DATA, "pcr")
        und = und.upper()
        snap = app.hub.chain(und)
        if snap is None:
            return {"label": DataLabel.UNAVAILABLE.value, "series": []}
        series = []
        for h in app.hub.history(und, snap.expiry.isoformat())[-720:]:
            a = analyze_chain(h, s.chain_strikes_each_side)
            series.append({"ts": h.ts, "spot": h.spot, "label": h.label.value, "status": a.status,
                           "pcr_total_oi": a.pcr_total_oi.value if a.pcr_total_oi else None,
                           "pcr_change_oi": a.pcr_change_oi.value if a.pcr_change_oi else None,
                           "pcr_change_oi_interpretation": a.pcr_change_oi.interpretation if a.pcr_change_oi else None})
        return {"underlying": und, "expiry": snap.expiry.isoformat(), "label": app.hub.chain_label(snap, 3000).value, "series": series,
                "definition": "Total OI PCR = ΣPE OI ÷ ΣCE OI; Change-in-OI PCR = ΣPE ΔOI ÷ ΣCE ΔOI over ATM ± N strikes; undefined when the denominator is 0"}

    @api.get("/api/market/fii-dii")
    def fiidii(period: str = "D", sess: Session = Depends(session)):
        can(sess, Capability.READ_MARKET_DATA, "fii/dii")
        part = app.fiidii.participant()
        cash = app.fiidii.cash()
        return _json({"methodology": METHODOLOGY, "participant": {ct: participant_series(part, ct)[-60:] for ct in CLIENT_TYPES},
                      "cash": {cat: aggregate_cash(cash, cat, period) for cat in ("FII", "DII")} if cash else {},
                      "label": "OFFICIAL END-OF-DAY (imported)" if part or cash else DataLabel.UNAVAILABLE.value})

    @api.post("/api/market/fii-dii/import")
    def fiidii_import(body: ImportBody, sess: Session = Depends(writer)):
        if body.kind == "participant_oi":
            return app.fiidii.import_participant(sess.principal, body.content, body.source, dt.date.fromisoformat(body.file_date) if body.file_date else None)
        return app.fiidii.import_cash(sess.principal, body.content, body.source)

    @api.get("/api/brokers")
    def brokers(sess: Session = Depends(session)):
        can(sess, Capability.READ_HEALTH, "brokers")
        return _json({"capability_matrix": capability_matrix(), "status": app.brokers.status(), "data_broker": s.data_broker or None})

    # ------------------------------------------------------------ decisions & approvals
    @api.get("/api/decisions/latest")
    def decisions_latest(sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "decisions")
        out = {}
        for und, p in app.decisions.latest.items():
            out[und] = {**{k: v for k, v in p.items() if k != "outputs"}, "narrative": app.decisions.narratives.get(p["decision_id"])}
        return _json(out)

    @api.get("/api/decisions")
    def decisions_list(limit: int = 50, sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "decisions")
        with app.db.engine.connect() as conn:
            rows = conn.execute(select(decisions.c.decision_id, decisions.c.ts, decisions.c.underlying, decisions.c.status)
                                .order_by(decisions.c.ts.desc()).limit(min(limit, 500)))
            return [dict(r._mapping) for r in rows]

    @api.get("/api/decisions/{decision_id}")
    def decision_get(decision_id: str, sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "decision")
        with app.db.engine.connect() as conn:
            r = conn.execute(select(decisions).where(decisions.c.decision_id == decision_id)).first()
        if r is None:
            raise KeyError(decision_id)
        return _json({**r.package, "narrative": app.decisions.narratives.get(decision_id),
                      "timeline": [e.to_dict() for e in app.events.by_correlation(decision_id)]})

    @api.get("/api/approvals")
    def approvals(sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "approvals")
        return _json({"pending": app.approvals.pending(), "history": app.approvals.history(50)})

    @api.post("/api/approvals/{aid}/accept")
    async def approval_accept(aid: str, body: AcceptBody, sess: Session = Depends(stepped)):
        return _json(await app.approvals.accept(aid, sess.principal, sess.step_up_at, body.modifications))

    @api.post("/api/approvals/{aid}/reject")
    def approval_reject(aid: str, body: ReasonBody, sess: Session = Depends(writer)):
        return app.approvals.reject(aid, sess.principal, body.reason)

    # ------------------------------------------------------------ orders & portfolio
    @api.get("/api/orders")
    def orders(account_id: str | None = None, limit: int = 200, sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "orders")
        return _json(app.book.query(account_id, None, min(limit, 1000)))

    @api.get("/api/orders/{intent_id}")
    def order_get(intent_id: str, sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "order")
        rec = app.book.get(intent_id)
        if rec is None:
            raise KeyError(intent_id)
        return _json({**rec, "events": [e.to_dict() for e in app.events.by_correlation(rec["intent"].get("decision_id") or rec["intent"]["correlation_id"])]})

    @api.post("/api/orders/manual")
    async def order_manual(body: ManualOrderBody, sess: Session = Depends(stepped)):
        return _json(await app.approvals.manual_ticket(sess.principal, sess.step_up_at, account_id=body.account_id, instrument_key=body.instrument_key,
                                                       side=body.side, lots=body.lots, order_type=body.order_type, limit_price=body.limit_price,
                                                       reduce_only=body.reduce_only, note=body.note))

    @api.post("/api/orders/{intent_id}/cancel")
    async def order_cancel(intent_id: str, body: ReasonBody, sess: Session = Depends(stepped)):
        can(sess, Capability.APPROVE_ACTION, "cancel order")
        app.events.append("CANCEL_REQUESTED", {"intent_id": intent_id, "reason": body.reason}, sess.principal)
        return _json(await app.gateway.cancel(app.p_mode, intent_id, body.reason or f"owner {sess.username}"))

    @api.get("/api/portfolio")
    def portfolio(sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "portfolio")
        out = []
        for a in app.accounts.accounts.values():
            pol, meta = app.builder.policy_for(a.account_id)
            age = pol.max_data_age_ms if pol else 3000
            v = app.ledger.valuation(a.account_id, lambda k, age=age: app.builder.mark(k, age), lambda k, age=age: app.builder.spot_for(k, age))
            out.append({"account": {"account_id": a.account_id, "kind": a.kind, "broker": a.broker, "display": a.display, "funds": a.funds},
                        "label": "SIMULATED" if a.kind == "PAPER" else "LIVE", "valuation": v, "risk": app.path_a.last.get(a.account_id),
                        "reconciliation": app.reconciler.account_status(a.account_id), "policy": {"label": (f"v{meta['version']}" if meta else "DEFAULT PAPER" if pol else "NONE"),
                                                                                                    "body": pol.model_dump(mode='json') if pol else None}})
        return _json(out)

    # ------------------------------------------------------------ risk, policies, mode, safety
    @api.get("/api/risk")
    def risk(sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "risk")
        return _json({"safety": app.safety.snapshot(), "blockers": app.safety.new_risk_blockers(), "path_a": app.path_a.last,
                      "path_b": app.path_b.findings[-50:], "watchdog": app.watchdog.trips[-20:], "kernel_rules": RULES_DOC})

    @api.get("/api/policies")
    def policies(sess: Session = Depends(session)):
        can(sess, Capability.READ_PORTFOLIO, "policies")
        return _json({"policies": app.policies.list(), "hard_limits": s.hard_limits().model_dump()})

    @api.post("/api/policies")
    def policy_propose(body: PolicyBody, sess: Session = Depends(stepped)):
        model = RiskPolicy.model_validate(body.body) if body.kind == "risk" else AutomationPolicy.model_validate(body.body)
        return _json(app.policies.propose(body.kind, model, sess.principal, body.note))

    @api.post("/api/policies/{policy_id}/activate")
    def policy_activate(policy_id: str, sess: Session = Depends(stepped)):
        return _json(app.policies.activate(policy_id, sess.principal))

    @api.get("/api/mode")
    def mode(sess: Session = Depends(session)):
        can(sess, Capability.READ_HEALTH, "mode")
        return _json(app.mode_ctl.describe())

    @api.post("/api/mode")
    def mode_change(body: ModeBody, sess: Session = Depends(stepped)):
        return _json(app.mode_ctl.transition(sess.principal, body.target, step_up_ok=True, confirmation=body.confirmation, reason=body.reason))

    @api.post("/api/mode/paper-auto-approve")
    def paper_auto(body: ToggleBody, sess: Session = Depends(stepped)):
        app.mode_ctl.set_paper_auto_approve(sess.principal, body.on)
        return _json(app.mode_ctl.describe())

    @api.get("/api/safety/verify")
    def safety_verify(sess: Session = Depends(session)):
        can(sess, Capability.READ_HEALTH, "verify")
        return _json(app.verify_recovery("on-demand"))

    @api.post("/api/safety/kill")
    def kill(body: ReasonBody, sess: Session = Depends(writer)):
        return _json(app.engage_kill(sess.principal, body.reason or "owner engaged"))

    @api.post("/api/safety/kill/release")
    def kill_release(sess: Session = Depends(stepped)):
        return _json(app.release_kill(sess.principal))

    @api.post("/api/safety/freeze/release")
    def freeze_release(body: FreezeReleaseBody, sess: Session = Depends(stepped)):
        return _json(app.release_freeze(sess.principal, body.codes))

    @api.post("/api/safety/recovery-lock/release")
    def lock_release(sess: Session = Depends(stepped)):
        return _json(app.release_recovery_lock(sess.principal))

    @api.post("/api/safety/ai/resume")
    def ai_resume(sess: Session = Depends(stepped)):
        app.safety.resume_ai(sess.principal)
        return {"ok": True}

    @api.post("/api/safety/quick-exit")
    async def quick_exit(body: QuickExitBody, sess: Session = Depends(writer)):
        if body.confirmation.strip().upper() != "EXIT":
            raise PermissionDenied("type EXIT to confirm Manual Quick Exit")
        return _json(await app.quick_exit(sess.principal, body.account_id))

    # ------------------------------------------------------------ reliability, alerts, audit, config
    @api.get("/api/reliability")
    def reliability(sess: Session = Depends(session)):
        can(sess, Capability.READ_HEALTH, "reliability")
        return _json({"health": app.health.describe(), "supervisor": app.supervisor.describe(), "incidents": app.incidents.list(limit=100),
                      "backups": {"last": app.backups.last, "files": app.backups.list()}, "watchdog": {"trips": app.watchdog.trips[-20:]}})

    @api.post("/api/incidents/{iid}/ack")
    def incident_ack(iid: str, sess: Session = Depends(writer)):
        can(sess, Capability.ACK_INCIDENT, "ack incident")
        return _json(app.incidents.update(iid, sess.principal, "ACKNOWLEDGED"))

    @api.post("/api/incidents/{iid}/resolve")
    def incident_resolve(iid: str, body: ResolveBody, sess: Session = Depends(stepped)):
        can(sess, Capability.ACK_INCIDENT, "resolve incident")
        ver = app.verify_recovery(f"resolve:{iid}")
        rec = app.incidents.get(iid)
        if rec is None:
            raise KeyError(iid)
        if rec["severity"] in ("SEV1", "SEV2") and not ver["passed"]:
            raise PermissionDenied("recovery verification must pass before a SEV1/SEV2 incident is resolved", failed=ver["failed"])
        return _json(app.incidents.resolve(iid, sess.principal, body.resolution, ver))

    @api.post("/api/components/{name}/unquarantine")
    def unquarantine(name: str, sess: Session = Depends(stepped)):
        app.unquarantine(sess.principal, name)
        return {"ok": True}

    @api.get("/api/alerts")
    def alerts(sess: Session = Depends(session)):
        can(sess, Capability.READ_HEALTH, "alerts")
        return _json({"alerts": app.alerts.recent(200), "delivery": app.alerts.delivery_health()})

    @api.post("/api/alerts/{aid}/ack")
    def alert_ack(aid: str, sess: Session = Depends(writer)):
        can(sess, Capability.ACK_INCIDENT, "ack alert")
        app.alerts.ack(aid, sess.username)
        return {"ok": True}

    @api.post("/api/backups")
    def backup_now(sess: Session = Depends(writer)):
        return _json(app.backups.run(sess.principal, "manual"))

    @api.get("/api/config")
    def config(sess: Session = Depends(session)):
        can(sess, Capability.READ_AUDIT, "config")
        return _json({"settings": s.public_view(), "integrity": app.config.status(), "hard_limits": s.hard_limits().model_dump()})

    @api.post("/api/config/known-good")
    def config_good(sess: Session = Depends(stepped)):
        return _json(app.config.mark_known_good(sess.principal))

    @api.get("/api/audit")
    def audit(type_prefix: str | None = None, limit: int = 200, sess: Session = Depends(session)):
        can(sess, Capability.READ_AUDIT, "audit")
        return _json([e.to_dict() for e in app.events.tail(min(limit, 2000), type_prefix=type_prefix)])

    @api.get("/api/audit/verify")
    def audit_verify(sess: Session = Depends(session)):
        can(sess, Capability.READ_AUDIT, "audit verify")
        app._chain_check = {**app.events.verify(), "at": app.clock.ts()}
        return _json(app._chain_check)

    @api.get("/api/audit/replay/{correlation_id}")
    def audit_replay(correlation_id: str, sess: Session = Depends(session)):
        can(sess, Capability.READ_AUDIT, "replay")
        evs = app.events.by_correlation(correlation_id)
        return _json({"correlation_id": correlation_id, "events": [e.to_dict() for e in evs], "reconstruction": replay_summary(evs)})

    # ------------------------------------------------------------ backtests
    @api.post("/api/backtest")
    async def run_backtest(body: BacktestBody, sess: Session = Depends(writer)):
        can(sess, Capability.READ_MARKET_DATA, "backtest")
        strat = REGISTRY.get((body.strategy_id, body.version))
        if strat is None:
            raise KeyError(f"{body.strategy_id} {body.version}")
        snaps = await asyncio.to_thread(load_snapshots, app, body.underlying, body.start, body.end)
        if not snaps:
            return {"status": "INSUFFICIENT DATA", "reason": "no stored chain snapshots for this underlying/period"}
        res = await asyncio.to_thread(bt.backtest, strat, snaps)
        wf = await asyncio.to_thread(bt.walk_forward, strat, snaps, body.walk_forward_folds)
        sens = await asyncio.to_thread(bt.sensitivity, strat, snaps)
        valid = bt.validation_summary(wf, res["label"])
        if valid:
            cur = app.db.kv_get("validated_backtests", {}) or {}
            cur[strat.strategy_id] = valid
            app.db.kv_set("validated_backtests", cur, sess.username)
        out = {"backtest": {k: v for k, v in res.items() if k != "trades"}, "trades": res["trades"][-200:], "walk_forward": wf, "sensitivity": sens,
               "stress": bt.stress(strat, snaps[len(snaps) // 2]), "validated": valid}
        app.db.kv_set(f"backtest:{body.underlying}:{strat.strategy_id}", _json({k: v for k, v in out.items() if k != "trades"}), sess.username)
        app.events.append("BACKTEST_RUN", {"underlying": body.underlying, "strategy": strat.strategy_id, "label": res["label"],
                                           "days": res["days_traded"], "validated": bool(valid)}, sess.principal)
        return _json(out)

    # ------------------------------------------------------------ websocket
    @api.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        try:
            app.auth.session(websocket.cookies.get(COOKIE))
        except AuthenticationRequired:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        sockets.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            sockets.discard(websocket)

    # ------------------------------------------------------------ static dashboard
    if static_dir and Path(static_dir).exists():
        api.mount("/", StaticFiles(directory=str(static_dir), html=True), name="dashboard")
    else:
        @api.get("/", response_class=HTMLResponse)
        def index():
            return "<!doctype html><title>AMRT</title><p>AI Market Risk Terminal API is running. The dashboard build was not found " \
                   "(build it with <code>npm run build</code> in <code>frontend/</code>).</p>"

    return api


RULES_DOC = "See docs/RISK_KERNEL.md: RK-001 … RK-028, evaluated on every order; reducing orders are never blocked by new-risk rules."


def replay_summary(evs) -> dict:
    """Rebuild the life of a decision / order purely from the event log."""
    out: dict[str, Any] = {"decision": None, "risk_decisions": [], "orders": {}, "approvals": []}
    for e in evs:
        p = e.payload
        if e.type == "DECISION_PACKAGE":
            out["decision"] = {"status": p.get("status"), "proposed_action": p.get("proposed_action"), "reasons": p.get("reasons")}
        elif e.type == "RISK_DECISION":
            out["risk_decisions"].append({"intent_id": p.get("intent_id"), "approved": p.get("approved"), "failed_rules": p.get("failed_rules")})
        elif e.type.startswith("ORDER_") and p.get("intent_id"):
            out["orders"].setdefault(p["intent_id"], []).append({"ts": e.ts, "event": e.type, **{k: v for k, v in p.items() if k != "intent"}})
        elif e.type.startswith("APPROVAL_"):
            out["approvals"].append({"ts": e.ts, "event": e.type, **p})
    return out
