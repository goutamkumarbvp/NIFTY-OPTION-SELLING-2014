"""Run the AI Market Risk Terminal: preflight checks, then the API, dashboard and all loops in one process.

    python -m amrt              # serve (default)
    python -m amrt preflight    # print the effective (redacted) configuration and readiness, then exit
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from amrt.config import Settings
from amrt.core.logging_setup import setup_logging


def _static_dir(s: Settings) -> Path | None:
    if s.static_dir:
        return Path(s.static_dir)
    here = Path(__file__).resolve().parent.parent
    for cand in (here / "frontend" / "out", here.parent / "frontend" / "out"):
        if cand.exists():
            return cand
    return None


def preflight(s: Settings) -> dict:
    problems, notes = [], []
    if s.simulated_market and s.live_capable:
        problems.append("AMRT_SIMULATED_MARKET=true is not allowed in a LIVE_CAPABLE deployment")
    if s.live_capable and not s.data_broker:
        problems.append("LIVE_CAPABLE without AMRT_DATA_BROKER: live orders would have no verified market data")
    if s.bind_host not in ("127.0.0.1", "localhost", "::1") and not s.cookie_secure:
        notes.append("binding to a non-loopback address without AMRT_COOKIE_SECURE=true: put the dashboard behind HTTPS")
    if not s.data_broker and not s.replay_file and not s.simulated_market:
        notes.append("no market data source configured: every market screen will show DATA UNAVAILABLE")
    if s.simulated_market:
        notes.append("SIMULATED market data enabled: every number is labelled SIMULATED; nothing here is live")
    mode = "LIVE_CAPABLE (live orders enabled)" if s.live_capable else "PAPER_ONLY (live order flow disabled)"
    return {"environment": mode, "problems": problems, "notes": notes, "settings": s.public_view()}


def main(argv: list[str]) -> int:
    s = Settings()
    setup_logging(s.log_format)
    pf = preflight(s)
    if argv[:1] == ["preflight"]:
        print(json.dumps({k: v for k, v in pf.items() if k != "settings"}, indent=2))
        return 1 if pf["problems"] else 0
    if pf["problems"]:
        for p in pf["problems"]:
            print(f"FATAL: {p}", file=sys.stderr)
        return 2
    for n in pf["notes"]:
        print(f"NOTE: {n}")
    import uvicorn

    from amrt.api.server import create_api
    from amrt.app import AmrtApp
    app = AmrtApp(s)
    api = create_api(app, static_dir=_static_dir(s))
    print(f"AMRT {pf['environment']} — dashboard on http://{s.bind_host}:{s.port}  (startup mode is always PAPER)")
    uvicorn.run(api, host=s.bind_host, port=s.port, log_level="info", proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
