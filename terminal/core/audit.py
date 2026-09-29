"""Append-only, hash chained audit trail (tamper evident)."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuditLog:
    GENESIS = "0" * 64

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._prev = self.GENESIS
        self._count = 0
        self.verify()

    @staticmethod
    def _hash(row: Dict[str, Any]) -> str:
        body = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(body).hexdigest()

    def verify(self) -> bool:
        with self._lock:
            prev = self.GENESIS
            count = 0
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    expected = row.pop("hash", None)
                    if row.get("prev_hash") != prev or expected != self._hash(row):
                        raise RuntimeError("AUDIT_CHAIN_CORRUPTED")
                    prev = expected
                    count += 1
            self._prev, self._count = prev, count
            return True

    def record(self, event: str, detail: Optional[Dict[str, Any]] = None, actor: str = "system") -> Dict[str, Any]:
        with self._lock:
            row = {"ts": time.time(), "event": event, "actor": actor, "detail": detail or {}, "prev_hash": self._prev}
            row["hash"] = self._hash(row)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
            self._prev = row["hash"]
            self._count += 1
            return row

    def tail(self, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]

    @property
    def count(self) -> int:
        return self._count
