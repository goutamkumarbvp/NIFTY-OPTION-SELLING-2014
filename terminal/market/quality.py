"""Market-data quality gate and tick recorder.

Every inbound tick / option quote is validated before it reaches the processor:
non-positive prices, impossible jumps (an outlier is held back until a second
update confirms the new level), crossed quotes and stale exchange timestamps are
counted per symbol and surfaced to the Guardian (DATA_QUALITY incident when the
reject rate climbs). Accepted data is appended to a daily gzip journal
(runtime/ticks/YYYY-MM-DD.jsonl.gz) so any session can be replayed for research
or post-mortem — real recorded data, not a simulation.
"""
from __future__ import annotations

import gzip
import json
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Iterator, List


class DataQualityMonitor:
    def __init__(self, settings, runtime_dir: Path) -> None:
        self.underlying_jump_pct = float(settings.dq_max_underlying_jump_pct)
        self.option_jump_pct = float(settings.dq_max_option_jump_pct)
        self.stale_seconds = float(settings.dq_stale_timestamp_seconds)
        self.recording = bool(settings.tick_recording)
        self.dir = runtime_dir / "ticks"
        self.last: Dict[str, float] = {}
        self.held: Dict[str, float] = {}  # symbol -> outlier price awaiting confirmation
        self.accepted = 0
        self.rejected = 0
        self.suspect = 0
        self.crossed = 0
        self.stale = 0
        self.by_reason: Dict[str, int] = {}
        self.per_symbol: Dict[str, Dict[str, int]] = {}
        self._events: Deque[tuple] = deque(maxlen=20000)  # (ts, accepted?)
        self._sym_events: Dict[str, Deque[tuple]] = {}
        self._buf: List[str] = []
        self._buf_ts = time.time()
        self.recorded = 0
        self.record_errors = 0

    # ---------------------------------------------------------------- checks
    def _reject(self, symbol: str, reason: str) -> None:
        self.rejected += 1
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
        ps = self.per_symbol.setdefault(symbol, {"accepted": 0, "rejected": 0})
        ps["rejected"] += 1
        ps["last_reason"] = reason
        self._events.append((time.time(), False))
        self._sym_events.setdefault(symbol, deque(maxlen=500)).append((time.time(), False))

    def _accept(self, symbol: str, price: float) -> None:
        self.accepted += 1
        self.last[symbol] = price
        self.held.pop(symbol, None)
        ps = self.per_symbol.setdefault(symbol, {"accepted": 0, "rejected": 0})
        ps["accepted"] += 1
        self._events.append((time.time(), True))
        self._sym_events.setdefault(symbol, deque(maxlen=500)).append((time.time(), True))

    def validate(self, symbol: str, price: float, ts: float | None = None, bid: float = 0.0, ask: float = 0.0, is_option: bool = False) -> str | None:
        """Returns None when the update is good, else the rejection reason."""
        if price is None or price <= 0:
            self._reject(symbol, "NON_POSITIVE_PRICE")
            return "NON_POSITIVE_PRICE"
        now = time.time()
        if ts and now - ts > self.stale_seconds:
            self.stale += 1  # counted, not rejected: brokers batch timestamps
        if bid and ask and ask < bid:
            self.crossed += 1
            self._reject(symbol, "CROSSED_QUOTE")
            return "CROSSED_QUOTE"
        prev = self.last.get(symbol)
        if prev:
            jump = abs(price - prev) / prev * 100
            limit = self.option_jump_pct if is_option else self.underlying_jump_pct
            if jump > limit and not (is_option and abs(price - prev) < 1.0):
                held = self.held.get(symbol)
                if held is not None and abs(price - held) / held * 100 <= limit:
                    # confirmed by a second update at the new level: accept the regime change
                    self._accept(symbol, price)
                    return None
                self.held[symbol] = price
                self.suspect += 1
                self._reject(symbol, "PRICE_JUMP")
                return "PRICE_JUMP"
        self._accept(symbol, price)
        return None

    def reject_rate(self, window_s: float = 60.0) -> float:
        now = time.time()
        recent = [ok for ts, ok in self._events if now - ts <= window_s]
        if len(recent) < 20:
            return 0.0
        return round(sum(1 for ok in recent if not ok) / len(recent) * 100, 2)

    def bad_symbols(self, window_s: float = 60.0, min_events: int = 20, min_rate: float = 50.0) -> List[Dict]:
        """Symbols whose own reject rate over the window is unacceptable (a single bad instrument
        must not hide behind thousands of good ticks from the rest of the book)."""
        now = time.time()
        out = []
        for sym, ev in self._sym_events.items():
            recent = [ok for ts, ok in ev if now - ts <= window_s]
            if len(recent) >= min_events:
                rate = sum(1 for ok in recent if not ok) / len(recent) * 100
                if rate >= min_rate:
                    out.append({"symbol": sym, "reject_rate_pct": round(rate, 1), "events": len(recent)})
        return sorted(out, key=lambda x: -x["reject_rate_pct"])[:10]

    # -------------------------------------------------------------- recorder
    def record(self, kind: str, symbol: str, fields: Dict) -> None:
        if not self.recording:
            return
        self._buf.append(json.dumps({"t": round(time.time(), 3), "k": kind, "s": symbol, **fields}, separators=(",", ":"), default=str))
        if len(self._buf) >= 500 or time.time() - self._buf_ts > 2.0:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{time.strftime('%Y-%m-%d')}.jsonl.gz"
            with gzip.open(path, "at", encoding="utf-8") as f:
                f.write("\n".join(self._buf) + "\n")
            self.recorded += len(self._buf)
        except Exception:
            self.record_errors += 1
        finally:
            self._buf = []
            self._buf_ts = time.time()

    def days(self) -> List[Dict]:
        if not self.dir.exists():
            return []
        return [{"day": p.name[:-9], "bytes": p.stat().st_size} for p in sorted(self.dir.glob("*.jsonl.gz"))]

    def iter_day(self, day: str) -> Iterator[Dict]:
        path = self.dir / f"{day}.jsonl.gz"
        if not path.exists():
            return iter(())
        return (json.loads(line) for line in gzip.open(path, "rt", encoding="utf-8") if line.strip())

    def describe(self) -> Dict:
        return {"accepted": self.accepted, "rejected": self.rejected, "suspect": self.suspect, "crossed": self.crossed, "stale_timestamps": self.stale,
                "reject_rate_1m_pct": self.reject_rate(), "by_reason": self.by_reason, "recording": self.recording, "recorded": self.recorded, "record_errors": self.record_errors,
                "worst_symbols": sorted(({"symbol": k, **v} for k, v in self.per_symbol.items() if v["rejected"]), key=lambda x: -x["rejected"])[:8]}
