"""Market data processor: candles, indicators, PCR/OI history, realised vol."""
from __future__ import annotations

import collections
import math
import statistics
from typing import Deque, Dict, List

from terminal.core.models import Candle, OptionChain, Tick


class SymbolState:
    def __init__(self, candle_seconds: int = 60, keep: int = 600) -> None:
        self.candle_seconds = candle_seconds
        self.candles: Deque[Candle] = collections.deque(maxlen=keep)
        self.ticks: Deque[Tick] = collections.deque(maxlen=3600)
        self.last: Tick | None = None
        self._cur: Candle | None = None
        self._vwap_pv = 0.0
        self._vwap_v = 0.0

    def push(self, tick: Tick) -> None:
        self.last = tick
        self.ticks.append(tick)
        bucket = int(tick.ts // self.candle_seconds) * self.candle_seconds
        vol_delta = 1
        if self._cur is None or self._cur.ts != bucket:
            if self._cur is not None:
                self.candles.append(self._cur)
            self._cur = Candle(ts=bucket, open=tick.ltp, high=tick.ltp, low=tick.ltp, close=tick.ltp, volume=vol_delta)
        else:
            c = self._cur
            c.high = max(c.high, tick.ltp)
            c.low = min(c.low, tick.ltp)
            c.close = tick.ltp
            c.volume += vol_delta
        self._vwap_pv += tick.ltp * vol_delta
        self._vwap_v += vol_delta

    def all_candles(self) -> List[Candle]:
        out = list(self.candles)
        if self._cur:
            out.append(self._cur)
        return out

    @property
    def vwap(self) -> float:
        return self._vwap_pv / self._vwap_v if self._vwap_v else (self.last.ltp if self.last else 0.0)


def ema(values: List[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: List[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def atr(candles: List[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return sum(trs[-period:]) / period


def supertrend(candles: List[Candle], period: int = 10, mult: float = 3.0) -> str | None:
    if len(candles) < period + 2:
        return None
    a = atr(candles, period)
    if not a:
        return None
    c = candles[-1]
    hl2 = (c.high + c.low) / 2
    upper, lower = hl2 + mult * a, hl2 - mult * a
    closes = [x.close for x in candles[-period:]]
    m = sum(closes) / len(closes)
    if c.close > upper - mult * a * 0.5 and c.close > m:
        return "UP"
    if c.close < lower + mult * a * 0.5 and c.close < m:
        return "DOWN"
    return "UP" if c.close >= m else "DOWN"


def realized_vol(closes: List[float], bars_per_year: float) -> float | None:
    if len(closes) < 20:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 10:
        return None
    return statistics.pstdev(rets) * math.sqrt(bars_per_year)


class MarketDataProcessor:
    def __init__(self, candle_seconds: int = 60) -> None:
        self.symbols: Dict[str, SymbolState] = {}
        self.candle_seconds = candle_seconds
        self.pcr_history: Dict[str, Deque[dict]] = collections.defaultdict(lambda: collections.deque(maxlen=720))
        self.vix_history: Deque[float] = collections.deque(maxlen=20000)
        self.chain_cache: Dict[str, OptionChain] = {}
        self.last_chain_ts: Dict[str, float] = {}

    def push(self, tick: Tick) -> None:
        st = self.symbols.get(tick.symbol)
        if st is None:
            st = self.symbols[tick.symbol] = SymbolState(self.candle_seconds)
        st.push(tick)
        if tick.symbol == "INDIAVIX":
            self.vix_history.append(tick.ltp)

    def last_price(self, symbol: str) -> float | None:
        st = self.symbols.get(symbol)
        return st.last.ltp if st and st.last else None

    def last_tick(self, symbol: str) -> Tick | None:
        st = self.symbols.get(symbol)
        return st.last if st else None

    def candles(self, symbol: str, limit: int = 200) -> List[Candle]:
        st = self.symbols.get(symbol)
        return st.all_candles()[-limit:] if st else []

    def record_chain(self, chain: OptionChain) -> None:
        self.chain_cache[chain.underlying] = chain
        self.last_chain_ts[chain.underlying] = chain.ts
        hist = self.pcr_history[chain.underlying]
        if not hist or chain.ts - hist[-1]["ts"] >= 10:
            hist.append({"ts": chain.ts, "pcr": chain.pcr, "pcr_volume": chain.pcr_volume, "spot": chain.spot, "iv": chain.iv_atm, "max_pain": chain.max_pain, "total_oi": chain.total_ce_oi + chain.total_pe_oi})

    def vix_rank(self) -> dict:
        h = list(self.vix_history)
        if not h:
            return {"vix": None, "rank": None, "percentile": None, "high": None, "low": None}
        cur = h[-1]
        lo, hi = min(h), max(h)
        rank = (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0
        pct = sum(1 for x in h if x <= cur) / len(h) * 100
        return {"vix": cur, "rank": round(rank, 1), "percentile": round(pct, 1), "high": lo if False else hi, "low": lo}

    def indicators(self, symbol: str) -> dict:
        st = self.symbols.get(symbol)
        if not st or not st.last:
            return {}
        cds = st.all_candles()
        closes = [c.close for c in cds]
        tick_closes = [t.ltp for t in list(st.ticks)[-300:]]
        e9, e21 = ema(closes, 9), ema(closes, 21)
        r = rsi(closes, 14) or rsi(tick_closes, 14)
        a = atr(cds, 14)
        stt = supertrend(cds)
        rv = realized_vol(closes, 252 * 375) or realized_vol(tick_closes, 252 * 375 * 60)
        last = st.last
        ret5 = None
        if len(st.ticks) > 300:
            ref = list(st.ticks)[-300].ltp
            ret5 = (last.ltp / ref - 1) * 100 if ref else None
        day_range = (last.high - last.low) if last.high and last.low else None
        trend = "FLAT"
        if e9 and e21:
            gap = (e9 - e21) / e21 * 100
            trend = "UP" if gap > 0.03 else ("DOWN" if gap < -0.03 else "FLAT")
        return {
            "ltp": last.ltp, "change_pct": last.change_pct, "open": last.open, "high": last.high, "low": last.low, "prev_close": last.prev_close,
            "ema9": round(e9, 2) if e9 else None, "ema21": round(e21, 2) if e21 else None, "rsi": round(r, 1) if r else None,
            "atr": round(a, 2) if a else None, "supertrend": stt, "vwap": round(st.vwap, 2), "realized_vol": round(rv * 100, 2) if rv else None,
            "trend": trend, "ret_5m_pct": round(ret5, 3) if ret5 is not None else None, "day_range": round(day_range, 2) if day_range else None,
            "candles": len(cds),
        }

    def sigma_move(self, symbol: str, window_seconds: int, annual_vol: float) -> float | None:
        """Move over the window expressed in standard deviations of the expected move."""
        st = self.symbols.get(symbol)
        if not st or len(st.ticks) < 3:
            return None
        now = st.last.ts
        ref = None
        for t in st.ticks:
            if now - t.ts <= window_seconds:
                ref = t
                break
        if ref is None or ref.ltp <= 0:
            return None
        move = st.last.ltp / ref.ltp - 1
        expected = annual_vol * math.sqrt(window_seconds / (252 * 6.25 * 3600))
        return move / expected if expected > 0 else None
