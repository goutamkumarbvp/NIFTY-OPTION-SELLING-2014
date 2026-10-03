"""Reliability Control Plane (AT-04, AT-11, AT-12): allowlist, bounded escalation, audit, no financial authority."""
import pytest
from harness import OWNER, paper_app

from amrt.core.enums import HealthState, Mode
from amrt.core.errors import NotReady, PermissionDenied
from amrt.reliability.supervisor import ALLOWLIST, FORBIDDEN
from amrt.storage.db import recovery_actions

pytestmark = [pytest.mark.unit, pytest.mark.acceptance]


def test_supervisor_has_no_financial_authority(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path)
    p = app.p_reliability
    for call in (lambda: app.safety.release_kill(p, {"passed": True}), lambda: app.safety.release_freeze(p, None, {"passed": True}),
                 lambda: app.safety.release_recovery_lock(p, {"passed": True}), lambda: app.safety.resume_ai(p),
                 lambda: app.unquarantine(p, "cache"), lambda: app.mode_ctl.transition(p, Mode.MANUAL, True),
                 lambda: app.policies.propose("RISK", app.builder.policy_for("PAPER-1")[0], p), lambda: app.safety.engage_kill(p, "x")):
        with pytest.raises(PermissionDenied):
            call()
    assert {"SUBMIT_ORDER", "RELEASE_KILL_SWITCH", "CHANGE_RISK_POLICY", "DELETE_RECORDS"} <= set(FORBIDDEN)
    with pytest.raises(ValueError):
        app.supervisor.register("cache", "SUBMIT_ORDER", lambda c: True)
    with pytest.raises(ValueError):
        app.supervisor.register("gateway", "RESTART_WORKER", lambda c: True)       # not restartable
    with pytest.raises(ValueError):
        app.supervisor.register("cache", "RECONNECT_STREAM", lambda c: True)       # wrong component kind
    assert "ENTER_READ_ONLY" in ALLOWLIST


async def test_bounded_escalation_is_audited_and_never_suppresses(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path)
    calls = []
    app.supervisor.handlers[(app.feed.component, "RECONNECT_STREAM")] = lambda c: calls.append(c) or False
    app.supervisor.handlers[(app.feed.component, "RESTART_WORKER")] = lambda c: calls.append(c) or False
    app.health.fail(app.feed.component, "socket closed")
    for _ in range(12):
        await app.supervisor.tick()
        app.clock.advance(60)
        if app.health.components[app.feed.component].quarantined:
            break
        app.health.fail(app.feed.component, "still down")
    assert 1 <= len(calls) <= 3                                                      # bounded attempts
    assert app.health.components[app.feed.component].quarantined
    incs = app.incidents.list()
    assert len(incs) == 1 and incs[0]["status"] == "ESCALATED"                       # one incident, escalated, not resolved or deleted
    with app.db.engine.connect() as c:
        rows = list(c.execute(recovery_actions.select()))
    assert len(rows) >= len(calls) + 1 and len({r.idempotency_key for r in rows}) == len(rows)
    assert app.events.by_type("RECOVERY_ACTION")
    for sql in ("DELETE FROM recovery_actions", "UPDATE recovery_actions SET action='X'", "DELETE FROM incidents"):
        with pytest.raises(Exception, match="protected"):
            with app.db.engine.begin() as c:
                c.exec_driver_sql(sql)
    assert len(app.incidents.list()) == 1


async def test_critical_failure_freezes_new_risk_and_sets_recovery_lock(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path)
    app.health.fail("reconciler", "crash")
    await app.supervisor.tick()
    s = app.safety.snapshot()
    assert s["freeze"]["active"] and s["recovery_lock"]["active"]
    app.health.beat("reconciler", HealthState.HEALTHY)
    await app.supervisor.tick()
    assert app.safety.snapshot()["recovery_lock"]["active"]                         # recovery alone does not release it
    with pytest.raises(NotReady):
        app.release_recovery_lock(OWNER)                                            # verification fails: loops not running
