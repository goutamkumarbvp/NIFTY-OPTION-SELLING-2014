"""Operations: restore a backup and verify a database (used by deploy/scripts/restore.sh and rollback.sh).

Restore never overwrites in place: the current database is first copied aside
(…pre-restore-<stamp>) so a restore can itself be rolled back. After restore the
schema version and the audit hash chain are verified; a failed verification
leaves the copy-aside file untouched and reports NOT READY.
"""
from __future__ import annotations

import datetime as dt
import gzip
import shutil
import sqlite3
import subprocess
from pathlib import Path

from amrt.events.store import EventStore
from amrt.storage.db import MIGRATIONS, Database


def verify_database(url: str) -> dict:
    db = Database(url)
    try:
        ver = db.schema_version()
        chain = EventStore(db).verify()
        latest = max(v for v, *_ in MIGRATIONS)
        ok = chain["ok"] and ver <= latest
        return {"ok": ok, "schema_version": ver, "code_schema_version": latest, "audit_chain": chain,
                "note": "" if ver == latest else "older schema: run migrations (python -m amrt migrate)"}
    finally:
        db.dispose()


def restore_sqlite(backup: str | Path, target: str | Path) -> dict:
    backup, target = Path(backup), Path(target)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    aside = None
    if target.exists():
        aside = target.with_name(f"{target.name}.pre-restore-{stamp}")
        with sqlite3.connect(target) as src_db, sqlite3.connect(aside) as dst_db:   # online backup: includes WAL contents
            src_db.backup(dst_db)
    tmp = target.with_suffix(".restoring")
    opener = gzip.open if backup.suffix == ".gz" else open
    with opener(backup, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    res = verify_database(f"sqlite:///{tmp}")
    if not res["ok"]:
        tmp.unlink(missing_ok=True)
        return {**res, "restored": False, "kept": str(target), "reason": "backup failed verification"}
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    tmp.replace(target)
    return {**res, "restored": True, "target": str(target), "previous_copy": str(aside) if aside else None}


def restore_postgres(dump: str | Path, url: str) -> dict:
    if shutil.which("pg_restore") is None:
        return {"restored": False, "reason": "pg_restore not installed"}
    pg = url.replace("postgresql+psycopg://", "postgresql://")
    subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", pg, str(dump)], check=True, capture_output=True, timeout=1800)
    return {**verify_database(url), "restored": True}
