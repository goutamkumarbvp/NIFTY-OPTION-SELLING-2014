"""Service health registry and lightweight system metrics (no psutil dependency)."""
from __future__ import annotations

import os
import platform
import time
from typing import Dict


def _cpu_times() -> tuple:
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        idle = float(parts[4]) + float(parts[5])
        total = sum(float(x) for x in parts[1:])
        return idle, total
    except Exception:
        return 0.0, 0.0


class HealthMonitor:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.started = time.time()
        self._last_cpu = _cpu_times()
        self.cpu_pct = 0.0
        self.services: Dict[str, dict] = {}

    def set(self, name: str, ok: bool, detail: str = "") -> None:
        self.services[name] = {"ok": ok, "detail": detail, "ts": time.time()}

    def sample(self) -> None:
        idle, total = _cpu_times()
        li, lt = self._last_cpu
        if total > lt:
            self.cpu_pct = round((1 - (idle - li) / (total - lt)) * 100, 1)
        self._last_cpu = (idle, total)

    def memory(self) -> dict:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":")
                    info[k] = float(v.strip().split()[0]) * 1024
            total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
            return {"total_mb": round(total / 1e6), "used_pct": round((1 - avail / total) * 100, 1) if total else None}
        except Exception:
            return {"total_mb": None, "used_pct": None}

    def disk(self) -> dict:
        try:
            st = os.statvfs(str(self.t.settings.runtime_dir))
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            return {"total_gb": round(total / 1e9, 1), "used_pct": round((1 - free / total) * 100, 1) if total else None}
        except Exception:
            return {"total_gb": None, "used_pct": None}

    def describe(self) -> dict:
        t = self.t
        self.set("feed", t.feed.is_fresh(t.settings.feed_stale_seconds), t.feed.name)
        self.set("broker", t.broker.connected, t.broker.name)
        self.set("council", (time.time() - t.council.last_cycle_ts) < t.settings.agent_cycle_seconds * 4 if t.council.last_cycle_ts else True, f"cycle {t.council.cycle}")
        self.set("risk", t.risk.snapshot.level.value != "HALTED", t.risk.snapshot.level.value)
        self.set("database", True, str(t.db.path.name))
        g = t.guardian
        self.set("guardian", g.enabled and (not g.last_scan_ts or (time.time() - g.last_scan_ts) < g.interval * 4), f"{g.scans} scans · {len(g.pending())} awaiting permission · {g.healed} healed" if g.enabled else "disabled")
        self.set("audit", True, f"{t.audit.count} records")
        return {"uptime_seconds": round(time.time() - self.started), "cpu_pct": self.cpu_pct, "memory": self.memory(), "disk": self.disk(), "python": platform.python_version(),
                "services": self.services, "feed": t.feed.status(), "broker": t.broker.status(), "websocket_clients": len(t.ws_clients), "loop_lag_ms": t.loop_lag_ms}
