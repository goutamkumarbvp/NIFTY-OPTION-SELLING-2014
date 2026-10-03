"""Deterministic SIMULATED market for offline demos and tests (PAPER_ONLY deployments only).

Everything it produces is labelled SIMULATED and is never presented as live.
Paper orders against it are accepted only when AMRT_SIMULATED_MARKET=true, and a
LIVE_CAPABLE deployment refuses to start with it enabled.

Model: geometric Brownian motion spot (seeded), Black-Scholes premiums with a
simple volatility smile, a fixed bid/ask spread, and an open-interest profile
peaking at round strikes away from spot that drifts slowly.
"""
from __future__ import annotations

import datetime as dt
import math
import random

from amrt.analytics.pricing import bs_price
from amrt.core.clock import IST
from amrt.core.enums import DataLabel, OptionType
from amrt.marketdata.instruments import spec
from amrt.marketdata.models import ChainRow, ChainSnapshot, Instrument, OptionLeg, Quote

START = {"NIFTY": 25000.0, "BANKNIFTY": 56000.0, "SENSEX": 82000.0, "CRUDEOIL": 6000.0, "NATURALGAS": 300.0}
EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 3}   # Tue / Thu; MCX uses mid-month


def next_expiry(symbol: str, today: dt.date) -> dt.date:
    if symbol in EXPIRY_WEEKDAY:
        d = (EXPIRY_WEEKDAY[symbol] - today.weekday()) % 7
        return today + dt.timedelta(days=d)
    exp = today.replace(day=16)
    return exp if exp >= today else (exp.replace(day=1) + dt.timedelta(days=32)).replace(day=16)


class SimulatedMarket:
    name = "simulator"
    live = False

    def __init__(self, clock, instruments, seed: int = 7, annual_vol: float = 0.13, strikes_each_side: int = 15) -> None:
        self.clock, self.instruments = clock, instruments
        self.rng = random.Random(seed)
        self.vol = annual_vol
        self.n = strikes_each_side
        self.spot: dict[str, float] = {}
        self.last_ts: dict[str, float] = {}
        self.oi_base: dict[tuple[str, float, str], float] = {}
        self.prev_close_oi: dict[tuple[str, float, str], float] = {}
        self.seqs: dict[str, int] = {}

    def _step(self, und: str) -> float:
        now = self.clock.ts()
        s = self.spot.get(und, START.get(und, 1000.0))
        dt_years = max(0.0, now - self.last_ts.get(und, now)) / (252 * 6.25 * 3600)
        if dt_years > 0:
            s *= math.exp(-0.5 * self.vol ** 2 * dt_years + self.vol * math.sqrt(dt_years) * self.rng.gauss(0, 1))
        self.spot[und], self.last_ts[und] = s, now
        return s

    def chain(self, und: str) -> tuple[ChainSnapshot, list[Quote]]:
        sp = spec(und)
        s = self._step(und)
        now_ist = self.clock.ist()
        expiry = next_expiry(und, now_ist.date())
        exp_dt = dt.datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=IST)
        t = max((exp_dt - now_ist).total_seconds(), 600.0) / (365 * 86400)
        step = sp.strike_step
        atm = round(s / step) * step
        rows, quotes = [], []
        now = self.clock.ts()
        for k in range(-self.n, self.n + 1):
            strike = atm + k * step
            row = {}
            for ot in (OptionType.CE, OptionType.PE):
                m = math.log(strike / s)
                iv = self.vol * (1 + 2.5 * m * m) + (0.01 if ot == OptionType.PE and strike < s else 0.0)
                px = max(0.05, bs_price(s, strike, t, iv, ot == OptionType.CE))
                spread = max(0.05, round(px * 0.004, 2))
                inst = self.instruments.option(und, expiry, strike, ot)
                self.instruments.add(inst, source="simulator")
                key3 = (und, strike, ot.value)
                if key3 not in self.oi_base:
                    dist = abs(strike - s) / step
                    roundness = 2.0 if strike % (step * 10) == 0 else (1.4 if strike % (step * 2) == 0 else 1.0)
                    side_bias = 1.3 if (ot == OptionType.CE and strike > s) or (ot == OptionType.PE and strike < s) else 0.7
                    self.oi_base[key3] = 400_000 * roundness * side_bias * math.exp(-dist / 6) + 20_000
                    self.prev_close_oi[key3] = self.oi_base[key3] * (0.85 + 0.2 * self.rng.random())
                prev = self.oi_base[key3]
                oi = max(0.0, prev * (1 + self.rng.gauss(0, 0.004)))
                self.oi_base[key3] = oi
                bid, ask = round(max(0.05, px - spread / 2), 2), round(px + spread / 2, 2)
                leg = OptionLeg(instrument_key=inst.key, ltp=round(px, 2), bid=bid, ask=ask, volume=int(oi * 3), oi=int(oi),
                                oi_change=int(oi - self.prev_close_oi[key3]), iv=round(iv * 100, 2))
                row[ot.value.lower()] = leg
                self.seqs[inst.key] = self.seqs.get(inst.key, 0) + 1
                quotes.append(Quote(instrument_key=inst.key, ltp=round(px, 2), bid=bid, ask=ask, oi=int(oi), recv_ts=now, seq=self.seqs[inst.key],
                                    source=self.name, label=DataLabel.SIMULATED))
            rows.append(ChainRow(strike=strike, ce=row["ce"], pe=row["pe"]))
        self.instruments.underlying(und)
        quotes.append(Quote(instrument_key=Instrument.underlying_key(sp.exchange, und), ltp=round(s, 2), recv_ts=now, source=self.name, label=DataLabel.SIMULATED))
        snap = ChainSnapshot(underlying=und, exchange=sp.exchange, expiry=expiry, spot=round(s, 2), spot_ts=now, ts=now, source=self.name,
                             label=DataLabel.SIMULATED, rows=rows, strike_step=step)
        return snap, quotes
