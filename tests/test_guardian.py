"""Guardian: self-monitoring, incident lifecycle, operator permission gate, remedies, escalation, API and Telegram."""
import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from terminal.agents.guardian import GuardianAgent
from terminal.api.app import create_app
from terminal.core.models import OrderSource
from tests.conftest import make_settings, make_terminal


@pytest.fixture
def app_and_terminal():
    t = make_terminal(make_settings(), seed=13)
    return create_app(t), t


async def _scan(t):
    return await t.guardian.scan()


async def test_guardian_watches_every_loop_and_is_quiet_when_healthy(terminal):
    t = terminal
    g: GuardianAgent = t.guardian
    assert g.enabled and "guardian-loop" in t.loop_specs() and t.loop_task("chain-loop") is not None
    await asyncio.sleep(1.2)
    assert {"chain-loop", "risk-loop", "broadcast-loop", "feed-supervisor"} <= set(t.heartbeats)
    new = await _scan(t)
    assert new == [] and g.pending() == []
    d = g.describe()
    assert d["auto_apply"] == "none" and d["watch"]["remedies"]["flatten_all"]["risk"] == "high"


async def test_crashed_loop_needs_permission_then_is_restarted_and_resolved(terminal):
    t = terminal
    g = t.guardian
    task = t.loop_task("chain-loop")
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    new = await _scan(t)
    inc = next(i for i in new if i.symptom == "LOOP_CRASHED:chain-loop")
    assert inc.status == "AWAITING_APPROVAL" and inc.remedy == "restart_loop" and inc.risk == "low"
    assert t.loop_task("chain-loop").done(), "nothing is applied before the operator permits it"
    assert any(a["event"] == "GUARDIAN_INCIDENT" for a in t.audit.tail(20))
    # duplicate scans do not create duplicate incidents
    await _scan(t)
    assert sum(1 for i in g.incidents if i.symptom == "LOOP_CRASHED:chain-loop") == 1 and inc.occurrences == 2
    # operator permits -> remedy applied -> loop alive -> next scan closes it as healed
    await g.approve(inc.id, "operator")
    assert inc.status == "APPLIED" and not t.loop_task("chain-loop").done() and "restarted" in inc.result
    await _scan(t)
    assert inc.status == "RESOLVED" and "healed" in inc.result and g.healed == 1
    assert [a["event"] for a in t.audit.tail(10) if a["event"].startswith("GUARDIAN")][-2:] == ["GUARDIAN_APPLIED", "GUARDIAN_RESOLVED"]


async def test_feed_disconnect_reconnect_with_permission(terminal):
    t = terminal
    g = t.guardian
    await t.feed.stop()
    assert not t.feed.connected
    # first sighting: the feed's own back-off gets a chance -> advisory only, nothing proposed
    inc = next(i for i in await _scan(t) if i.symptom == "FEED_DISCONNECTED")
    assert inc.status == "ADVISORY" and inc.remedy is None
    # still down after the grace period -> the same incident is upgraded to a remedy that needs permission
    g._feed_down_since = time.time() - 30
    assert await _scan(t) == []
    assert inc.remedy == "reconnect_feed" and inc.status == "AWAITING_APPROVAL" and inc.occurrences == 2
    await g.approve(inc.id, "operator")
    assert t.feed.connected and inc.status == "APPLIED"
    await asyncio.sleep(0.3)
    await _scan(t)
    assert inc.status == "RESOLVED"


async def test_rejection_mutes_and_never_acts(terminal):
    t = terminal
    g = t.guardian
    task = t.loop_task("learning-loop")
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    inc = next(i for i in await _scan(t) if i.symptom == "LOOP_CRASHED:learning-loop")
    g.reject(inc.id, "operator", "leave it")
    assert inc.status == "REJECTED" and t.loop_task("learning-loop").done()
    assert await _scan(t) == [], "a rejected incident is not re-raised immediately"
    assert any(a["event"] == "GUARDIAN_REJECTED" for a in t.audit.tail(10))


async def test_auto_apply_policy_respects_risk_and_never_flattens(terminal):
    t = terminal
    g = t.guardian
    g.auto_apply = "low"
    task = t.loop_task("broadcast-loop")
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    inc = next(i for i in await _scan(t) if i.symptom == "LOOP_CRASHED:broadcast-loop")
    assert inc.status == "APPLIED" and inc.approved_by == "guardian(auto)" and not t.loop_task("broadcast-loop").done()
    from terminal.agents.guardian import Incident
    assert not g._auto_allowed(Incident(component="x", symptom="y", remedy="flatten_all", risk="high"))
    assert not g._auto_allowed(Incident(component="x", symptom="y", remedy="relogin_session", risk="medium"))
    g.auto_apply = "all"
    assert g._auto_allowed(Incident(component="x", symptom="y", remedy="relogin_session", risk="medium"))
    assert not g._auto_allowed(Incident(component="x", symptom="y", remedy="flatten_all", risk="high"))


