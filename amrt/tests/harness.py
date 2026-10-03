"""Test rigs: a PAPER app on the SIMULATED market, and a LIVE_CAPABLE app wired to a controllable fake broker.

The fake broker sits behind the real LiveBrokerVenue, so every live-path test exercises the real
gateway ticket checks, Risk Kernel, order book and reconciler — only the broker itself is simulated.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
from pathlib import Path

from amrt.app import AmrtApp
from amrt.brokers.base import BrokerSession, LiveBrokerVenue
from amrt.brokers.kotak import PROFILE as KOTAK_PROFILE
from amrt.config import Settings
from amrt.core.clock import IST, ManualClock
from amrt.core.enums import AuthorizationKind, DataLabel, HealthState, Mode, OrderPurpose, OrderType, Origin, Side
from amrt.core.errors import BrokerRejected, BrokerTimeout
from amrt.execution.accounts import Account
from amrt.execution.intents import AuthorizationContext
from amrt.execution.venue import BrokerAck, FundsReport, OrderStatusReport, PositionReport
from amrt.marketdata.models import Instrument, Quote
from amrt.risk.policy import RiskPolicy
from amrt.security.identity import Principal, PrincipalKind

OWNER = Principal.of(PrincipalKind.OWNER, "owner")
CRITICAL = ("gateway", "event_store", "database", "portfolio_risk_monitor", "safety_monitor", "reconciler", "master_agent")
EXPIRY = dt.date(2026, 10, 6)


def clock_at(h: int = 9, m: int = 25, day: dt.date = dt.date(2026, 10, 5)) -> ManualClock:
    return ManualClock(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=IST))


def env(monkeypatch, tmp_path: Path, **extra: str) -> None:
    base = {"AMRT_RUNTIME_DIR": str(tmp_path / "rt"), "AMRT_UNDERLYINGS": "NIFTY", "AMRT_DATABASE_URL": "", "AMRT_REDIS_URL": "",
            "AMRT_ENVIRONMENT": "PAPER_ONLY", "AMRT_LIVE_ORDERS_ENABLED": "false", "AMRT_SIMULATED_MARKET": "false", "AMRT_DATA_BROKER": "",
            "AMRT_REPLAY_FILE": "", "AMRT_LLM_ENABLED": "false"}
    base.update(extra)
    monkeypatch.setitem(Settings.model_config, "env_file", None)     # never read a developer's real .env during tests
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    for k in ("NEO_CONSUMER_KEY", "ZERODHA_API_KEY", "ANGEL_API_KEY", "UPSTOX_ACCESS_TOKEN", "GROWW_API_KEY", "GROWW_ACCESS_TOKEN"):
        if k not in extra:
            monkeypatch.delenv(k, raising=False)


def beat_all(app: AmrtApp) -> None:
    for n in CRITICAL:
        app.health.beat(n, HealthState.HEALTHY)


# ---------------------------------------------------------------- paper rig
def paper_app(monkeypatch, tmp_path: Path, clock: ManualClock | None = None, **extra: str) -> AmrtApp:
    env(monkeypatch, tmp_path, **{"AMRT_SIMULATED_MARKET": "true", **extra})
    return AmrtApp(Settings(), clock=clock or clock_at(9, 14))


async def warm(app: AmrtApp, polls: int = 40, step: float = 10.0) -> None:
    for _ in range(polls):
        await app.feed.poll_once()
        app.clock.advance(step)
    await app.feed.poll_once()


# ---------------------------------------------------------------- live rig
class FakeSession(BrokerSession):
    """A broker that does exactly what the test says: fill, rest, reject, time out (optionally still accepting)."""
    name = "fakebroker"
    profile = KOTAK_PROFILE

    def __init__(self, settings, clock) -> None:
        super().__init__(settings, clock)
        self.behaviour = "fill"            # fill | open | reject | timeout | timeout_accepted | error
        self.fill_price: float | None = None
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, list] = {}   # instrument_key -> [qty, avg, trading_symbol]
        self.funds = {"available": 900_000.0, "margin_used": 100_000.0, "total": 1_000_000.0}
        self.places = 0
        self.cancels = 0
        self._ids = itertools.count(1)
        self.fail_reads = False
        self.authenticated = True

    def credentials_present(self) -> bool:
        return True

    def _login_sync(self) -> dict:
        return {"ok": True}

    async def fetch_chain(self, underlying, expiry_iso, instruments):  # pragma: no cover - not used by the rig
        raise NotImplementedError

    async def fetch_expiries(self, underlying):  # pragma: no cover
        raise NotImplementedError

    async def fetch_quotes(self, instruments):  # pragma: no cover
        return []

    def _book(self, req, status: str, filled: int) -> str:
        oid = f"FB{next(self._ids):05d}"
        px = self.fill_price or req.limit_price or 100.0
        self.orders[oid] = {"broker_order_id": oid, "client_tag": req.client_tag, "trading_symbol": req.trading_symbol, "side": req.side,
                            "quantity": req.quantity, "filled_qty": filled, "avg_price": px if filled else 0.0, "status": status}
        if filled:
            p = self.positions.setdefault(req.instrument_key, [0, 0.0, req.trading_symbol])
            signed = filled if req.side == Side.BUY else -filled
            p[1] = px if p[0] == 0 else p[1]
            p[0] += signed
        return oid

    async def _place(self, req) -> BrokerAck:
        self.places += 1
        b = self.behaviour
        if b == "reject":
            raise BrokerRejected("RMS: insufficient margin")
        if b == "timeout":
            raise BrokerTimeout("fakebroker.place_order timed out")
        if b == "timeout_accepted":            # the broker took the order but the answer never arrived
            self._book(req, "FILLED", req.quantity)
            raise BrokerTimeout("fakebroker.place_order timed out")
        if b == "error":
            raise ConnectionResetError("connection reset by peer")
        if b == "open":
            oid = self._book(req, "OPEN", 0)
            return BrokerAck(accepted=True, broker_order_id=oid, status="OPEN")
        oid = self._book(req, "FILLED", req.quantity)
        return BrokerAck(accepted=True, broker_order_id=oid, status="FILLED", filled_qty=req.quantity, avg_price=self.orders[oid]["avg_price"])

    async def _cancel(self, broker_order_id: str) -> BrokerAck:
        self.cancels += 1
        o = self.orders[broker_order_id]
        o["status"] = "CANCELLED"
        return BrokerAck(accepted=True, broker_order_id=broker_order_id, status="CANCELLED")

    async def fetch_order_book(self):
        if self.fail_reads:
            raise BrokerTimeout("order book read timed out")
        return [OrderStatusReport(**{k: o[k] for k in ("broker_order_id", "client_tag", "trading_symbol", "side", "quantity", "filled_qty", "avg_price", "status")})
                for o in self.orders.values()]

    async def fetch_positions(self):
        if self.fail_reads:
            raise BrokerTimeout("positions read timed out")
        return [PositionReport(trading_symbol=v[2], instrument_key=k, net_qty=v[0], avg_price=v[1]) for k, v in self.positions.items() if v[0]]

    async def fetch_funds(self):
        return FundsReport(available=self.funds["available"], margin_used=self.funds["margin_used"], total=self.funds["total"], ts=self.clock.ts())


class LiveRig:
    ACCOUNT = "FAKE-LIVE"

    def __init__(self, app: AmrtApp, session: FakeSession) -> None:
        self.app, self.session = app, session
        self.src = app.hub.register_source("fakebroker", live=True)
        self.src.authenticated = self.src.connected = True
        self.keys: dict[tuple[float, str], str] = {}
        for strike in (24800, 24900, 25000, 25100, 25200):
            for ot in ("CE", "PE"):
                inst = app.instruments.option("NIFTY", EXPIRY, strike, ot)
                app.instruments.add(inst, source="fakebroker")
                self.keys[(strike, ot)] = inst.key
        app.instruments.underlying("NIFTY")
        self.spot_key = Instrument.underlying_key("NSE", "NIFTY")

    def key(self, strike: float, ot: str) -> str:
        return self.keys[(strike, ot)]

    def quote(self, key: str, ltp: float, spread: float = 1.0, drift_s: float = 0.2) -> None:
        now = self.app.clock.ts()
        q = Quote(instrument_key=key, ltp=ltp, bid=round(ltp - spread / 2, 2), ask=round(ltp + spread / 2, 2), exchange_ts=now - drift_s, recv_ts=now,
                  source="fakebroker", label=DataLabel.LIVE_UNVERIFIED)
        assert self.app.hub.ingest(q)

    def fresh_market(self, spot: float = 25000.0) -> None:
        self.quote(self.spot_key, spot, spread=0.0)
        for (strike, ot), k in self.keys.items():
            intrinsic = max(0.0, spot - strike) if ot == "CE" else max(0.0, strike - spot)
            self.quote(k, round(intrinsic + 60.0, 2))

    async def ready(self) -> None:
        """Bring the rig to a state where new risk is permitted: fresh data, reconciled, healthy, MANUAL mode."""
        self.fresh_market()
        beat_all(self.app)
        self.app.health.beat(f"broker:{self.ACCOUNT}", HealthState.HEALTHY)
        await self.app.reconciler.reconcile_all()
        beat_all(self.app)

    def intent(self, strike=25000, ot="CE", side=Side.BUY, lots=1, order_type=OrderType.LIMIT, price=None, purpose=OrderPurpose.ENTRY,
               reduce_only=False, origin=Origin.OWNER_MANUAL, mode: Mode | None = None, idem=None, account: str | None = None):
        now = self.app.clock.ts()
        auth = AuthorizationContext(kind=AuthorizationKind.OWNER_APPROVAL, approved_by="owner", approval_id="T", step_up_at=now, approved_at=now)
        key = self.key(strike, ot)
        if price is None and order_type == OrderType.LIMIT:
            q = self.app.hub.quotes.get(key)
            price = (q.ask if side == Side.BUY else q.bid) if q else 50.0
        acct = account or self.ACCOUNT
        return self.app.pipeline.make_intent(account_id=acct, broker=self.app.accounts.get(acct).broker, mode=mode or self.app.mode_ctl.mode,
                                             instrument_key=key, side=side, lots=lots, order_type=order_type, limit_price=price, purpose=purpose,
                                             reduce_only=reduce_only, origin=origin, authorization=auth, correlation_id="TEST",
                                             decision_id=idem, idem_parts=(idem,) if idem else ())


def live_app(monkeypatch, tmp_path: Path, clock: ManualClock | None = None, naked: bool = True, **extra: str) -> LiveRig:
    env(monkeypatch, tmp_path, AMRT_ENVIRONMENT="LIVE_CAPABLE", AMRT_LIVE_ORDERS_ENABLED="true", AMRT_ORDER_ACK_TIMEOUT_SECONDS="2",
        AMRT_UNKNOWN_GRACE_SECONDS="30", **extra)
    app = AmrtApp(Settings(), clock=clock or clock_at(9, 25))
    sess = FakeSession(app.settings, app.clock)
    app.accounts.add(Account(LiveRig.ACCOUNT, "fakebroker", "LIVE", "Fake live account"))
    app.gateway.register_venue(LiveRig.ACCOUNT, LiveBrokerVenue(app.p_gateway, sess, LiveRig.ACCOUNT, app.gateway.verifier(), app.clock))
    app.health.register(f"broker:{LiveRig.ACCOUNT}", critical=False, period_s=30.0, kind="broker")
    pol = RiskPolicy(account_id=LiveRig.ACCOUNT, allow_naked_short=naked, max_lots_per_order=2, max_open_lots=8)
    p = app.policies.propose("RISK", pol, OWNER)
    app.policies.activate(p["policy_id"], OWNER)
    app.mode_ctl.mode = Mode.MANUAL            # test shortcut; the guarded transition itself is covered in test_modes.py
    return LiveRig(app, sess)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)
