"""Integration on real PostgreSQL 16 and Redis 7: append-only triggers, chain under concurrency, order idempotency, cache/rate limiter."""
import asyncio
import threading

import pytest
from harness import live_app
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from amrt.core.enums import Side
from amrt.events.store import EventStore
from amrt.security.identity import Principal, PrincipalKind
from amrt.storage.cache import RateLimiter, RedisCache
from amrt.storage.db import Database

pytestmark = pytest.mark.integration
SYS = Principal.of(PrincipalKind.MONITORING, "it")


def test_pg_event_chain_concurrent_and_append_only(pg_db_url):
    db = Database(pg_db_url)
    assert db.migrate() == [1, 2] and db.migrate() == []
    ev = EventStore(db)

    def writer(n):
        for i in range(25):
            ev.append("T", {"w": n, "i": i}, SYS)
    ts = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    v = ev.verify()
    assert v["ok"] and v["checked"] == 100
    from amrt.core.clock import Clock
    from amrt.reliability.incidents import IncidentManager
    IncidentManager(db, ev, Clock()).open("x", "SEV3", "t", {}, SYS)
    for sql in ("UPDATE events SET type='X'", "DELETE FROM events", "TRUNCATE events", "TRUNCATE incidents", "DELETE FROM incidents",
                "TRUNCATE recovery_actions", "TRUNCATE decisions"):
        with pytest.raises(DBAPIError, match="append-only|protected"):
            with db.engine.begin() as c:
                c.execute(text(sql))
    db.dispose()


async def test_pg_full_live_path_with_unknown_and_duplicates(pg_db_url, monkeypatch, tmp_path):
    rig = live_app(monkeypatch, tmp_path, AMRT_DATABASE_URL=pg_db_url)
    assert rig.app.db.url.startswith("postgresql")
    await rig.ready()
    rig.session.behaviour = "timeout_accepted"
    r = await rig.app.pipeline.submit(rig.intent(idem="PG1"))
    assert r["order"]["state"] == "ORDER STATE UNKNOWN"
    rig.session.behaviour = "fill"
    results = await asyncio.gather(*[rig.app.pipeline.submit(rig.intent(idem="PG1")) for _ in range(5)])
    assert all(x["order"].get("duplicate") for x in results) and rig.session.places == 1
    await rig.app.reconciler.reconcile_all()
    assert rig.app.book.unknown(None) == [] and rig.app.ledger.position(rig.ACCOUNT, rig.key(25000, "CE")).net_qty == 65
    assert rig.app.events.verify()["ok"]
    # safety state persists across a restart on PostgreSQL
    rig.app.safety.engage_kill(rig.app.p_path_b, "persist")
    from amrt.risk.emergency import SafetyState
    s2 = SafetyState(rig.app.db, rig.app.events, tmp_path / "other_kill_file")
    assert s2.state["kill_switch"]["engaged"] and s2.kill_file_engaged()        # switch B re-written from switch A


def test_redis_cache_and_rate_limiter(redis_url):
    c = RedisCache(redis_url)
    c.flush()
    c.set("k", {"a": 1}, ttl=5)
    assert c.get("k") == {"a": 1} and c.ping()
    rl = RateLimiter(c, limit=3, window_seconds=10)
    assert [rl.allow("u") for _ in range(5)] == [True, True, True, False, False]


async def test_ledger_restart_reloads_positions(pg_db_url, monkeypatch, tmp_path):
    rig = live_app(monkeypatch, tmp_path, AMRT_DATABASE_URL=pg_db_url)
    await rig.ready()
    await rig.app.pipeline.submit(rig.intent(side=Side.BUY))
    from amrt.portfolio.ledger import Ledger
    L2 = Ledger(rig.app.db, rig.app.clock)
    assert L2.position(rig.ACCOUNT, rig.key(25000, "CE")).net_qty == 65
