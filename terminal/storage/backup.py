"""Point-in-time backups of the runtime database and audit trail (SQLite online backup API),
rotated on a schedule and at end of day, so a corrupted disk or a bad deploy never loses the
book, the fills or the evidence trail."""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Dict, List


class BackupManager:
    def __init__(self, terminal) -> None:
        self.t = terminal
        s = terminal.settings
        self.dir = Path(s.backup_dir) if s.backup_dir else s.runtime_dir / "backups"
        self.keep = int(s.backup_keep)
        self.interval = float(s.backup_interval_minutes) * 60
        self.last_ts = 0.0
        self.last_error = ""
        self.count = 0

    def run(self, label: str = "scheduled") -> Dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"
        db_path = self.dir / f"terminal-{stamp}-{label}.sqlite3"
        try:
            src = self.t.db._conn
            dst = sqlite3.connect(str(db_path))
            with dst:
                src.backup(dst)
            dst.close()
            audit_src = self.t.audit.path
            if audit_src.exists():
                shutil.copy2(audit_src, self.dir / f"audit-{stamp}-{label}.jsonl")
            self.last_ts, self.count, self.last_error = time.time(), self.count + 1, ""
            self.prune()
            self.t.audit.record("BACKUP", {"file": db_path.name, "label": label}, "backup")
            return {"ok": True, "file": db_path.name}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return {"ok": False, "error": self.last_error}

    def prune(self) -> int:
        files = sorted(self.dir.glob("terminal-*.sqlite3"))
        removed = 0
        for f in files[:-self.keep] if len(files) > self.keep else []:
            parts = f.name[len("terminal-"):].split("-")
            stamp = parts[0] + "-" + parts[1]
            for g in self.dir.glob(f"*{stamp}*"):
                g.unlink(missing_ok=True)
                removed += 1
        return removed

    def due(self) -> bool:
        return self.interval > 0 and time.time() - self.last_ts >= self.interval

    def list(self) -> List[Dict]:
        if not self.dir.exists():
            return []
        return [{"file": f.name, "bytes": f.stat().st_size, "ts": f.stat().st_mtime} for f in sorted(self.dir.glob("terminal-*.sqlite3"), reverse=True)[:20]]

    def describe(self) -> Dict:
        return {"dir": str(self.dir), "keep": self.keep, "interval_minutes": self.interval / 60, "count": self.count, "last_ts": self.last_ts, "last_error": self.last_error, "files": self.list()[:5]}
