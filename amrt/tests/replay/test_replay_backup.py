"""Audit replay, backup restoration and rollback (AT-13, AT-16)."""
import sqlite3

import pytest
from harness import OWNER, beat_all, paper_app, warm
from sqlalchemy.exc import DatabaseError

from amrt import ops
from amrt.api.server import replay_summary
from amrt.security.identity import Principal, PrincipalKind
from amrt.storage.db import Database

pytestmark = [pytest.mark.replay, pytest.mark.recovery, pytest.mark.acceptance]


async def test_decision_and_orders_replay_from_the_log(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path)
    await warm(app)
    app.clock.advance(30)
    await app.feed.poll_once()
    await app.reconciler.reconcile_all()
    beat_all(app)
    app.mode_ctl.set_paper_auto_approve(OWNER, True)
    pkg = (await app.decisions.cycle())[0]
    assert pkg["status"] == "REQUIRES APPROVAL"
    rep = replay_summary(app.events.by_correlation(pkg["decision_id"]))
    assert rep["decision"]["status"] == "REQUIRES APPROVAL"
    assert len(rep["risk_decisions"]) == 4 and all(r["approved"] for r in rep["risk_decisions"])
    assert len(rep["orders"]) == 4 and rep["approvals"]
    assert app.events.verify()["ok"]


def test_backup_restore_and_rollback(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path)
    op = Principal.of(PrincipalKind.OPERATOR, "op")
    app.events.append("MARKER", {"n": 1}, op)
    b = app.backups.run(op, "test")
    assert b["ok"] and b["path"].endswith(".gz")
    target = app.db.url.split("///", 1)[1]
    app.events.append("AFTER_BACKUP", {"n": 2}, op)
    app.db.dispose()
    res = ops.restore_sqlite(b["path"], target)
    assert res["restored"] and res["audit_chain"]["ok"] and res["previous_copy"]
    db = Database(app.db.url)
    from amrt.events.store import EventStore
    ev = EventStore(db)
    types = [e.type for e in ev.tail(500)]
    assert "MARKER" in types and "AFTER_BACKUP" not in types                          # state rolled back to the backup point
    rollback = ops.restore_sqlite(res["previous_copy"], target)                      # roll the restore itself back
    assert rollback["restored"] and "AFTER_BACKUP" in [e.type for e in EventStore(Database(app.db.url)).tail(500)]


def test_corrupt_backup_is_refused(tmp_path):
    good = tmp_path / "live.sqlite3"
    db = Database(f"sqlite:///{good}")
    db.migrate()
    db.dispose()
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database")
    with pytest.raises((DatabaseError, sqlite3.DatabaseError)):
        ops.restore_sqlite(bad, good)
    assert ops.verify_database(f"sqlite:///{good}")["ok"]
