"""Run the AI Market Risk Terminal: preflight checks, then the API, dashboard and all loops in one process.

    python -m amrt              # serve (default)
    python -m amrt preflight    # print the effective (redacted) configuration and readiness, then exit
    python -m amrt migrate      # apply database migrations
    python -m amrt verify       # verify schema version and the audit hash chain
    python -m amrt backup       # take a backup now (runtime/backups)
    python -m amrt restore FILE # restore a backup (the current database is copied aside first)
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
    if argv[:1] in (["migrate"], ["verify"], ["backup"], ["restore"]):
        return ops_command(s, argv)
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


def ops_command(s: Settings, argv: list[str]) -> int:
    from amrt import ops
    from amrt.storage.db import Database
    cmd = argv[0]
    if cmd == "migrate":
        db = Database(s.db_url)
        print(json.dumps({"applied": db.migrate(), "schema_version": db.schema_version()}))
        return 0
    if cmd == "verify":
        res = ops.verify_database(s.db_url)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 1
    if cmd == "backup":
        from amrt.core.clock import Clock
        from amrt.events.store import EventStore
        from amrt.security.identity import Principal, PrincipalKind
        from amrt.services import BackupService
        db = Database(s.db_url)
        db.migrate()
        res = BackupService(db, s, EventStore(db), Clock()).run(Principal.of(PrincipalKind.OPERATOR, "cli"), "cli")
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 1
    if len(argv) < 2:
        print("usage: python -m amrt restore <backup file>", file=sys.stderr)
        return 2
    if s.db_url.startswith("sqlite"):
        res = ops.restore_sqlite(argv[1], s.db_url.split("///", 1)[1])
    else:
        res = ops.restore_postgres(argv[1], s.db_url)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("restored") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
