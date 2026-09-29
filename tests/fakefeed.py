"""Test-only scripted feed. The product has no simulator; the test suite needs a
deterministic tick source that behaves like a broker feed: it emits underlying
ticks and streams option quotes for the strikes the terminal is watching, so
chains are fully 'live' and every strategy/risk path runs end to end."""
from __future__ import annotations

import asyncio
import math
import random
from typing import Dict, List

from terminal.core.models import Tick, Underlying
from terminal.market.feed import MarketFeed
from terminal.market.pricing import bs_price


class ScriptedFeed(MarketFeed):
    name = "scripted-test-feed"

    def __init__(self, universe: List[Underlying], interval: float = 0.2, seed: int = 7, terminal=None) -> None:
        super().__init__()
        self.universe = universe
        self.interval = interval
        self.rng = random.Random(seed)
        self.terminal = terminal
        self.prices: Dict[str, float] = {u.symbol: u.base_spot for u in universe}
        self.prev_close: Dict[str, float] = dict(self.prices)
        self.vix = 13.5
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._option_tokens: Dict[str, str] = {}
        self.oi_seed: Dict[str, int] = {}

    async def start(self) -> None:
        self._stop = asyncio.Event()
        self.connected = True
        self._task = asyncio.create_task(self._run(), name="scripted-feed")

    async def stop(self) -> None:
        self.connected = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def subscribe_options(self, entries: List[Dict]) -> int:
        new = [e for e in entries if e["token"] not in self._option_tokens]
        for e in new:
            self._option_tokens[e["token"]] = e["symbol"]
        return len(new)

    def shock(self, symbol: str, pct: float) -> None:
        if symbol.upper() == "INDIAVIX":
            self.vix *= 1 + pct / 100.0
        elif symbol in self.prices:
            self.prices[symbol] *= 1 + pct / 100.0

    def _oi(self, sym: str, distance: float, is_put: bool) -> int:
        if sym not in self.oi_seed:
            peak = -4.0 if is_put else 4.0
            self.oi_seed[sym] = int(2_000_000 * (math.exp(-0.16 * abs(distance - peak)) + 0.3) * self.rng.uniform(0.8, 1.2))
        return self.oi_seed[sym]

    async def _run(self) -> None:
        while not self._stop.is_set():
            self.vix = max(8.0, self.vix + self.rng.gauss(0, 0.02))
            await self._emit(Tick(symbol="INDIAVIX", ltp=round(self.vix, 2), prev_close=13.5, change_pct=round((self.vix / 13.5 - 1) * 100, 2)))
            for u in self.universe:
                p = self.prices[u.symbol]
                p *= math.exp(self.rng.gauss(0, u.base_vol * math.sqrt(self.interval / (252 * 6.25 * 3600))))
                self.prices[u.symbol] = p
                await self._emit(Tick(symbol=u.symbol, ltp=round(p, 2), prev_close=self.prev_close[u.symbol], change_pct=round((p / self.prev_close[u.symbol] - 1) * 100, 2),
                                      open=self.prev_close[u.symbol], high=max(p, self.prev_close[u.symbol]), low=min(p, self.prev_close[u.symbol])))
            # stream option quotes for every strike of the current chains, like a broker feed would
            t = self.terminal
            if t is not None:
                for sym, ch in list(t.chains.items()):
                    u = t.universe.get(sym)
                    spot = self.prices.get(sym, ch.spot)
                    tt = max((ch.days_to_expiry) / 365.0, 1e-5)
                    base_iv = (self.vix / 100.0) * (u.base_vol / 0.13) if u.exchange.value != "MCX" else u.base_vol
                    for r in ch.rows:
                        i = (r.strike - ch.atm_strike) / u.strike_step
                        for q in (r.ce, r.pe):
                            is_call = q.option_type.value == "CE"
                            k = math.log(r.strike / spot)
                            iv = max(0.05, base_iv * (1 - 0.9 * k + 3.2 * k * k / max(math.sqrt(tt * 52), 0.35)))
                            px = max(0.05, round(bs_price(spot, r.strike, tt, iv, is_call) / 0.05) * 0.05)
                            await self._emit_option(q.symbol, {"ltp": round(px, 2), "oi": self._oi(q.symbol, i, not is_call), "volume": 1000, "bid": round(max(0.05, px - 0.05), 2), "ask": round(px + 0.05, 2)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass


def live_chain(u: Underlying, spot: float, vix: float, expiry: str, builder=None, seed: int = 7):
    """Build a chain and overlay broker-style quotes (model prices + synthetic OI) on every row,
    so unit tests see the same fully-live chain the terminal gets from a streaming broker feed."""
    from terminal.market.chain import OptionChainBuilder

    rng = random.Random(seed)
    b = builder or OptionChainBuilder()
    ch = b.build(u, spot, vix, expiry)
    quotes = {}
    for r in ch.rows:
        i = (r.strike - ch.atm_strike) / u.strike_step
        for q, peak in ((r.ce, 4.0), (r.pe, -4.0)):
            oi = int(2_000_000 * (math.exp(-0.16 * abs(i - peak)) + 0.3) * rng.uniform(0.8, 1.2))
            quotes[q.symbol] = {"ltp": q.ltp, "oi": oi, "oi_change": int(oi * rng.uniform(-0.05, 0.05)), "volume": int(oi * 0.1), "bid": round(max(0.05, q.ltp - 0.05), 2), "ask": round(q.ltp + 0.05, 2)}
    b.apply_broker_quotes(quotes)
    return b.build(u, spot, vix, expiry)
