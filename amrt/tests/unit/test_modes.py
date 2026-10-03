"""Mode isolation and transitions (AT-06)."""
import pytest
from harness import OWNER, live_app, paper_app

from amrt.core.enums import Mode, Side
from amrt.core.errors import InvalidTransition, NotReady, PermissionDenied
from amrt.modes.controller import CONFIRM_AUTOMATIC, ModeController
from amrt.security.identity import Principal, PrincipalKind

pytestmark = [pytest.mark.unit, pytest.mark.acceptance]


def test_paper_only_deployment_refuses_live_modes(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path)
    assert app.mode_ctl.mode == Mode.PAPER and app.accounts.live() == []
    with pytest.raises(NotReady):
        app.mode_ctl.transition(OWNER, Mode.MANUAL, step_up_ok=True)
    assert app.gateway.venues["PAPER-1"].kind == "PAPER" and len(app.gateway.venues) == 1


def test_startup_never_restores_a_live_mode(monkeypatch, tmp_path):
    rig = live_app(monkeypatch, tmp_path)
    app = rig.app
    app.db.kv_set("mode_state", {"mode": "AUTOMATIC"}, "x")
    from amrt.modes import controller as c
    app.db.kv_set(c.STATE_KEY, {"mode": "AUTOMATIC", "history": []}, "x")
    mc = ModeController(app.db, app.events, app.clock, app.settings, app.p_mode)
    assert mc.mode == Mode.PAPER
    assert app.events.by_type("MODE_RESET_ON_STARTUP")


async def test_guarded_transitions(monkeypatch, tmp_path):
    rig = live_app(monkeypatch, tmp_path)
    app = rig.app
    app.mode_ctl.mode = Mode.PAPER
    await rig.ready()
    for p in (Principal.of(PrincipalKind.MASTER_AGENT, "m"), app.p_reliability, Principal.of(PrincipalKind.OPERATOR, "op"),
              Principal.of(PrincipalKind.READ_ONLY, "v")):
        with pytest.raises(PermissionDenied):
            app.mode_ctl.transition(p, Mode.MANUAL, step_up_ok=True)
    with pytest.raises(PermissionDenied):
        app.mode_ctl.transition(OWNER, Mode.MANUAL, step_up_ok=False)
    with pytest.raises(InvalidTransition):
        app.mode_ctl.transition(OWNER, Mode.AUTOMATIC, step_up_ok=True, confirmation=CONFIRM_AUTOMATIC)
    with pytest.raises(NotReady) as e:                       # configuration not yet reviewed as known-good
        app.mode_ctl.transition(OWNER, Mode.MANUAL, step_up_ok=True)
    assert "configuration_known_good" in e.value.detail["failed"]
    app.config.mark_known_good(OWNER)
    await rig.ready()
    assert app.mode_ctl.transition(OWNER, Mode.MANUAL, step_up_ok=True)["mode"] == "MANUAL"
    with pytest.raises(PermissionDenied):
        app.mode_ctl.transition(OWNER, Mode.AUTOMATIC, step_up_ok=True, confirmation="yes")
    with pytest.raises(NotReady):                            # no automation policy yet
        app.mode_ctl.transition(OWNER, Mode.AUTOMATIC, step_up_ok=True, confirmation=CONFIRM_AUTOMATIC)
    await app.pipeline.submit(rig.intent(side=Side.BUY))
    with pytest.raises(InvalidTransition):                  # live exposure blocks the switch to PAPER
        app.mode_ctl.transition(OWNER, Mode.PAPER, step_up_ok=True)
    assert app.events.by_type("MODE_CHANGED")
