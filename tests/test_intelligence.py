"""Phase C/D: copilot, council evaluation + learned model, bandit selection, sentinel anomalies, journal, Telegram, metrics."""
import asyncio
import time

from httpx import ASGITransport, AsyncClient

from terminal.agents.specialists import SentinelAgent, StrategySelectorAgent
from terminal.analytics.models import FEATURES, OnlineLogit, vectorize
from terminal.core.models import OrderSource, TerminalMode


# ---------------------------------------------------------------- copilot (deterministic mode, tools)
async def test_copilot_tools_are_strict_and_answer_deterministically(terminal):
    t = terminal
    for d in t.copilot.tool_definitions():
        assert d["strict"] is True and d["input_schema"]["additionalProperties"] is False
        assert set(d["input_schema"]["required"]) == set(d["input_schema"]["properties"])
    r = await t.copilot.chat("what is my risk?", "operator")
    assert r["mode"] == "deterministic" and "Risk GREEN" in r["answer"] and r["tools"][0]["tool"] == "get_risk"
    r = await t.copilot.chat("show chain NIFTY", "operator")
    assert "ATM" in r["answer"] and r["tools"][0]["tool"] == "get_chain"
    assert (await t.copilot.run_tool("nope", {}, "x"))["error"].startswith("unknown tool")
    hist = t.db.copilot_history("operator")
    assert [h["role"] for h in hist][-2:] == ["user", "assistant"]


async def test_copilot_can_only_propose_not_trade(terminal):
    t = terminal
    r = await t.copilot.chat("propose an iron condor on NIFTY", "operator")
    assert r["tools"][0]["tool"] == "propose_plan"
    pend = t.council.pending_plans()
    assert len(pend) == 1 and pend[0].strategy == "iron_condor" and pend[0].underlying == "NIFTY"
    assert not t.positions.open_positions(), "a copilot proposal never places orders"
    assert any(x["event"] == "COPILOT_PROPOSAL" for x in t.audit.tail(50))
    assert {d["name"] for d in t.copilot.tool_definitions()} & {"place_order", "set_mode", "update_limits"} == set()


# ---------------------------------------------------------------- evaluation + learned model
async def test_decisions_are_journaled_scored_and_train_the_model(terminal):
    t = terminal
    await t.council.run_cycle(["NIFTY"], force=True)
    pending = t.db.unscored_decisions(9e12)
    assert pending and pending[0]["underlying"] == "NIFTY" and "agents" in pending[0]["features"]
    t.evaluator.horizon = 0
    await asyncio.sleep(0.3)
    assert t.evaluator.score_pending() >= 1
    rows = t.db.scored_decisions()
    assert rows and rows[0]["outcome"]["seller_friendly"] in (True, False)
    sc = t.evaluator.scorecard()
    assert sc["scored"] >= 1 and "Sentinel" in sc["agents"]
    n0 = t.entry_model.model.n
    assert t.entry_model.train_pending() >= 1 and t.entry_model.model.n > n0
    assert t.db.memory_get("entry_quality_model")["n"] == t.entry_model.model.n
    d = (await t.council.run_cycle(["NIFTY"], force=True))[0]
    lm = next(a for a in d.assessments if a.agent == "LearnedModel")
    assert 0.0 <= lm.data["p"] <= 1.0 and lm.stance in ("SELLER_FRIENDLY", "NEUTRAL", "HOSTILE")


def test_online_logit_learns_a_separable_rule():
    m = OnlineLogit(lr=0.2)
    xs = [[1.0] + [0.0] * (len(FEATURES) - 1), [-1.0] + [0.0] * (len(FEATURES) - 1)]
    for _ in range(200):
        m.update(xs[0], 1.0)
        m.update(xs[1], 0.0)
    assert m.predict(xs[0]) > 0.85 and m.predict(xs[1]) < 0.15
    assert vectorize({"rsi": 50, "iv_atm": 14, "realized_vol": 12, "vix_rank": 50, "pcr": 1.0, "dte": 5, "ret_5m_pct": 0.1, "trend": "FLAT", "regime": "RANGE"}) is not None
    assert vectorize({"rsi": 50}) is None


