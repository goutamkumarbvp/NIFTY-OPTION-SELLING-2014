"""Market data feed: poll a chain source per underlying, validate through the hub, compute analytics.

Sources implement `async chain(underlying) -> (ChainSnapshot, list[Quote])`:
* BrokerChainSource — a broker MarketDataReader (read-only facade, no order methods).
* ReplayChainSource — recorded snapshots (JSONL), labelled HISTORICAL REPLAY.
* SimulatedMarket   — opt-in SIMULATED data for PAPER_ONLY demos.
Each poll's quotes (underlying + every leg) go through MarketDataHub.ingest, so
the Risk Kernel's freshness checks see exactly what the dashboard sees.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections import defaultdict, deque
from pathlib import Path

from amrt.analytics.option_chain import analyze_chain, observe_windows
from amrt.core.enums import DataLabel, HealthState
from amrt.marketdata.instruments import spec
from amrt.marketdata.models import ChainRow, ChainSnapshot, Instrument, OptionLeg, Quote

log = logging.getLogger("amrt.feed")


def leg_quotes(snap: ChainSnapshot, source: str, label: DataLabel) -> list[Quote]:
    out = []
    for r in snap.rows:
        for leg in (r.ce, r.pe):
            if leg is not None and leg.ltp and leg.ltp > 0:
                out.append(Quote(instrument_key=leg.instrument_key, ltp=leg.ltp, bid=leg.bid, ask=leg.ask, volume=leg.volume, oi=leg.oi,
                                 exchange_ts=snap.exchange_ts, recv_ts=snap.ts, source=source, label=label))
    return out


class BrokerChainSource:
    live = True

    def __init__(self, reader, instruments, clock, expiry_ttl_s: float = 600.0) -> None:
        self.reader, self.instruments, self.clock = reader, instruments, clock
        self.name = reader.name
        self._exp: dict[str, tuple[float, str]] = {}
        self.expiry_ttl_s = expiry_ttl_s

    async def _expiry(self, und: str) -> str:
        cached = self._exp.get(und)
        if cached and self.clock.ts() - cached[0] < self.expiry_ttl_s:
            return cached[1]
        today = self.clock.ist().date().isoformat()
        exps = sorted(e for e in await self.reader.fetch_expiries(und) if e >= today)
        if not exps:
            raise LookupError(f"no current expiry for {und}")
        self._exp[und] = (self.clock.ts(), exps[0])
        return exps[0]

    async def chain(self, und: str) -> tuple[ChainSnapshot, list[Quote]]:
        snap, spot_q = await self.reader.fetch_chain(und, await self._expiry(und), self.instruments)
        quotes = leg_quotes(snap, self.name, DataLabel.LIVE_UNVERIFIED)
        if spot_q is not None:
            quotes.append(spot_q)
        return snap, quotes


class ReplayChainSource:
    """Plays recorded chain snapshots (one JSON object per line) in order, re-stamped with receipt time.

    The original exchange time is kept in `exchange_ts`; everything is labelled HISTORICAL REPLAY.
    """
    live = False

    def __init__(self, path: str | Path, instruments, clock, loop: bool = True) -> None:
        self.name = "replay"
        self.instruments, self.clock, self.loop = instruments, clock, loop
        self.records: dict[str, list[dict]] = defaultdict(list)
        for line in Path(path).read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                self.records[rec["underlying"].upper()].append(rec)
        self.pos: dict[str, int] = defaultdict(int)

    async def chain(self, und: str) -> tuple[ChainSnapshot, list[Quote]]:
        recs = self.records.get(und)
        if not recs:
            raise LookupError(f"no replay records for {und}")
        i = self.pos[und]
        if i >= len(recs):
            if not self.loop:
                raise LookupError("replay finished")
            i = 0
        self.pos[und] = i + 1
        rec = recs[i]
        sp = spec(und)
        expiry = dt.date.fromisoformat(rec["expiry"])
        now = self.clock.ts()
        rows = []
        for r in rec["rows"]:
            legs = {}
            for side in ("ce", "pe"):
                leg = r.get(side)
                if leg:
                    inst = self.instruments.option(und, expiry, r["strike"], side.upper())
                    self.instruments.add(inst, source="replay")
                    legs[side] = OptionLeg(**{**leg, "instrument_key": inst.key})
            rows.append(ChainRow(strike=r["strike"], **legs))
        snap = ChainSnapshot(underlying=und, exchange=sp.exchange, expiry=expiry, spot=rec.get("spot"), spot_ts=now, ts=now,
                             exchange_ts=rec.get("ts"), source="replay", label=DataLabel.HISTORICAL_REPLAY, rows=rows, strike_step=rec.get("strike_step"))
        quotes = leg_quotes(snap, "replay", DataLabel.HISTORICAL_REPLAY)
        if snap.spot:
            self.instruments.underlying(und)
            quotes.append(Quote(instrument_key=Instrument.underlying_key(sp.exchange, und), ltp=snap.spot, recv_ts=now, source="replay",
                                label=DataLabel.HISTORICAL_REPLAY))
        return snap, quotes


class MarketFeed:
    def __init__(self, hub, source, underlyings: list[str], health, clock, strikes_each_side: int = 10, max_age_ms: float = 3000,
                 persist_every_s: float = 60.0) -> None:
        self.hub, self.source, self.underlyings, self.health, self.clock = hub, source, underlyings, health, clock
        self.n, self.max_age_ms, self.persist_every_s = strikes_each_side, max_age_ms, persist_every_s
        self.component = f"data:{source.name}"
        health.register(self.component, critical=False, period_s=10.0, kind="datasource", restartable=True)
        self.state = hub.register_source(source.name, live=getattr(source, "live", False))
        self.analytics: dict[str, object] = {}
        self.windows: dict[str, dict] = {}
        self.spot_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))
        self.errors: dict[str, str] = {}
        self._last_persist: dict[str, float] = {}
        self.polls = 0

    async def poll_once(self) -> dict[str, str]:
        res = {}
        for und in self.underlyings:
            try:
                snap, quotes = await self.source.chain(und)
            except Exception as e:  # noqa: BLE001 - a failing underlying is reported, others continue
                self.errors[und] = f"{type(e).__name__}: {e}"[:300]
                res[und] = "ERROR"
                continue
            for q in quotes:
                self.hub.ingest(q)
            if not self.hub.ingest_chain(snap):
                res[und] = "REJECTED"
                continue
            label = self.hub.chain_label(snap, self.max_age_ms)
            a = analyze_chain(snap, self.n, label=label)
            self.analytics[und] = a
            self.windows[und] = observe_windows(self.hub.history(und, snap.expiry.isoformat()), strikes_each_side=self.n)
            if snap.spot:
                self.spot_history[und].append((snap.ts, snap.spot))
            if self.clock.ts() - self._last_persist.get(und, 0.0) >= self.persist_every_s:
                try:
                    self.hub.persist_chain(snap, a.model_dump(mode="json"))
                    self._last_persist[und] = self.clock.ts()
                except Exception:  # noqa: BLE001
                    log.exception("chain persist failed")
            self.errors.pop(und, None)
            res[und] = "OK"
        self.polls += 1
        ok = sum(1 for v in res.values() if v == "OK")
        self.state.connected = ok > 0
        if ok == len(self.underlyings):
            self.health.beat(self.component, HealthState.HEALTHY, reason="all underlyings OK")
        elif ok:
            self.health.beat(self.component, HealthState.DEGRADED, reason="; ".join(f"{k}: {v}" for k, v in self.errors.items()))
        else:
            self.health.fail(self.component, "; ".join(f"{k}: {v}" for k, v in self.errors.items()) or "no data")
        return res

    async def reconnect(self) -> bool:
        login = getattr(getattr(self.source, "reader", None), "login", None)
        if login is not None:
            await login()
            self.state.authenticated = True
        res = await self.poll_once()
        return any(v == "OK" for v in res.values())

    async def run(self, interval_s: float, beats: dict) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("feed poll failed")
            beats[self.component] = asyncio.get_running_loop().time()
            await asyncio.sleep(interval_s)


class SimulatedSource:
    """Adapter giving SimulatedMarket the async source interface."""
    live = False

    def __init__(self, sim) -> None:
        self.sim = sim
        self.name = sim.name

    async def chain(self, und: str):
        return self.sim.chain(und)
