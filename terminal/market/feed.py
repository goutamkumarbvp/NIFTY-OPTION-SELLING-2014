"""Market data feeds.

* ``SimulatedFeed`` – a realistic multi-market simulator (regime switching GBM
  with intraday seasonality, jumps and a VIX process). It lets the whole terminal
  (agents, risk, execution) run end-to-end without any broker credentials.
* ``KotakNeoFeed`` – adapter skeleton for the Kotak Neo API; activated only when
  credentials are configured and the SDK is importable.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Dict, List, Optional

from terminal.core.clock import now_ist
from terminal.core.models import Tick, Underlying
from terminal.market.universe import VIX_SYMBOL

log = logging.getLogger("terminal.feed")

TickHandler = Callable[[Tick], Awaitable[None]]


class MarketFeed(ABC):
    name = "abstract"

    def __init__(self) -> None:
        self._handlers: List[TickHandler] = []
        self.connected = False
        self.last_tick_ts: float = 0.0
        self.tick_count = 0
        self.errors = 0

    def on_tick(self, handler: TickHandler) -> None:
        self._handlers.append(handler)

    async def _emit(self, tick: Tick) -> None:
        self.last_tick_ts = tick.ts
        self.tick_count += 1
        for h in self._handlers:
            try:
                await h(tick)
            except Exception:  # pragma: no cover - defensive
                log.exception("tick handler failed")

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    def is_fresh(self, stale_seconds: float) -> bool:
        return self.connected and (time.time() - self.last_tick_ts) <= stale_seconds

    def status(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "last_tick_age": round(time.time() - self.last_tick_ts, 2) if self.last_tick_ts else None,
            "ticks": self.tick_count,
            "errors": self.errors,
        }


class _SimSeries:
    """One simulated underlying: GBM + mean-reverting vol + jumps."""

    def __init__(self, u: Underlying, seed: int) -> None:
        self.u = u
        self.rng = random.Random(seed)
        self.price = u.base_spot * (1 + self.rng.uniform(-0.004, 0.004))
        self.prev_close = u.base_spot
        self.open = self.price
        self.high = self.price
        self.low = self.price
        self.vol = u.base_vol
        self.drift_regime = 0.0
        self.volume = 0
        self.regime_ttl = 0

    def step(self, dt_seconds: float, vix_level: float) -> float:
        # regime switching drift (trend / range) every few minutes
        if self.regime_ttl <= 0:
            self.regime_ttl = self.rng.randint(120, 900)
            self.drift_regime = self.rng.choice([-1.0, -0.4, 0.0, 0.0, 0.4, 1.0]) * self.u.base_vol * 2.5
        self.regime_ttl -= dt_seconds
        # vol mean reverts toward VIX implied level for indices, own base for commodities
        target = self.u.base_vol * (vix_level / 13.0) if self.u.exchange.value != "MCX" else self.u.base_vol
        self.vol += (target - self.vol) * 0.02 + self.rng.gauss(0, 0.002)
        self.vol = max(0.06, min(self.vol, 0.9))
        dt = dt_seconds / (252 * 6.25 * 3600)
        z = self.rng.gauss(0, 1)
        jump = 0.0
        if self.rng.random() < dt_seconds * 0.00025:  # rare jump
            jump = self.rng.choice([-1, 1]) * self.rng.uniform(0.002, 0.008)
        ret = (self.drift_regime - 0.5 * self.vol ** 2) * dt + self.vol * math.sqrt(dt) * z + jump
        self.price *= math.exp(ret)
        self.high = max(self.high, self.price)
        self.low = min(self.low, self.price)
        self.volume += int(abs(z) * 900 + 100)
        return self.price

    def new_day(self) -> None:
        self.prev_close = self.price
        self.open = self.price
        self.high = self.price
        self.low = self.price
        self.volume = 0


class SimulatedFeed(MarketFeed):
    name = "simulated"

    def __init__(self, universe: List[Underlying], interval: float = 1.0, speed: float = 1.0, always_open: bool = True, seed: Optional[int] = None) -> None:
        super().__init__()
        self.interval = max(0.1, interval)
        self.speed = max(0.1, speed)
        self.always_open = always_open
        base_seed = seed if seed is not None else int(time.time())
        self.series: Dict[str, _SimSeries] = {u.symbol: _SimSeries(u, base_seed + i * 7919) for i, u in enumerate(universe)}
        self.vix = 13.0 + random.Random(base_seed).uniform(-1.5, 2.5)
        self.vix_prev_close = self.vix
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._day = now_ist().date()
        self.rng = random.Random(base_seed ^ 0xABCDEF)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = asyncio.Event()
        self.connected = True
        self._task = asyncio.create_task(self._run(), name="sim-feed")
        log.info("simulated feed started (%d instruments)", len(self.series))

    async def stop(self) -> None:
        self.connected = False
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None

    def shock(self, symbol: str, pct: float) -> None:
        """Test hook: instantly move an underlying by pct (e.g. -3.0)."""
        s = self.series.get(symbol.upper())
        if s:
            s.price *= 1 + pct / 100.0
            s.high, s.low = max(s.high, s.price), min(s.low, s.price)
        if symbol.upper() == VIX_SYMBOL:
            self.vix *= 1 + pct / 100.0

    async def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            today = now_ist().date()
            if today != self._day:
                self._day = today
                for s in self.series.values():
                    s.new_day()
                self.vix_prev_close = self.vix
            dt_s = self.interval * self.speed
            # VIX: mean reverting around 13.5 with occasional spikes
            self.vix += (13.5 - self.vix) * 0.0015 * dt_s + self.rng.gauss(0, 0.03) * math.sqrt(dt_s)
            if self.rng.random() < dt_s * 0.00008:
                self.vix *= 1 + self.rng.uniform(0.05, 0.18)
            self.vix = max(8.0, min(self.vix, 60.0))
            await self._emit(Tick(symbol=VIX_SYMBOL, ltp=round(self.vix, 2), change_pct=round((self.vix / self.vix_prev_close - 1) * 100, 2), prev_close=self.vix_prev_close))
            for s in self.series.values():
                price = s.step(dt_s, self.vix)
                await self._emit(Tick(symbol=s.u.symbol, ltp=round(price, 2), change_pct=round((price / s.prev_close - 1) * 100, 2), open=round(s.open, 2), high=round(s.high, 2), low=round(s.low, 2), prev_close=round(s.prev_close, 2), volume=s.volume))
            elapsed = time.time() - t0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.02, self.interval - elapsed))
            except asyncio.TimeoutError:
                pass


class KotakNeoFeed(MarketFeed):
    """Kotak Neo live feed adapter.

    The adapter authenticates with the Neo API (consumer key + mobile + UCC +
    MPIN + TOTP) and subscribes to index/underlying quotes. It is deliberately
    read-only. Activate with ``DATA_SOURCE=kotak`` and the NEO_* variables.
    """

    name = "kotak_neo"

    def __init__(self, universe: List[Underlying], settings) -> None:
        super().__init__()
        self.universe = universe
        self.settings = settings
        self._client = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        s = self.settings
        missing = [k for k, v in {"NEO_CONSUMER_KEY": s.neo_consumer_key, "NEO_MOBILE_NUMBER": s.neo_mobile_number, "NEO_UCC": s.neo_ucc, "NEO_MPIN": s.neo_mpin, "NEO_TOTP_SECRET": s.neo_totp_secret}.items() if not v]
        if missing:
            raise RuntimeError(f"KOTAK_CREDENTIALS_MISSING: {', '.join(missing)}")
        try:
            from neo_api_client import NeoAPI  # type: ignore
            import pyotp  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("KOTAK_SDK_NOT_INSTALLED: pip install kotakneoapi pyotp") from exc

        loop = asyncio.get_running_loop()

        def _connect():
            client = NeoAPI(consumer_key=s.neo_consumer_key, environment="prod", access_token=None, neo_fin_key=None)
            client.login(mobilenumber=s.neo_mobile_number, ucc=s.neo_ucc, totp=pyotp.TOTP(s.neo_totp_secret).now())
            client.session_2fa(OTP=s.neo_mpin)
            return client

        self._client = await loop.run_in_executor(None, _connect)
        self.connected = True

        def _on_message(message):
            try:
                rows = message if isinstance(message, list) else [message]
                for row in rows:
                    sym = str(row.get("ts") or row.get("tk") or "").upper()
                    ltp = float(row.get("ltp") or row.get("lp") or 0)
                    if not sym or ltp <= 0:
                        continue
                    for u in self.universe:
                        if u.symbol in sym:
                            asyncio.run_coroutine_threadsafe(self._emit(Tick(symbol=u.symbol, ltp=ltp)), loop)
            except Exception:
                self.errors += 1

        self._client.on_message = _on_message
        tokens = [{"instrument_token": u.symbol, "exchange_segment": "nse_cm" if u.exchange.value == "NSE" else ("bse_cm" if u.exchange.value == "BSE" else "mcx_fo")} for u in self.universe]
        await loop.run_in_executor(None, lambda: self._client.subscribe(instrument_tokens=tokens, isIndex=True))
        log.info("Kotak Neo feed connected")

    async def stop(self) -> None:
        self.connected = False
        if self._client is not None:
            try:
                self._client.un_subscribe(instrument_tokens=[])
            except Exception:
                pass
        self._client = None