# ---------------------------------------------------------------- bandit selection
async def test_strategy_selector_prefers_proven_strategy(terminal):
    t = terminal
    ctx = t.council.build_context("NIFTY")
    ctx.memory["regime"] = "RANGE"
    ctx.memory["vol_score"] = 0.0
    ctx.memory["flow"] = {}
    ctx.memory["strategy_stats"] = {"iron_fly:RANGE": {"n": 40, "wins": 38, "pnl": 100000, "win_rate": 0.95}, "short_strangle:RANGE": {"n": 40, "wins": 4, "pnl": -50000, "win_rate": 0.1},
                                    "iron_condor:RANGE": {"n": 40, "wins": 20, "pnl": 0, "win_rate": 0.5}, "short_straddle:RANGE": {"n": 40, "wins": 20, "pnl": 0, "win_rate": 0.5}}
    picks = {}
    for k in range(30):
        ctx.ts = 1000 + k
        a = await StrategySelectorAgent(t).assess(ctx)
        picks[a.data["strategy"]] = picks.get(a.data["strategy"], 0) + 1
    assert picks.get("iron_fly", 0) >= 20, picks
    assert picks.get("short_strangle", 0) <= 2, "a 10% win-rate structure is almost never chosen"


# ---------------------------------------------------------------- sentinel anomalies
async def test_sentinel_flags_oi_unwind_and_spread_blowout(terminal):
    t = terminal
    ctx = t.council.build_context("NIFTY")
    ch = ctx.chain
    total = ch.total_ce_oi + ch.total_pe_oi
    ctx.pcr_history = [{"ts": time.time() - 60 * i, "pcr": ch.pcr, "total_oi": int(total * 1.3)} for i in range(6, 0, -1)]
    a = await SentinelAgent(t).assess(ctx)
    assert any("OI unwind" in f for f in a.findings)
    atm = next(r for r in ch.rows if r.strike == ch.atm_strike)
    atm.ce.bid, atm.ce.ask = max(0.05, atm.ce.ltp * 0.85), atm.ce.ltp * 1.15
    a = await SentinelAgent(t).assess(ctx)
    assert a.veto and any("blowout" in f for f in a.findings)


# ---------------------------------------------------------------- journal
async def test_journal_entry_and_similar_days(terminal):
    t = terminal
    plan = t.strategies.make_plan("short_strangle", "NIFTY", 1, source=OrderSource.MANUAL)
    run = await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    await t.strategies.exit_run(run.id, "tester", "TARGET")
    entry = t.journal.write_today()
    assert entry["trades"] == 2 and abs(entry["pnl"] - sum(float(x["pnl"]) for x in t.db.trades())) < 1e-6
    assert entry["runs"][0]["strategy"] == "short_strangle" and entry["lessons"]
    assert t.db.journal(5)[0]["day"] == entry["day"]
    sim = t.journal.similar_days(entry["features"])
    assert sim and sim[0]["day"] == entry["day"] and sim[0]["distance"] == 0.0


# ---------------------------------------------------------------- telegram dispatcher
async def test_telegram_commands(terminal):
    t = terminal
    tg = t.telegram
    assert "MANUAL/PAPER" in await tg.handle("/status")
    assert "Usage" in await tg.handle("/gate")
    assert "CLOSED" in await tg.handle("/gate close") and not t.risk.safety_gate_open
    assert "OPEN" in await tg.handle("/gate open") and t.risk.safety_gate_open
    assert "AUTO" in await tg.handle("/mode auto") and t.mode == TerminalMode.AUTO
    plan = t.strategies.make_plan("iron_condor", "NIFTY", 1, source=OrderSource.MANUAL)
    await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    assert "iron_condor" in await tg.handle("/runs")
    assert "confirm" in await tg.handle("/kill")
    assert t.positions.open_positions()
    assert "KILL SWITCH" in await tg.handle("/kill confirm")
    assert not t.positions.open_positions() and t.mode == TerminalMode.MANUAL
    assert "Commands" in await tg.handle("/whatever")
    assert "Risk" in await tg.handle("/ask risk")


# ---------------------------------------------------------------- HTTP: metrics + insights + copilot
async def test_metrics_and_insight_routes():
    from terminal.api.app import create_app
    from tests.conftest import make_settings, make_terminal, wait_live
    t = make_terminal(make_settings(), seed=21)
    app = create_app(t)
    async with app.router.lifespan_context(app):
        await wait_live(t)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            m = (await c.get("/metrics")).text
            assert "terminal_up 1" in m and 'terminal_spot{symbol="NIFTY"' in m
            r = await c.get("/api/journal")
            assert r.status_code == 200 and "today" in r.json()["data"]
            r = await c.get("/api/agents/evaluation")
            assert r.json()["data"]["model"]["samples"] >= 0
            r = await c.post("/api/copilot/chat", json={"message": "positions"})
            assert r.status_code == 200 and r.json()["data"]["mode"] == "deterministic"
            r = await c.get("/api/copilot")
            assert "propose_plan" in r.json()["data"]["tools"]
            r = await c.get("/api/broker/reconcile")
            assert r.json()["data"]["positions"]["ok"] is None
