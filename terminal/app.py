"""Composition root: builds every service once and runs the background loops."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set

from terminal import PRODUCT, __version__
from terminal.agents.orchestrator import Council
from terminal.config import Settings, get_settings
from terminal.core.audit import AuditLog
from terminal.core.bus import EventBus
from terminal.core.models import OptionChain, OptionQuote, OrderSource, TerminalMode, Tick, TradingEnv
from terminal.execution.brokers.base import Broker
from terminal.execution.brokers.live import AngelOneBroker, KotakNeoBroker, ZerodhaBroker
from terminal.execution.brokers.paper import PaperBroker
from terminal.execution.orders import OrderManager
from terminal.execution.positions import PositionManager
from terminal.market.chain import OptionChainBuilder
from terminal.market.angel import AngelOneSession
from terminal.market.feed import AngelOneFeed, KotakNeoFeed, MarketFeed, SimulatedFeed, ZerodhaFeed
from terminal.market.kotak import KotakNeoSession
from terminal.market.zerodha import ZerodhaSession
from terminal.market.processor import MarketDataProcessor
from terminal.market.universe import VIX_SYMBOL, Universe
from terminal.monitoring.health import HealthMonitor
from terminal.notifications.alerts import AlertEngine
from terminal.risk.manager import RiskManager
from terminal.storage.db import Database
from terminal.strategy.engine import StrategyEngine
from terminal.strategy.scheduler import Scheduler

log = logging.getLogger("terminal")


class Terminal:
    def __init__(self, settings: Optional[Settings] = None, seed: Optional[int] = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings
        s.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus()
        self.audit = AuditLog(s.runtime_dir / "audit.jsonl")
        self.db = Database(s.runtime_dir / "terminal.sqlite3")
        self.universe = Universe(s.runtime_dir, s.market_list)
        self.processor = MarketDataProcessor()
        self.chain_builder = OptionChainBuilder(seed=seed if seed is not None else int(time.time()) % 100000)
        self.chains: Dict[str, OptionChain] = {}
        self._quotes: Dict[str, OptionQuote] = {}
        self.mode = TerminalMode(self.db.get_setting("terminal_mode", s.terminal_mode))
        self.env = TradingEnv(s.trading_env)
        if self.env == TradingEnv.LIVE and not s.live_allowed:
            log.warning("TRADING_ENV=LIVE but interlocks not satisfied; forcing PAPER")
            self.env = TradingEnv.PAPER
        self.engine_running = False
        self.paused = False
        self.loop_lag_ms = 0.0
        self.ws_clients: Set = set()
        self.started_at = time.time()
        # feeds & broker: one live broker session (Kotak Neo or Zerodha Kite) shared by feed, chain poller and broker
        self.live = None  # KotakNeoSession | ZerodhaSession | None
        providers = {s.data_source.lower()} | ({s.broker.lower()} if self.env == TradingEnv.LIVE else set())
        providers.discard("simulated")
        providers.discard("paper")
        if len(providers) > 1:
            raise RuntimeError(f"ONE_LIVE_PROVIDER_ONLY: DATA_SOURCE and BROKER must agree ({sorted(providers)})")
        if "kotak" in providers:
            self.live = KotakNeoSession(s, s.runtime_dir)
        elif "zerodha" in providers:
            self.live = ZerodhaSession(s, s.runtime_dir)
        elif "angel" in providers:
            self.live = AngelOneSession(s, s.runtime_dir)
        elif providers:
            raise RuntimeError(f"UNKNOWN_LIVE_PROVIDER: {sorted(providers)}")
        self.live_chain_stats = {"polls": 0, "quotes": 0, "errors": 0, "last_ts": 0.0, "last_error": ""}
        self.feed: MarketFeed = self._make_feed(seed)
        self.broker: Broker = self._make_broker()
        # services
        self.scheduler = Scheduler(s, sim_always_open=(s.data_source == "simulated" and s.sim_always_open))
        self.alerts = AlertEngine(self)
        self.positions = PositionManager(self)
        self.orders = OrderManager(self)
        self.strategies = StrategyEngine(self)
        self.risk = RiskManager(self)
        self.council = Council(self)
        self.health = HealthMonitor(self)
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._chain_refresh_seconds = 2.0 if s.data_source == "simulated" else 3.0
        self.feed.on_tick(self._on_tick)
        self.audit.record("TERMINAL_INIT", {"version": __version__, "mode": self.mode.value, "env": self.env.value, "data_source": s.data_source, "broker": self.broker.name})

    # ------------------------------------------------------------- factories
    def _make_feed(self, seed: Optional[int]) -> MarketFeed:
        s = self.settings
        if s.data_source.lower() == "kotak":
            return KotakNeoFeed(self.universe.all(), self.live)
        if s.data_source.lower() == "zerodha":
            return ZerodhaFeed(self.universe.all(), self.live)
        if s.data_source.lower() == "angel":
            return AngelOneFeed(self.universe.all(), self.live)
        return SimulatedFeed(self.universe.all(), interval=s.tick_interval_seconds, speed=s.sim_speed, always_open=s.sim_always_open, seed=seed)

    def _make_broker(self) -> Broker:
        s = self.settings
        if self.env == TradingEnv.LIVE and s.live_allowed:
            if s.broker.lower() == "kotak":
                return KotakNeoBroker(s, self.live, self.universe)
            if s.broker.lower() == "zerodha":
                return ZerodhaBroker(s, self.live, self.universe)
            if s.broker.lower() == "angel":
                return AngelOneBroker(s, self.live, self.universe)
            raise RuntimeError(f"UNSUPPORTED_LIVE_BROKER:{s.broker}")
        return PaperBroker(capital=s.capital)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        try:
            await self.broker.connect()
        except Exception as exc:
            self.log("CRITICAL", "broker", f"broker connect failed: {exc}")
        try:
            await self.feed.start()
        except Exception as exc:
            self.log("CRITICAL", "feed", f"feed start failed: {exc}")
            await self.alerts.emit("CRITICAL", "feed", "Market feed failed to start", str(exc))
        self._stop = asyncio.Event()
        self._tasks = [asyncio.create_task(self._chain_loop(), name="chain-loop"), asyncio.create_task(self._risk_loop(), name="risk-loop"),
                       asyncio.create_task(self._council_loop(), name="council-loop"), asyncio.create_task(self._broadcast_loop(), name="broadcast-loop"),
                       asyncio.create_task(self._feed_supervisor(), name="feed-supervisor")]
        if self.live is not None:
            self._tasks.append(asyncio.create_task(self._live_chain_loop(), name="live-chain-poller"))
        self.engine_running = True
        self.log("INFO", "terminal", f"{PRODUCT} v{__version__} started: mode={self.mode.value} env={self.env.value} feed={self.feed.name} broker={self.broker.name}")

    async def stop(self) -> None:
        self._stop.set()
        self.engine_running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.feed.stop()
        self.db.close()

    # ------------------------------------------------------------- helpers
    def log(self, level: str, source: str, message: str) -> None:
        getattr(log, level.lower() if level.lower() in ("debug", "info", "warning", "error", "critical") else "info")("[%s] %s", source, message)
        try:
            self.db.log(level, source, message)
        except Exception:
            pass
        self.bus.publish_nowait("log", {"ts": time.time(), "level": level, "source": source, "message": message})

    def quote(self, symbol: str) -> Optional[OptionQuote]:
        return self._quotes.get(symbol)

    def chain_for(self, underlying: str, expiry: Optional[str] = None) -> OptionChain:
        u = self.universe.get(underlying)
        cur = self.chains.get(u.symbol)
        if cur is not None and (expiry is None or cur.expiry == expiry):
            return cur
        spot = self.processor.last_price(u.symbol) or u.base_spot
        vix = self.processor.last_price(VIX_SYMBOL) or 13.0
        ch = self.chain_builder.build(u, spot, vix, expiry)
        for r in ch.rows:
            self._quotes[r.ce.symbol] = r.ce
            self._quotes[r.pe.symbol] = r.pe
        return ch

    async def set_mode(self, mode: TerminalMode, actor: str, reason: str = "") -> TerminalMode:
        if mode == self.mode:
            return mode
        if mode == TerminalMode.AUTO and self.risk.kill_switch:
            raise ValueError("RESET_KILL_SWITCH_FIRST")
        old, self.mode = self.mode, mode
        self.db.set_setting("terminal_mode", mode.value)
        if mode == TerminalMode.AUTO:
            # stale human-approval proposals must not linger; AUTO re-evaluates from scratch
            for plan in self.council.pending_plans():
                plan.status = "EXPIRED"
                self.db.save_plan(plan.model_dump(mode="json"))
            self.council._proposal_cooldown.clear()
        self.audit.record("MODE_CHANGED", {"from": old.value, "to": mode.value, "reason": reason}, actor)
        self.log("WARNING", "terminal", f"Mode {old.value} → {mode.value} by {actor} {reason}")
        await self.alerts.emit("WARNING" if mode == TerminalMode.AUTO else "INFO", "mode", f"Terminal mode: {mode.value}", f"Changed by {actor}. {reason}")
        await self.bus.publish("mode.changed", {"mode": mode.value, "actor": actor})
        return mode

    # ------------------------------------------------------------- loops
    async def _on_tick(self, tick: Tick) -> None:
        self.processor.push(tick)

    async def _chain_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                vix = self.processor.last_price(VIX_SYMBOL) or 13.0
                for u in self.universe.all():
                    spot = self.processor.last_price(u.symbol)
                    if spot is None:
                        continue
                    cur = self.chains.get(u.symbol)
                    expiry = cur.expiry if cur else None
                    # roll to next expiry once the current one has settled
                    if expiry and expiry not in self.chain_builder.expiries(u, 6):
                        expiry = None
                    held = {p.strike for p in self.positions.open_positions() if p.underlying == u.symbol and (expiry is None or p.expiry == expiry)}
                    ch = self.chain_builder.build(u, spot, vix, expiry, extra_strikes=held)
                    self.chains[u.symbol] = ch
                    for r in ch.rows:
                        self._quotes[r.ce.symbol] = r.ce
                        self._quotes[r.pe.symbol] = r.pe
                    self.processor.record_chain(ch)
                    # positions held on other expiries keep receiving marks too
                    other = {p.expiry for p in self.positions.open_positions() if p.underlying == u.symbol and p.expiry != ch.expiry}
                    for exp in other:
                        held = {p.strike for p in self.positions.open_positions() if p.underlying == u.symbol and p.expiry == exp}
                        try:
                            side = self.chain_builder.build(u, spot, vix, exp, extra_strikes=held)
                        except Exception:
                            continue
                        for r in side.rows:
                            self._quotes[r.ce.symbol] = r.ce
                            self._quotes[r.pe.symbol] = r.pe
                self.positions.mark(self.quote)
                await self.strategies.monitor()
            except Exception:
                log.exception("chain loop error")
            self.loop_lag_ms = round((time.perf_counter() - t0) * 1000, 1)
            await asyncio.sleep(self._chain_refresh_seconds)

    async def _live_chain_loop(self) -> None:
        """Poll real option quotes from the live broker and overlay them on the model chain."""
        await asyncio.sleep(5.0)
        while not self._stop.is_set():
            if self.live.missing_credentials() or not self.live.authenticated:
                await asyncio.sleep(30.0)
                continue
            wanted = [x.strip().upper() for x in self.settings.live_chain_underlyings.split(",") if x.strip()] or list(self.council.focus)
            for sym in wanted:
                if self._stop.is_set() or not self.universe.has(sym):
                    continue
                u = self.universe.get(sym)
                cur = self.chains.get(sym)
                expiry = cur.expiry if cur else self.chain_builder.nearest_expiry(u)
                strikes = [r.strike for r in cur.rows] if cur else None
                try:
                    parsed = await self.live.option_quotes(u, expiry, strikes)
                    quotes = {self.chain_builder.option_symbol(sym, expiry, strike, ot): q for (strike, ot), q in parsed.items()}
                    if quotes:
                        self.chain_builder.apply_broker_quotes(quotes)
                        self.live_chain_stats["quotes"] = len(quotes)
                        self.live_chain_stats["last_ts"] = time.time()
                    self.live_chain_stats["polls"] += 1
                except Exception as exc:
                    self.live_chain_stats["errors"] += 1
                    self.live_chain_stats["last_error"] = str(exc)[:200]
                    log.warning("live chain poll failed for %s: %s", sym, exc)
                await asyncio.sleep(max(1.0, self.settings.live_chain_poll_seconds / max(1, len(wanted))))

    async def _risk_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.risk.evaluate()
                self.health.sample()
            except Exception:
                log.exception("risk loop error")
            await asyncio.sleep(1.0)

    async def _council_loop(self) -> None:
        await asyncio.sleep(3.0)
        while not self._stop.is_set():
            try:
                if not self.paused and self.chains:
                    await self.council.run_cycle()
            except Exception:
                log.exception("council loop error")
            await asyncio.sleep(self.settings.agent_cycle_seconds)

    async def _feed_supervisor(self) -> None:
        failures = 0
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            try:
                if self.feed.last_tick_ts and not self.feed.is_fresh(self.settings.feed_stale_seconds * 3):
                    failures += 1
                    self.log("WARNING", "feed", f"feed stale, reconnect attempt {failures}")
                    await self.alerts.emit("WARNING", "feed", "Market feed stale", "Attempting reconnect", dedupe_seconds=120)
                    await self.feed.reconnect()
                    if failures >= 5 and self.strategies.active_runs():
                        await self.alerts.emit("CRITICAL", "feed", "Feed unrecoverable", "Consider flattening positions manually", dedupe_seconds=300)
                else:
                    failures = 0
            except Exception as exc:
                self.log("ERROR", "feed", f"supervisor error: {exc}")

    async def _broadcast_loop(self) -> None:
        import json
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            if not self.ws_clients:
                continue
            try:
                payload = json.dumps({"type": "snapshot", "data": self.snapshot()}, default=str)
            except Exception:
                log.exception("snapshot failed")
                continue
            dead = []
            for ws in list(self.ws_clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.ws_clients.discard(ws)

    # ------------------------------------------------------------- snapshot
    def market_overview(self) -> List[dict]:
        out = []
        for u in self.universe.all():
            tk = self.processor.last_tick(u.symbol)
            ch = self.chains.get(u.symbol)
            out.append({"symbol": u.symbol, "name": u.name, "exchange": u.exchange.value, "lot_size": u.lot_size, "ltp": tk.ltp if tk else None, "change_pct": tk.change_pct if tk else None,
                        "open": tk.open if tk else None, "high": tk.high if tk else None, "low": tk.low if tk else None, "session": self.scheduler.session(u),
                        "expiry": ch.expiry if ch else None, "pcr": ch.pcr if ch else None, "iv_atm": ch.iv_atm if ch else None, "max_pain": ch.max_pain if ch else None,
                        "expected_move": ch.expected_move if ch else None, "atm": ch.atm_strike if ch else None, "dte": ch.days_to_expiry if ch else None,
                        "enabled": self.risk.market_enabled.get(u.exchange.value, True)})
        return out

    def snapshot(self) -> dict:
        vix = self.processor.last_tick(VIX_SYMBOL)
        return {
            "ts": time.time(), "product": PRODUCT, "version": __version__, "mode": self.mode.value, "env": self.env.value, "engine_running": self.engine_running, "paused": self.paused,
            "live_allowed": self.settings.live_allowed, "feed": self.feed.status(), "broker": self.broker.status(), "data_source": self.settings.data_source,
            "live_session": {**self.live.status(), "chain": self.live_chain_stats} if self.live else None, "vix": {"ltp": vix.ltp, "change_pct": vix.change_pct} if vix else None,
            "market": self.market_overview(), "risk": self.risk.describe(), "positions": self.positions.snapshot(), "orders": self.orders.recent(60),
            "pending_orders": [o.model_dump(mode="json") for o in self.orders.pending_approval()], "runs": [r.model_dump(mode="json") for r in self.strategies.runs.values() if r.status != "CLOSED"],
            "pending_plans": [p.model_dump(mode="json") for p in self.council.pending_plans()], "council": {"cycle": self.council.cycle, "busy": self.council.busy, "focus": self.council.focus, "auto_trades_today": self.council.auto_trades_today,
            "last": [d.model_dump(mode="json") | {"assessments": [{k: v for k, v in a.model_dump(mode="json").items() if k != "data"} for a in d.assessments]} for d in self.council.decisions[-4:][::-1]],
            "agents": [a.status() for a in self.council.agents], "briefs": self.council.last_brief, "llm": self.council.llm.status()},
            "alerts": [a.model_dump(mode="json") for a in self.alerts.recent[-15:][::-1]], "scheduler": self.scheduler.describe(), "pnl": {"daily": self.positions.daily_pnl(), "realized": round(self.positions.realized_today, 2), "unrealized": self.positions.unrealized(), "charges": round(self.positions.charges_today, 2)},
        }


_TERMINAL: Optional[Terminal] = None


def get_terminal() -> Terminal:
    global _TERMINAL
    if _TERMINAL is None:
        _TERMINAL = Terminal()
    return _TERMINAL


def set_terminal(t: Optional[Terminal]) -> None:
    global _TERMINAL
    _TERMINAL = t
