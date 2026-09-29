"""SQLite persistence for orders, trades, strategy runs, decisions, alerts and settings."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, order_id TEXT, symbol TEXT, side TEXT, qty INTEGER, price REAL, charges REAL, strategy_run_id TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_open REAL, ts_close REAL, symbol TEXT, underlying TEXT, exchange TEXT, side TEXT, qty INTEGER, entry REAL, exit REAL, pnl REAL, charges REAL, strategy TEXT, strategy_run_id TEXT, source TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS strategy_runs (id TEXT PRIMARY KEY, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS council (id TEXT PRIMARY KEY, ts REAL, underlying TEXT, decision TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, ts REAL, level TEXT, category TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS agent_memory (key TEXT PRIMARY KEY, value TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS daily_stats (day TEXT PRIMARY KEY, realized REAL, trades INTEGER, charges REAL, data TEXT);
CREATE TABLE IF NOT EXISTS system_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, source TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS idx_trades_close ON trades(ts_close);
CREATE INDEX IF NOT EXISTS idx_syslog_ts ON system_log(ts);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ generic helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def upsert_json(self, table: str, id_: str, data: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> None:
        cols = ["id", "ts", "data"] + list((extra or {}).keys())
        vals = [id_, time.time(), json.dumps(data, default=str)] + list((extra or {}).values())
        placeholders = ",".join("?" for _ in cols)
        self._exec(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})", vals)

    def load_json(self, table: str, limit: int = 200, where: str = "", params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        rows = self._rows(f"SELECT data FROM {table} {where} ORDER BY ts DESC LIMIT ?", list(params) + [limit])
        return [json.loads(r["data"]) for r in rows]

    # ------------------------------------------------------------ orders / fills
    def save_order(self, order: Dict[str, Any]) -> None:
        self.upsert_json("orders", order["id"], order)

    def orders(self, limit: int = 300) -> List[Dict[str, Any]]:
        return self.load_json("orders", limit)

    def add_fill(self, order_id: str, symbol: str, side: str, qty: int, price: float, charges: float, run_id: Optional[str], source: str) -> None:
        self._exec("INSERT INTO fills (ts, order_id, symbol, side, qty, price, charges, strategy_run_id, source) VALUES (?,?,?,?,?,?,?,?,?)",
                   (time.time(), order_id, symbol, side, qty, price, charges, run_id, source))

    def fills(self, limit: int = 500) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._rows("SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ trades
    def add_trade(self, **kw: Any) -> None:
        cols = ["ts_open", "ts_close", "symbol", "underlying", "exchange", "side", "qty", "entry", "exit", "pnl", "charges", "strategy", "strategy_run_id", "source", "reason"]
        self._exec(f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [kw.get(c) for c in cols])

    def trades(self, limit: int = 1000, since: Optional[float] = None) -> List[Dict[str, Any]]:
        if since:
            return [dict(r) for r in self._rows("SELECT * FROM trades WHERE ts_close >= ? ORDER BY ts_close DESC LIMIT ?", (since, limit))]
        return [dict(r) for r in self._rows("SELECT * FROM trades ORDER BY ts_close DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ misc tables
    def save_run(self, run: Dict[str, Any]) -> None:
        self.upsert_json("strategy_runs", run["id"], run)

    def runs(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.load_json("strategy_runs", limit)

    def save_plan(self, plan: Dict[str, Any]) -> None:
        self.upsert_json("plans", plan["id"], plan)

    def plans(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.load_json("plans", limit)

    def save_council(self, decision: Dict[str, Any]) -> None:
        self.upsert_json("council", decision["id"], decision, {"underlying": decision.get("underlying"), "decision": decision.get("decision")})

    def council(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.load_json("council", limit)

    def save_alert(self, alert: Dict[str, Any]) -> None:
        self.upsert_json("alerts", alert["id"], alert, {"level": alert.get("level"), "category": alert.get("category")})

    def alerts(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.load_json("alerts", limit)

    def set_setting(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO settings (key, value, ts) VALUES (?,?,?)", (key, json.dumps(value, default=str), time.time()))

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def all_settings(self) -> Dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self._rows("SELECT key, value FROM settings")}

    def memory_set(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO agent_memory (key, value, ts) VALUES (?,?,?)", (key, json.dumps(value, default=str), time.time()))

    def memory_get(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM agent_memory WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def memory_all(self) -> Dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self._rows("SELECT key, value FROM agent_memory")}

    def log(self, level: str, source: str, message: str) -> None:
        self._exec("INSERT INTO system_log (ts, level, source, message) VALUES (?,?,?,?)", (time.time(), level, source, message[:2000]))
        # keep table bounded
        if int(time.time()) % 97 == 0:
            self._exec("DELETE FROM system_log WHERE id < (SELECT MAX(id) FROM system_log) - 20000")

    def logs(self, limit: int = 300, level: Optional[str] = None, source: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM system_log", []
        clauses = []
        if level:
            clauses.append("level=?")
            params.append(level)
        if source:
            clauses.append("source LIKE ?")
            params.append(f"%{source}%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._rows(sql, params)]
