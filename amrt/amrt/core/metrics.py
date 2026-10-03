"""Minimal Prometheus-compatible metrics registry (no client library dependency)."""
from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self.gauges: dict[tuple[str, tuple], float] = {}
        self.help: dict[str, str] = {}

    @staticmethod
    def _k(name: str, labels: dict | None) -> tuple[str, tuple]:
        return name, tuple(sorted((labels or {}).items()))

    def inc(self, name: str, value: float = 1.0, help: str = "", **labels) -> None:
        with self._lock:
            self.counters[self._k(name, labels)] += value
            if help:
                self.help[name] = help

    def set(self, name: str, value: float, help: str = "", **labels) -> None:
        with self._lock:
            self.gauges[self._k(name, labels)] = float(value)
            if help:
                self.help[name] = help

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for kind, store in (("counter", self.counters), ("gauge", self.gauges)):
                names = sorted({k[0] for k in store})
                for n in names:
                    if n in self.help:
                        lines.append(f"# HELP {n} {self.help[n]}")
                    lines.append(f"# TYPE {n} {kind}")
                    for (name, labels), v in sorted(store.items()):
                        if name != n:
                            continue
                        lab = ",".join(f'{a}="{b}"' for a, b in labels)
                        lines.append(f"{n}{{{lab}}} {v}" if lab else f"{n} {v}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()
