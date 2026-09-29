"""Latency SLOs: reservoir histograms for the critical paths (tick → mark, option quote → stop-loss
evaluation, order submit → ack / fill, council cycle) with p50/p95/p99 and SLO breach flags that
the Guardian turns into incidents."""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict


class LatencyTracker:
    def __init__(self, settings) -> None:
        self.slo_ms: Dict[str, float] = {"tick_to_mark": float(settings.slo_tick_to_mark_ms), "quote_to_eval": float(settings.slo_quote_to_eval_ms),
                                         "order_submit_to_fill": float(settings.slo_order_fill_ms), "council_cycle": float(settings.slo_council_cycle_ms)}
        self.samples: Dict[str, Deque[float]] = {k: deque(maxlen=3000) for k in self.slo_ms}
        self.samples["order_submit_to_ack"] = deque(maxlen=3000)
        self.breaches: Dict[str, int] = {}

    def observe(self, name: str, ms: float) -> None:
        d = self.samples.setdefault(name, deque(maxlen=3000))
        d.append(max(0.0, ms))

    def timer(self, name: str):
        t0 = time.perf_counter()

        def done() -> float:
            ms = (time.perf_counter() - t0) * 1000
            self.observe(name, ms)
            return ms
        return done

    @staticmethod
    def _pct(d: Deque[float], p: float) -> float | None:
        if not d:
            return None
        s = sorted(d)
        return round(s[min(len(s) - 1, int(p * (len(s) - 1)))], 2)

    def stats(self) -> Dict[str, Dict]:
        out = {}
        for name, d in self.samples.items():
            p95 = self._pct(d, 0.95)
            slo = self.slo_ms.get(name)
            breach = bool(slo and p95 is not None and p95 > slo and len(d) >= 20)
            if breach:
                self.breaches[name] = self.breaches.get(name, 0) + 1
            out[name] = {"n": len(d), "p50": self._pct(d, 0.5), "p95": p95, "p99": self._pct(d, 0.99), "slo_ms": slo, "breach": breach}
        return out

    def breached(self) -> Dict[str, Dict]:
        return {k: v for k, v in self.stats().items() if v["breach"]}
