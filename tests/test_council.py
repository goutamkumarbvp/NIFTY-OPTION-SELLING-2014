import asyncio

import pytest

from terminal.core.models import OrderSource, TerminalMode


async def test_council_cycle_produces_decisions(terminal):
    t = terminal
    decisions = await t.council.run_cycle(["NIFTY"], force=True)
    assert len(decisions) == 1
    d = decisions[0]
    assert {a.agent for a in d.assessments} >= {"MarketAnalyst", "VolatilityAgent", "OptionsFlow", "EventRisk", "Sentinel", "RiskGuardian", "StrategySelector"}
    assert -1 <= d.consensus <= 1 and d.decision in {"HOLD", "AVOID", "VETO", "PROPOSE", "ENTER", "PROTECT"}
    assert all(a.stance != "ERROR" for a in d.assessments)


@pytest.mark.parametrize("terminal", [{"AUTO_MIN_CONSENSUS": "-1", "AUTO_MIN_CONFIDENCE": "0"}], indirect=True)
async def test_manual_mode_proposes_and_operator_approves(terminal):
    t = terminal
    decisions = await t.council.run_cycle(["NIFTY"], force=True)
    assert decisions[0].decision == "PROPOSE"
    pending = t.council.pending_plans()
    assert len(pending) == 1 and not t.positions.open_positions()
    run_id = await t.council.approve_plan(pending[0].id, "operator")
    assert t.strategies.runs[run_id].status == "ACTIVE" and t.positions.open_positions()


@pytest.mark.parametrize("terminal", [{"AUTO_MIN_CONSENSUS": "-1", "AUTO_MIN_CONFIDENCE": "0", "TERMINAL_MODE": "AUTO"}], indirect=True)
async def test_auto_mode_deploys_without_human(terminal):
    t = terminal
    assert t.mode == TerminalMode.AUTO
    decisions = await t.council.run_cycle(["NIFTY"], force=True)
    assert decisions[0].decision == "ENTER"
    runs = t.strategies.active_runs("NIFTY")
    assert len(runs) == 1 and runs[0].source == OrderSource.AUTO
    # second cycle holds (already managing a run) instead of stacking
    again = await t.council.run_cycle(["NIFTY"], force=True)
    assert again[0].decision == "HOLD"


@pytest.mark.parametrize("terminal", [{"AUTO_MIN_CONSENSUS": "-1", "AUTO_MIN_CONFIDENCE": "0", "TERMINAL_MODE": "AUTO"}], indirect=True)
async def test_sentinel_vetoes_on_black_swan(terminal):
    t = terminal
    t.settings.black_swan_sigma = 0.01  # any move is a black swan
    await asyncio.sleep(1.0)
    t.feed.shock("NIFTY", -2.0)
    await asyncio.sleep(0.8)
    d = (await t.council.run_cycle(["NIFTY"], force=True))[0]
    sentinel = next(a for a in d.assessments if a.agent == "Sentinel")
    assert sentinel.veto and d.decision in {"VETO", "PROTECT"}
    assert not t.strategies.active_runs()


async def test_closed_gate_is_vetoed_by_risk_guardian(terminal):
    t = terminal
    t.risk.set_gate(False, "operator")
    d = (await t.council.run_cycle(["NIFTY"], force=True))[0]
    rg = next(a for a in d.assessments if a.agent == "RiskGuardian")
    assert rg.veto and d.decision == "VETO"


async def test_agent_config_persists(terminal):
    t = terminal
    st = t.council.update_agent("VolatilityAgent", {"enabled": False, "weight": 2.2}, "operator")
    assert st["enabled"] is False and st["weight"] == 2.2
    assert t.db.get_setting("agent_config")["VolatilityAgent"] == {"enabled": False, "weight": 2.2}
