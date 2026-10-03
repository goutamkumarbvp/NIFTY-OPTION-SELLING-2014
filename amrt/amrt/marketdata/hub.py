"""Market data hub: validate, de-duplicate, detect gaps, label, persist and serve quotes and chains.

Rules:
* A quote is rejected if invalid (non-positive price, crossed book, timestamp
  from the future beyond the drift tolerance, out-of-order sequence).
* Sequence gaps are counted and surfaced to the Safety Monitor.
* `quote()` raises DataUnavailable rather than returning anything stale.
* `LIVE DATA VERIFIED` requires: a live broker source whose session is
  authenticated, the quote passed validation, it is fresh, and broker clock
  drift is inside tolerance. Anything else is labelled what it is.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from collections.abc import Callable

from sqlalchemy import insert

from amrt.core.enums import DataLabel
from amrt.core.errors import DataUnavailable
from amrt.marketdata.models import ChainSnapshot, Freshness, Quote
from amrt.storage.db import Database, chain_snapshots, market_ticks

log = logging.getLogger("amrt.marketdata")


class SourceState:
    def __init__(self, name: str, live: bool) -> None:
        self.name = name
        self.live = live
        self.authenticated = False
        self.connected = False
        self.last_recv_ts = 0.0
        self.received = 0
        self.rejected = 0
        self.gaps = 0
        self.drift_ms_samples: deque[float] = deque(maxlen=200)
        self.last_error = ""

    @property
    def drift_ms(self) -> float | None:
        if not self.drift_ms_samples:
            return None
        s = sorted(self.drift_ms_samples)
        return s[len(s) // 2]

    def to_dict(self) -> dict:
        return {"name": self.name, "live": self.live, "authenticated": self.authenticated, "connected": self.connected, "last_recv_ts": self.last_recv_ts,
                "received": self.received, "rejected": self.rejected, "gaps": self.gaps, "drift_ms": self.drift_ms, "last_error": self.last_error}


class MarketDataHub:
    def __init__(self, clock, db: Database | None = None, max_drift_ms: float = 2000.0, persist_ticks: bool = True) -> None:
        self.clock = clock
        self.db = db
        self.max_drift_ms = max_drift_ms
        self.persist_ticks = persist_ticks and db is not None
        self.sources: dict[str, SourceState] = {}
        self.quotes: dict[str, Quote] = {}
        self._last_seq: dict[tuple[str, str], int] = {}
        self.chains: dict[tuple[str, str], ChainSnapshot] = {}
        self.chain_history: dict[tuple[str, str], deque[ChainSnapshot]] = defaultdict(lambda: deque(maxlen=720))
        self._tick_buf: list[dict] = []
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Quote], None]] = []
        self._chain_listeners: list[Callable[[ChainSnapshot], None]] = []
        self.rejections: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------ sources
    def register_source(self, name: str, live: bool) -> SourceState:
        st = self.sources.get(name) or SourceState(name, live)
        self.sources[name] = st
        return st

    def on_quote(self, fn: Callable[[Quote], None]) -> None:
        self._listeners.append(fn)

    def on_chain(self, fn: Callable[[ChainSnapshot], None]) -> None:
        self._chain_listeners.append(fn)

    # ------------------------------------------------------------- ingest
    def _reject(self, src: SourceState | None, reason: str) -> bool:
        self.rejections[reason] += 1
        if src is not None:
            src.rejected += 1
        return False

    def ingest(self, q: Quote) -> bool:
        src = self.sources.get(q.source.split(":")[0]) or self.sources.get(q.source)
        now = self.clock.ts()
        if q.bid and q.ask and q.ask < q.bid:
            return self._reject(src, "CROSSED_BOOK")
        if q.exchange_ts is not None:
            drift_ms = (q.exchange_ts - q.recv_ts) * 1000.0
            if drift_ms > self.max_drift_ms:
                return self._reject(src, "TIMESTAMP_IN_FUTURE")
            if src is not None and q.label != DataLabel.HISTORICAL_REPLAY:
                src.drift_ms_samples.append(q.recv_ts * 1000.0 - q.exchange_ts * 1000.0)
        with self._lock:
            if q.seq is not None:
                k = (q.source, q.instrument_key)
                last = self._last_seq.get(k)
                if last is not None:
                    if q.seq == last:
                        return self._reject(src, "DUPLICATE")
                    if q.seq < last:
                        return self._reject(src, "OUT_OF_ORDER")
                    if q.seq > last + 1 and src is not None:
                        src.gaps += 1
                self._last_seq[k] = q.seq
            prev = self.quotes.get(q.instrument_key)
            if prev is not None and q.exchange_ts and prev.exchange_ts and q.exchange_ts < prev.exchange_ts and prev.source == q.source:
                return self._reject(src, "OLDER_THAN_CURRENT")
            self.quotes[q.instrument_key] = q
            if src is not None:
                src.received += 1
                src.last_recv_ts = now
            if self.persist_ticks:
                self._tick_buf.append({"ts": q.exchange_ts or q.recv_ts, "recv_ts": q.recv_ts, "instrument_key": q.instrument_key, "ltp": q.ltp, "bid": q.bid,
                                       "ask": q.ask, "volume": q.volume, "oi": q.oi, "seq": q.seq, "source": q.source, "label": q.label.value})
        for fn in list(self._listeners):
            try:
                fn(q)
            except Exception:
                log.exception("quote listener failed")
        return True

    def ingest_chain(self, snap: ChainSnapshot, persist: bool = True) -> bool:
        if not snap.rows:
            self.rejections["EMPTY_CHAIN"] += 1
            return False
        key = (snap.underlying, snap.expiry.isoformat())
        with self._lock:
            prev = self.chains.get(key)
            if prev is not None and snap.ts < prev.ts and prev.source == snap.source:
                self.rejections["CHAIN_OUT_OF_ORDER"] += 1
                return False
            self.chains[key] = snap
            self.chain_history[key].append(snap)
        src = self.sources.get(snap.source.split(":")[0])
        if src is not None:
            src.last_recv_ts = self.clock.ts()
        for fn in list(self._chain_listeners):
            try:
                fn(snap)
            except Exception:
                log.exception("chain listener failed")
        return True

    def persist_chain(self, snap: ChainSnapshot, analytics: dict) -> None:
        if self.db is None:
            return
        with self.db.tx() as conn:
            conn.execute(insert(chain_snapshots).values(underlying=snap.underlying, expiry=snap.expiry.isoformat(), ts=snap.ts, source=snap.source,
                                                        label=snap.label.value, analytics=analytics, rows=[r.model_dump(mode="json") for r in snap.rows]))

    def flush(self) -> int:
        if not self.persist_ticks:
            return 0
        with self._lock:
            buf, self._tick_buf = self._tick_buf, []
        if buf:
            with self.db.tx() as conn:
                conn.execute(insert(market_ticks), buf)
        return len(buf)

    # -------------------------------------------------------------- serve
    def freshness(self, instrument_key: str, max_age_ms: float) -> Freshness:
        q = self.quotes.get(instrument_key)
        if q is None:
            return Freshness(instrument_key=instrument_key, age_ms=None, max_age_ms=max_age_ms, fresh=False, label=DataLabel.UNAVAILABLE, reason="NO_DATA")
        age = (self.clock.ts() - q.recv_ts) * 1000.0
        label = self.effective_label(q, max_age_ms)
        fresh = age <= max_age_ms
        return Freshness(instrument_key=instrument_key, age_ms=round(age, 1), max_age_ms=max_age_ms, fresh=fresh,
                         label=label if fresh else DataLabel.UNAVAILABLE, source=q.source, reason="" if fresh else "STALE")

    def effective_label(self, q: Quote, max_age_ms: float) -> DataLabel:
        if q.label in (DataLabel.HISTORICAL_REPLAY, DataLabel.SIMULATED):
            return q.label
        age = (self.clock.ts() - q.recv_ts) * 1000.0
        if age > max_age_ms:
            return DataLabel.UNAVAILABLE
        src = self.sources.get(q.source.split(":")[0])
        if src is None or not src.live:
            return DataLabel.LIVE_UNVERIFIED
        drift = src.drift_ms
        if src.authenticated and src.connected and (drift is None or abs(drift) <= self.max_drift_ms):
            return DataLabel.LIVE_VERIFIED
        return DataLabel.LIVE_UNVERIFIED

    def quote(self, instrument_key: str, max_age_ms: float) -> Quote:
        q = self.quotes.get(instrument_key)
        if q is None:
            raise DataUnavailable("DATA UNAVAILABLE", instrument=instrument_key, reason="NO_DATA")
        age = (self.clock.ts() - q.recv_ts) * 1000.0
        if age > max_age_ms:
            raise DataUnavailable("DATA UNAVAILABLE", instrument=instrument_key, reason="STALE", age_ms=round(age, 1))
        return q

    def chain(self, underlying: str, expiry: str | None = None) -> ChainSnapshot | None:
        if expiry:
            return self.chains.get((underlying, expiry))
        cands = [s for (u, _), s in self.chains.items() if u == underlying]
        return min(cands, key=lambda s: s.expiry) if cands else None

    def chain_label(self, snap: ChainSnapshot | None, max_age_ms: float) -> DataLabel:
        """Effective label of a chain snapshot, by the same rules as quotes."""
        if snap is None:
            return DataLabel.UNAVAILABLE
        if snap.label in (DataLabel.HISTORICAL_REPLAY, DataLabel.SIMULATED):
            return snap.label
        if (self.clock.ts() - snap.ts) * 1000.0 > max_age_ms:
            return DataLabel.UNAVAILABLE
        src = self.sources.get(snap.source.split(":")[0])
        if src is not None and src.live and src.authenticated and src.connected and (src.drift_ms is None or abs(src.drift_ms) <= self.max_drift_ms):
            return DataLabel.LIVE_VERIFIED
        return DataLabel.LIVE_UNVERIFIED

    def history(self, underlying: str, expiry: str) -> list[ChainSnapshot]:
        return list(self.chain_history.get((underlying, expiry), ()))

    def chain_age_ms(self, underlying: str) -> float | None:
        s = self.chain(underlying)
        return None if s is None else (self.clock.ts() - s.ts) * 1000.0

    def describe(self) -> dict:
        return {"sources": {k: v.to_dict() for k, v in self.sources.items()}, "instruments": len(self.quotes), "chains": len(self.chains),
                "rejections": dict(self.rejections), "pending_ticks": len(self._tick_buf)}
