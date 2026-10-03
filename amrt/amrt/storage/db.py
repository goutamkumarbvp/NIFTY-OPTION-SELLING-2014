"""Database layer: SQLAlchemy Core on PostgreSQL (production) or SQLite (single-user desktop, tests).

The `events` table is append-only at the database level (triggers reject UPDATE
and DELETE on both dialects). Schema changes go through numbered migrations.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

schema_migrations = Table("schema_migrations", metadata,
                          Column("version", Integer, primary_key=True), Column("description", String(200)), Column("applied_at", Float))

events = Table("events", metadata,
               Column("seq", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
               Column("event_id", String(40), nullable=False, unique=True),
               Column("ts", Float, nullable=False, index=True),
               Column("type", String(80), nullable=False, index=True),
               Column("actor_id", String(80), nullable=False),
               Column("actor_kind", String(40), nullable=False),
               Column("correlation_id", String(64), index=True),
               Column("causation_id", String(64)),
               Column("idempotency_key", String(80), index=True),
               Column("payload", JSON, nullable=False),
               Column("prev_hash", String(64), nullable=False),
               Column("hash", String(64), nullable=False))

event_chain_head = Table("event_chain_head", metadata,
                         Column("id", Integer, primary_key=True), Column("seq", BigInteger, nullable=False), Column("hash", String(64), nullable=False))

kv = Table("kv", metadata,
           Column("key", String(120), primary_key=True), Column("value", JSON, nullable=False), Column("version", Integer, nullable=False),
           Column("updated_at", Float, nullable=False), Column("updated_by", String(80), nullable=False))

users = Table("users", metadata,
              Column("username", String(64), primary_key=True), Column("role", String(20), nullable=False),
              Column("pw_hash", String(200), nullable=False), Column("pw_salt", String(64), nullable=False),
              Column("totp_secret", String(64), nullable=True), Column("created_at", Float, nullable=False),
              Column("failed_count", Integer, nullable=False, default=0), Column("locked_until", Float, nullable=False, default=0.0),
              Column("disabled", Boolean, nullable=False, default=False))

policies = Table("policies", metadata,
                 Column("policy_id", String(40), primary_key=True), Column("kind", String(20), nullable=False, index=True),
                 Column("account_id", String(60), nullable=False, index=True), Column("version", Integer, nullable=False),
                 Column("body", JSON, nullable=False), Column("hash", String(64), nullable=False),
                 Column("status", String(20), nullable=False), Column("created_by", String(64), nullable=False), Column("created_at", Float, nullable=False),
                 Column("approved_by", String(64)), Column("approved_at", Float), Column("note", Text),
                 UniqueConstraint("kind", "account_id", "version", name="uq_policy_version"))

orders = Table("orders", metadata,
               Column("intent_id", String(40), primary_key=True), Column("account_id", String(60), nullable=False, index=True),
               Column("broker", String(20), nullable=False), Column("mode", String(12), nullable=False),
               Column("instrument_key", String(80), nullable=False, index=True), Column("side", String(4), nullable=False),
               Column("quantity", Integer, nullable=False), Column("state", String(40), nullable=False, index=True),
               Column("idempotency_key", String(80), nullable=False, unique=True), Column("client_tag", String(40), nullable=False),
               Column("broker_order_id", String(80), index=True), Column("filled_qty", Integer, nullable=False, default=0),
               Column("avg_price", Float, nullable=False, default=0.0), Column("intent", JSON, nullable=False), Column("risk_decision", JSON),
               Column("history", JSON, nullable=False), Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False),
               Column("unknown_since", Float), Column("reconciled_at", Float), Column("last_error", Text), Column("simulated", Boolean, nullable=False))

fills = Table("fills", metadata,
              Column("fill_id", String(80), primary_key=True), Column("intent_id", String(40), index=True), Column("account_id", String(60), index=True),
              Column("broker_order_id", String(80)), Column("instrument_key", String(80), nullable=False), Column("side", String(4), nullable=False),
              Column("quantity", Integer, nullable=False), Column("price", Float, nullable=False), Column("charges", Float, nullable=False, default=0.0),
              Column("ts", Float, nullable=False), Column("simulated", Boolean, nullable=False), Column("source", String(40), nullable=False))

positions = Table("positions", metadata,
                  Column("account_id", String(60), primary_key=True), Column("instrument_key", String(80), primary_key=True),
                  Column("net_qty", Integer, nullable=False), Column("avg_price", Float, nullable=False), Column("realized_pnl", Float, nullable=False),
                  Column("charges", Float, nullable=False), Column("lot_size", Integer, nullable=False), Column("updated_at", Float, nullable=False),
                  Column("simulated", Boolean, nullable=False))

decisions = Table("decisions", metadata,
                  Column("decision_id", String(40), primary_key=True), Column("ts", Float, nullable=False, index=True),
                  Column("underlying", String(30)), Column("status", String(30), nullable=False), Column("package", JSON, nullable=False))

approvals = Table("approvals", metadata,
                  Column("approval_id", String(40), primary_key=True), Column("decision_id", String(40), index=True),
                  Column("status", String(20), nullable=False), Column("created_at", Float, nullable=False), Column("expires_at", Float, nullable=False),
                  Column("decided_at", Float), Column("decided_by", String(64)), Column("body", JSON, nullable=False))

incidents = Table("incidents", metadata,
                  Column("incident_id", String(40), primary_key=True), Column("opened_at", Float, nullable=False), Column("severity", String(10), nullable=False),
                  Column("component", String(80), nullable=False), Column("title", String(300), nullable=False), Column("status", String(20), nullable=False),
                  Column("body", JSON, nullable=False), Column("closed_at", Float), Column("closed_by", String(64)))

recovery_actions = Table("recovery_actions", metadata,
                         Column("action_id", String(40), primary_key=True), Column("incident_id", String(40), index=True), Column("ts", Float, nullable=False),
                         Column("action", String(60), nullable=False), Column("component", String(80), nullable=False), Column("actor", String(80), nullable=False),
                         Column("idempotency_key", String(80), nullable=False, unique=True), Column("record", JSON, nullable=False))

chain_snapshots = Table("chain_snapshots", metadata,
                        Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
                        Column("underlying", String(30), nullable=False, index=True), Column("expiry", String(10), nullable=False),
                        Column("ts", Float, nullable=False, index=True), Column("source", String(40), nullable=False), Column("label", String(30), nullable=False),
                        Column("analytics", JSON, nullable=False), Column("rows", JSON, nullable=False))

market_ticks = Table("market_ticks", metadata,
                     Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
                     Column("ts", Float, nullable=False, index=True), Column("recv_ts", Float, nullable=False), Column("instrument_key", String(80), nullable=False, index=True),
                     Column("ltp", Float), Column("bid", Float), Column("ask", Float), Column("volume", BigInteger), Column("oi", BigInteger),
                     Column("seq", BigInteger), Column("source", String(40), nullable=False), Column("label", String(30), nullable=False))

fii_participant = Table("fii_participant", metadata,
                        Column("date", String(10), primary_key=True), Column("client_type", String(10), primary_key=True),
                        Column("figures", JSON, nullable=False), Column("source", String(300), nullable=False), Column("file_hash", String(64), nullable=False),
                        Column("imported_at", Float, nullable=False), Column("revision", Integer, nullable=False, default=1))

fii_cash = Table("fii_cash", metadata,
                 Column("date", String(10), primary_key=True), Column("category", String(10), primary_key=True),
                 Column("buy_value", Float, nullable=False), Column("sell_value", Float, nullable=False), Column("net_value", Float, nullable=False),
                 Column("source", String(300), nullable=False), Column("file_hash", String(64), nullable=False), Column("imported_at", Float, nullable=False),
                 Column("revision", Integer, nullable=False, default=1))

alerts = Table("alerts", metadata,
               Column("alert_id", String(40), primary_key=True), Column("ts", Float, nullable=False, index=True), Column("severity", String(10), nullable=False),
               Column("category", String(40), nullable=False), Column("title", String(300), nullable=False), Column("body", Text),
               Column("delivery", JSON, nullable=False), Column("acked_by", String(64)), Column("acked_at", Float))

config_versions = Table("config_versions", metadata,
                        Column("version", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
                        Column("ts", Float, nullable=False), Column("hash", String(64), nullable=False), Column("body", JSON, nullable=False),
                        Column("actor", String(64), nullable=False), Column("known_good", Boolean, nullable=False, default=False))


def _append_only_sqlite(conn: Connection) -> None:
    for op in ("UPDATE", "DELETE"):
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS events_no_{op.lower()} BEFORE {op} ON events "
                          "BEGIN SELECT RAISE(ABORT, 'events table is append-only'); END;"))


def _append_only_pg(conn: Connection) -> None:
    conn.execute(text("CREATE OR REPLACE FUNCTION amrt_append_only() RETURNS trigger AS $$ BEGIN "
                      "RAISE EXCEPTION 'events table is append-only'; END $$ LANGUAGE plpgsql;"))
    conn.execute(text("DROP TRIGGER IF EXISTS events_append_only ON events;"))
    conn.execute(text("CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events FOR EACH ROW EXECUTE FUNCTION amrt_append_only();"))


def _m1(conn: Connection, dialect: str) -> None:
    metadata.create_all(conn)
    (_append_only_pg if dialect == "postgresql" else _append_only_sqlite)(conn)
    if conn.execute(select(event_chain_head.c.id)).first() is None:
        conn.execute(event_chain_head.insert().values(id=1, seq=0, hash="0" * 64))


# tables whose rows may never be removed (incidents may still change status; the others are fully immutable)
NO_DELETE = ("incidents", "recovery_actions", "decisions")
NO_UPDATE = ("recovery_actions", "decisions")


def _m2(conn: Connection, dialect: str) -> None:
    """Incidents and recovery records cannot be deleted (incidents can never be suppressed);
    TRUNCATE is blocked on every protected table, including the event log (PostgreSQL)."""
    if dialect == "postgresql":
        conn.execute(text("CREATE OR REPLACE FUNCTION amrt_protected() RETURNS trigger AS $$ BEGIN "
                          "RAISE EXCEPTION 'table % is protected: % not allowed', TG_TABLE_NAME, TG_OP; END $$ LANGUAGE plpgsql;"))
        for t in NO_DELETE:
            ops = "UPDATE OR DELETE" if t in NO_UPDATE else "DELETE"
            conn.execute(text(f"DROP TRIGGER IF EXISTS {t}_protected ON {t};"))
            conn.execute(text(f"CREATE TRIGGER {t}_protected BEFORE {ops} ON {t} FOR EACH ROW EXECUTE FUNCTION amrt_protected();"))
        for t in ("events",) + NO_DELETE:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t};"))
            conn.execute(text(f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION amrt_protected();"))
    else:
        for t in NO_DELETE:
            for op in (("UPDATE", "DELETE") if t in NO_UPDATE else ("DELETE",)):
                conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                                  f"BEGIN SELECT RAISE(ABORT, '{t} is protected: {op} not allowed'); END;"))


MIGRATIONS: list[tuple[int, str, Any]] = [
    (1, "initial schema, append-only events, chain head", _m1),
    (2, "protect incidents, recovery actions and decisions; block TRUNCATE", _m2),
]


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(url, **kwargs)
        self.dialect = self.engine.dialect.name
        self.lock = threading.RLock()
        if self.dialect == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

    def migrate(self) -> list[int]:
        applied: list[int] = []
        with self.lock, self.engine.begin() as conn:
            schema_migrations.create(conn, checkfirst=True)
            done = {r[0] for r in conn.execute(select(schema_migrations.c.version))}
            for version, desc, fn in MIGRATIONS:
                if version in done:
                    continue
                fn(conn, self.dialect)
                conn.execute(schema_migrations.insert().values(version=version, description=desc, applied_at=time.time()))
                applied.append(version)
        return applied

    def schema_version(self) -> int:
        with self.engine.connect() as conn:
            rows = [r[0] for r in conn.execute(select(schema_migrations.c.version))]
        return max(rows) if rows else 0

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        with self.lock, self.engine.begin() as conn:
            yield conn

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- key/value
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.engine.connect() as conn:
            row = conn.execute(select(kv.c.value).where(kv.c.key == key)).first()
        return row[0] if row else default

    def kv_get_versioned(self, key: str) -> tuple[Any, int]:
        with self.engine.connect() as conn:
            row = conn.execute(select(kv.c.value, kv.c.version).where(kv.c.key == key)).first()
        return (row[0], row[1]) if row else (None, 0)

    def kv_set(self, key: str, value: Any, actor: str) -> int:
        with self.tx() as conn:
            row = conn.execute(select(kv.c.version).where(kv.c.key == key)).first()
            if row is None:
                conn.execute(kv.insert().values(key=key, value=value, version=1, updated_at=time.time(), updated_by=actor))
                return 1
            ver = row[0] + 1
            conn.execute(kv.update().where(kv.c.key == key).values(value=value, version=ver, updated_at=time.time(), updated_by=actor))
            return ver

    def dispose(self) -> None:
        self.engine.dispose()
