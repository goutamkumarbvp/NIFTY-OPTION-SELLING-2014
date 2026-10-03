"""Failure injection (AT-10, AT-12): database loss, heartbeat loss, master failure, stale data, watchdog, broker read failure."""
import time

import pytest
from harness import beat_all, live_app

from amrt.core.enums import Side
from amrt.reliability.watchdog import Watchdog

pytestmark = [pytest.mark.chaos, pytest.mark.acceptance]


@pytest.fixture
async def rig(monkeypatch, tmp_path):
    r = live_app(monkeypatch, tmp_path)
    await r.ready()
    return r


async def test_database_loss_engages_switch_b(rig, monkeypatch):
    app = rig.app
    monkeypatch.setattr(app.db, "ping", lambda: False)
    found = await app.path_b.tick()
    assert {"code": "DATABASE_UNREACHABLE"} in found and app.safety.kill_file_engaged()


async def test_heartbeat_loss_freezes_new_risk(rig):
    app = rig.app
    app.health.fail("portfolio_risk_monitor", "loop died")
    await app.path_b.tick()
    assert any(r["code"] == "HEARTBEAT_LOSS:portfolio_risk_monitor" for r in app.safety.state["freeze"]["reasons"])
    beat_all(app)
    r = await app.pipeline.submit(rig.intent())
    assert "RK-002" in r["decision"]["failed_rules"]                     # latched until the owner releases


async def test_master_failure_suspends_ai_without_promoting_another_agent(rig):
    app = rig.app
    app.health.fail("master_agent", "exception")
    await app.path_b.tick()
    assert app.safety.state["ai_suspended"]["active"]
    pkg = {"decision_id": "DP-x", "status": "REQUIRES APPROVAL", "account_id": rig.ACCOUNT, "proposed_action": {"legs": []}}
    assert (await app.decisions.route(pkg)) == {"routed": False, "reason": "AI-driven actions suspended"}


async def test_stale_data_on_held_positions_freezes(rig):
    app = rig.app
    await app.pipeline.submit(rig.intent(side=Side.BUY))
    app.clock.advance(20)
    await app.path_b.tick()
    assert any(r["code"].startswith("STALE_DATA") for r in app.safety.state["freeze"]["reasons"])


async def test_path_a_freezes_when_pnl_is_unavailable(rig):
    app = rig.app
    await app.pipeline.submit(rig.intent(side=Side.BUY))
    app.clock.advance(20)                                                  # marks go stale: P&L cannot be computed
    await app.path_a.tick()
    assert any(r["code"].startswith("PNL_UNAVAILABLE") for r in app.safety.state["freeze"]["reasons"])


async def test_unresolved_unknown_order_freezes_after_30s(rig):
    app = rig.app
    rig.session.behaviour = "timeout"
    await app.pipeline.submit(rig.intent())
    app.clock.advance(31)
    rig.fresh_market()
    await app.path_b.tick()
    assert any(r["code"] == "ORDER_STATE_UNKNOWN" for r in app.safety.state["freeze"]["reasons"])


async def test_broker_read_failure_marks_reconciliation_stale(rig):
    app = rig.app
    rig.session.fail_reads = True
    st = await app.reconciler.reconcile_account(rig.ACCOUNT)
    assert st["last_error"] and app.events.by_type("RECONCILIATION_FAILED")
    app.clock.advance(61)
    rig.fresh_market()
    beat_all(app)
    r = await app.pipeline.submit(rig.intent())
    assert "RK-012" in r["decision"]["failed_rules"]


async def test_kill_switch_holds_when_agents_and_supervisor_are_down(rig):
    app = rig.app
    for a in app.master.all_agents():
        app.health.quarantine(f"agent:{a.name}", "chaos")
    app.health.fail("master_agent", "down")
    app.health.fail("supervisor", "down")
    from harness import OWNER
    app.engage_kill(OWNER, "owner pressed")
    r = await app.pipeline.submit(rig.intent())
    assert "RK-001" in r["decision"]["failed_rules"] and rig.session.places == 0


def test_watchdog_trips_on_silent_protective_loops(tmp_path, monkeypatch):
    from harness import paper_app
    app = paper_app(monkeypatch, tmp_path)
    now = time.monotonic()
    beats = {"event_loop": now, "portfolio_risk_monitor": now - 100, "safety_monitor": now}
    wd = Watchdog(app.safety, stall_s=5, positions_open=lambda: True, loop_beats=beats)
    assert "portfolio_risk_monitor" in wd.check()
    assert Watchdog(app.safety, 5, lambda: False, beats).check() is None          # no exposure: loop silence alone is not a trip
    beats["event_loop"] = now - 60
    assert "event loop stalled" in Watchdog(app.safety, 5, lambda: False, beats).check()
    wd.period_s = 0.05
    wd.start()
    time.sleep(0.3)
    wd.stop()
    assert app.safety.kill_file_engaged() and wd.trips
    app.safety.sync_from_file()
    assert app.safety.state["kill_switch"]["engaged"]                               # switch A latched from switch B