async def test_escalation_when_remedy_does_not_clear_symptom(terminal):
    t = terminal
    g = t.guardian
    from terminal.agents.guardian import Finding
    f = Finding(component="feed", symptom="FEED_STALE", summary="silent", remedy="reconnect_feed")
    inc = await g._raise(f)
    await g.approve(inc.id, "operator")
    inc.verify_by = time.time() - 1
    # keep the symptom "present" by patching the detector for one scan
    g.detect = lambda: [f]
    await _scan(t)
    assert inc.status == "FAILED" and "persists" in inc.result
    esc = next(i for i in g.incidents if i.escalated_from == inc.id)
    assert esc.remedy == "relogin_session" and esc.status == "AWAITING_APPROVAL" and esc.risk == "medium"
    # flatten is proposed last in the chain and still requires a human
    g.auto_apply = "all"
    esc.verify_by = time.time() - 1
    esc.status, esc.applied_at = "APPLIED", time.time()
    await _scan(t)
    esc2 = next(i for i in g.incidents if i.escalated_from == esc.id)
    assert esc2.remedy == "pause_entries" and esc2.status == "APPLIED" and t.paused
    esc2.verify_by = time.time() - 1
    await _scan(t)
    esc3 = next(i for i in g.incidents if i.escalated_from == esc2.id)
    assert esc3.remedy == "flatten_all" and esc3.status == "AWAITING_APPROVAL"


async def test_expiry_and_self_recovery(terminal):
    t = terminal
    g = t.guardian
    from terminal.agents.guardian import Finding
    f = Finding(component="council", symptom="COUNCIL_STALLED", summary="x", remedy="restart_loop", remedy_args={"loop": "council-loop"})
    inc = await g._raise(f)
    inc.expires_at = time.time() - 1
    g.detect = lambda: [f]
    await _scan(t)
    assert inc.status == "EXPIRED"
    inc2 = await g._raise(f)
    g.detect = lambda: []
    await _scan(t)
    assert inc2.status == "RESOLVED" and "self-recovered" in inc2.result


async def test_flatten_remedy_closes_positions_only_after_permission(terminal):
    t = terminal
    g = t.guardian
    plan = t.strategies.make_plan("short_strangle", "NIFTY", 1, source=OrderSource.MANUAL)
    await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    assert t.positions.open_positions()
    from terminal.agents.guardian import Finding
    inc = await g._raise(Finding(component="feed", symptom="FEED_DOWN_WITH_EXPOSURE", severity="CRITICAL", summary="blind", remedy="flatten_all"))
    assert inc.status == "AWAITING_APPROVAL" and t.positions.open_positions()
    await g.approve(inc.id, "operator")
    await asyncio.sleep(0.5)
    assert not t.positions.open_positions() and "exit order" in inc.result


async def test_guardian_api_and_telegram(app_and_terminal):
    from tests.conftest import wait_live
    app, t = app_and_terminal
    async with app.router.lifespan_context(app):
        await wait_live(t)
        task = t.loop_task("learning-loop")
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/guardian/scan")
            assert r.status_code == 200 and r.json()["data"]["pending"] == 1
            inc = r.json()["data"]["new_incidents"][0]
            r = await c.get("/api/guardian")
            assert r.json()["data"]["incidents"][0]["id"] == inc["id"]
            reply = await t.telegram.handle("/heal")
            assert inc["id"] in reply and "/heal approve" in reply
            r = await c.post(f"/api/guardian/{inc['id']}/approve")
            assert r.status_code == 200 and r.json()["data"]["status"] == "APPLIED"
            assert not t.loop_task("learning-loop").done()
            r = await c.post(f"/api/guardian/{inc['id']}/approve")
            assert r.status_code >= 400, "cannot approve twice"
            r = await c.post("/api/guardian/policy", json={"auto_apply": "low"})
            assert r.json()["data"]["auto_apply"] == "low" and t.guardian.auto_apply == "low"
            r = await c.get("/api/system/health")
            assert r.json()["data"]["services"]["guardian"]["ok"]
            assert "Guardian" in await t.telegram.handle("/heal")
