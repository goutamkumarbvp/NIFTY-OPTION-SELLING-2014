"""Supporting services: official FII/DII data store, backups, configuration integrity, security probe."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select

from amrt.analytics.fii_dii import file_hash, parse_fii_dii_cash, parse_participant_oi
from amrt.core.ids import digest
from amrt.security.identity import Capability, Principal, require
from amrt.storage.db import config_versions, fii_cash, fii_participant


# ------------------------------------------------------------------ FII / DII
class FiiDiiStore:
    """Imports official NSE files. A re-import of the same date with different content is stored as a new revision."""

    def __init__(self, db, events, clock) -> None:
        self.db, self.events, self.clock = db, events, clock

    def import_participant(self, principal: Principal, raw: str, source: str, file_date: dt.date | None = None) -> dict:
        require(principal, Capability.IMPORT_DATA, "import participant OI")
        recs = parse_participant_oi(raw, file_date)
        h = file_hash(raw)
        n = 0
        with self.db.tx() as conn:
            for r in recs:
                old = conn.execute(select(fii_participant).where(fii_participant.c.date == r["date"], fii_participant.c.client_type == r["client_type"])).first()
                if old is not None and old.file_hash == h:
                    continue
                if old is not None:
                    conn.execute(fii_participant.update().where(fii_participant.c.date == r["date"], fii_participant.c.client_type == r["client_type"])
                                 .values(figures=r["figures"], source=source, file_hash=h, imported_at=self.clock.ts(), revision=old.revision + 1))
                else:
                    conn.execute(fii_participant.insert().values(date=r["date"], client_type=r["client_type"], figures=r["figures"], source=source, file_hash=h,
                                                                 imported_at=self.clock.ts(), revision=1))
                n += 1
        self.events.append("DATA_IMPORTED", {"kind": "participant_oi", "date": recs[0]["date"], "rows": n, "file_hash": h, "source": source}, principal)
        return {"date": recs[0]["date"], "rows_written": n, "file_hash": h}

    def import_cash(self, principal: Principal, raw: str, source: str) -> dict:
        require(principal, Capability.IMPORT_DATA, "import FII/DII cash")
        recs = parse_fii_dii_cash(raw)
        h = file_hash(raw)
        n = 0
        with self.db.tx() as conn:
            for r in recs:
                key = (fii_cash.c.date == r["date"], fii_cash.c.category == r["category"])
                old = conn.execute(select(fii_cash).where(*key)).first()
                vals = {k: r[k] for k in ("buy_value", "sell_value", "net_value")}
                if old is not None and old.file_hash == h:
                    continue
                if old is not None:
                    conn.execute(fii_cash.update().where(*key).values(**vals, source=source, file_hash=h, imported_at=self.clock.ts(), revision=old.revision + 1))
                else:
                    conn.execute(fii_cash.insert().values(date=r["date"], category=r["category"], **vals, source=source, file_hash=h, imported_at=self.clock.ts(), revision=1))
                n += 1
        self.events.append("DATA_IMPORTED", {"kind": "fii_dii_cash", "rows": n, "file_hash": h, "source": source}, principal)
        return {"rows_written": n, "file_hash": h}

    def participant(self, days: int = 260) -> list[dict]:
        with self.db.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(fii_participant).order_by(fii_participant.c.date.desc()).limit(days * 5))]
        return sorted(rows, key=lambda r: (r["date"], r["client_type"]))

    def cash(self, days: int = 1300) -> list[dict]:
        with self.db.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(fii_cash).order_by(fii_cash.c.date.desc()).limit(days * 2))]
        return sorted(rows, key=lambda r: (r["date"], r["category"]))


# ------------------------------------------------------------------ backups
class BackupService:
    """SQLite: online backup API. PostgreSQL: pg_dump (custom format) when available. Retains the newest N."""

    def __init__(self, db, settings, events, clock) -> None:
        self.db, self.settings, self.events, self.clock = db, settings, events, clock
        self.dir = Path(settings.runtime_dir) / "backups"
        self.last: dict[str, Any] = {}

    def run(self, principal: Principal, label: str = "scheduled") -> dict:
        require(principal, Capability.RUN_BACKUP, "backup")
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.fromtimestamp(self.clock.ts(), dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        url = self.db.url
        try:
            if url.startswith("sqlite"):
                src = url.split("///", 1)[1]
                out = self.dir / f"amrt-{stamp}.sqlite3"
                with sqlite3.connect(src) as s, sqlite3.connect(out) as d:
                    s.backup(d)
                with open(out, "rb") as f, gzip.open(str(out) + ".gz", "wb") as g:
                    shutil.copyfileobj(f, g)
                out.unlink()
                path = Path(str(out) + ".gz")
            else:
                if shutil.which("pg_dump") is None:
                    raise RuntimeError("pg_dump not installed")
                path = self.dir / f"amrt-{stamp}.dump"
                pg_url = url.replace("postgresql+psycopg://", "postgresql://")
                subprocess.run(["pg_dump", "--format=custom", f"--file={path}", pg_url], check=True, capture_output=True, timeout=600)
            res = {"ok": True, "path": str(path), "bytes": path.stat().st_size, "at": self.clock.ts(), "label": label, "schema_version": self.db.schema_version()}
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "at": self.clock.ts(), "label": label}
        self._prune()
        self.last = res
        self.events.append("BACKUP_COMPLETED" if res["ok"] else "BACKUP_FAILED", {k: v for k, v in res.items() if k != "path"} | {"file": Path(res.get("path", "")).name},
                           principal)
        return res

    def _prune(self) -> None:
        files = sorted(self.dir.glob("amrt-*"), key=lambda p: p.name, reverse=True)
        for p in files[self.settings.backup_keep:]:
            p.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        if not self.dir.exists():
            return []
        return [{"file": p.name, "bytes": p.stat().st_size} for p in sorted(self.dir.glob("amrt-*"), reverse=True)]


# ------------------------------------------------------------ config integrity
class ConfigIntegrity:
    """Hash of the effective (redacted) configuration + hard limits. The owner marks a version known-good;
    any difference afterwards is reported and blocks mode escalation until reviewed."""

    def __init__(self, db, settings, events, clock) -> None:
        self.db, self.settings, self.events, self.clock = db, settings, events, clock

    def current(self) -> dict:
        view = self.settings.public_view()
        view.pop("runtime_dir", None)
        body = {"settings": view, "hard_limits": self.settings.hard_limits().model_dump()}
        return {"hash": digest(body), "body": body}

    def record(self, actor: str) -> dict:
        cur = self.current()
        with self.db.engine.connect() as conn:
            last = conn.execute(select(config_versions).order_by(config_versions.c.version.desc()).limit(1)).first()
        if last is not None and last.hash == cur["hash"]:
            return {"version": last.version, "hash": last.hash, "new": False}
        with self.db.tx() as conn:
            r = conn.execute(config_versions.insert().values(ts=self.clock.ts(), hash=cur["hash"], body=cur["body"], actor=actor, known_good=False))
            ver = r.inserted_primary_key[0]
        return {"version": ver, "hash": cur["hash"], "new": True}

    def mark_known_good(self, principal: Principal) -> dict:
        require(principal, Capability.CHANGE_RISK_POLICY, "mark configuration known-good")
        rec = self.record(principal.id)
        with self.db.tx() as conn:
            conn.execute(config_versions.update().where(config_versions.c.version == rec["version"]).values(known_good=True))
        self.events.append("CONFIG_KNOWN_GOOD", rec, principal)
        return rec

    def status(self) -> dict:
        cur = self.current()
        with self.db.engine.connect() as conn:
            good = conn.execute(select(config_versions).where(config_versions.c.known_good.is_(True)).order_by(config_versions.c.version.desc()).limit(1)).first()
        if good is None:
            return {"ok": None, "hash": cur["hash"], "known_good": None, "note": "no known-good configuration recorded yet"}
        diff = sorted(k for k in set(cur["body"]["settings"]) | set(good.body["settings"]) if cur["body"]["settings"].get(k) != good.body["settings"].get(k))
        if cur["body"]["hard_limits"] != good.body["hard_limits"]:
            diff.append("hard_limits")
        return {"ok": cur["hash"] == good.hash, "hash": cur["hash"], "known_good": good.hash, "known_good_version": good.version, "differences": diff}


def load_events_calendar(path: str) -> list[dict]:
    """Owner-supplied JSON list of {"date": "YYYY-MM-DD", "name": str, "source": str}. Nothing is fetched or invented."""
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("date"), str) and e.get("name")]
