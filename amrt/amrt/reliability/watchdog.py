"""Watchdog: independent of the event loop.

In-process: a daemon thread checks the asyncio loop heartbeat and the protective
loops (Path A and Path B). If the loop stalls, or a protective loop stops beating
while positions are open, it engages kill switch B (file only — no database or
lock access, so it works when the process is wedged).

Out-of-process (`python -m amrt.reliability.watchdog --runtime-dir DIR`): watches
the heartbeat file the application writes every second and engages switch B if
it goes stale. Run it as a separate service so a crashed or hung application
still ends in a frozen, safe state.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path

from amrt.security.identity import Principal, PrincipalKind

HEARTBEAT_FILE = "heartbeat.json"
KILL_FILE = "KILL_SWITCH"


def write_heartbeat(runtime_dir: Path, info: dict) -> None:
    p = Path(runtime_dir) / HEARTBEAT_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({**info, "ts": time.time(), "pid": os.getpid()}))
    os.replace(tmp, p)


def engage_file(kill_file: Path, reason: str, by: str) -> None:
    kill_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = kill_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"engaged": True, "by": by, "kind": "WATCHDOG", "at": time.time(), "reason": reason, "variant": "FREEZE", "switch": "B"}))
    os.replace(tmp, kill_file)


class Watchdog:
    def __init__(self, safety, stall_s: float, positions_open, loop_beats: dict[str, float], protective_loops=("portfolio_risk_monitor", "safety_monitor"),
                 period_s: float = 1.0, clock=time.monotonic) -> None:
        self.principal = Principal.of(PrincipalKind.WATCHDOG, "watchdog")
        self.safety, self.stall_s, self.positions_open = safety, stall_s, positions_open
        self.loop_beats = loop_beats              # name -> monotonic timestamp, written by the loops
        self.protective_loops, self.period_s, self._clock = protective_loops, period_s, clock
        self.trips: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> str | None:
        now = self._clock()
        loop = self.loop_beats.get("event_loop")
        if loop is not None and now - loop > self.stall_s:
            return f"event loop stalled for {now - loop:.1f}s"
        try:
            exposed = bool(self.positions_open())
        except Exception:  # noqa: BLE001 - unknown exposure is treated as exposure
            exposed = True
        if exposed:
            for name in self.protective_loops:
                t = self.loop_beats.get(name)
                if t is None or now - t > self.stall_s:
                    return f"protective loop {name} silent for {'never' if t is None else f'{now - t:.1f}s'} with positions open"
        return None

    def _run(self) -> None:
        while not self._stop.wait(self.period_s):
            reason = self.check()
            if reason and not self.safety.kill_file_engaged():
                try:
                    self.safety.engage_file_switch(self.principal, reason)
                except Exception:  # noqa: BLE001
                    engage_file(self.safety.kill_file, reason, self.principal.id)
                self.trips.append({"at": time.time(), "reason": reason})

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="amrt-watchdog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def main() -> None:  # pragma: no cover - exercised by the deployment rehearsal
    ap = argparse.ArgumentParser(description="AMRT external watchdog")
    ap.add_argument("--runtime-dir", default=os.environ.get("AMRT_RUNTIME_DIR", "runtime"))
    ap.add_argument("--stall-seconds", type=float, default=float(os.environ.get("AMRT_WATCHDOG_STALL_SECONDS", "15")))
    ap.add_argument("--grace-seconds", type=float, default=60.0, help="wait this long for the first heartbeat")
    a = ap.parse_args()
    rt = Path(a.runtime_dir)
    started = time.time()
    while True:
        time.sleep(1.0)
        hb = rt / HEARTBEAT_FILE
        try:
            info = json.loads(hb.read_text())
            age = time.time() - float(info["ts"])
        except (OSError, ValueError, KeyError):
            age = None if time.time() - started < a.grace_seconds else float("inf")
        if age is not None and age > a.stall_seconds and not (rt / KILL_FILE).exists():
            engage_file(rt / KILL_FILE, f"application heartbeat stale ({age:.0f}s)", "external-watchdog")
            print(f"[watchdog] heartbeat stale ({age:.0f}s): kill switch B engaged", flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()
