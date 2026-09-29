"""Institutional controls: pre-trade order controls, portfolio stress / clusters / expiry cut-off,
data-quality gate + tick journal, TCA + attribution, latency SLOs, four-eyes governance,
login lockout, backups and model versioning."""
import asyncio
import gzip

import pytest
from httpx import ASGITransport, AsyncClient

from terminal.api.app import create_app
from terminal.core.models import OrderSource, OrderStatus, OrderType, Side
from tests.conftest import make_settings, make_terminal, wait_live


# ---------------------------------------------------------------- pre-trade controls
async def test_price_collar_and_order_value_caps(terminal):
    t = terminal
    q = t.chains["NIFTY"].rows[25].ce  # ATM
    o = t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL, order_type=OrderType.LIMIT, limit_price=round(q.ltp * 1.5, 1))
    o = await t.orders.submit(o, "trader")
    assert o.status == OrderStatus.RISK_REJECTED and "PRICE_COLLAR" in o.message
    t.risk.update_limits({"max_order_value": 1000}, "tester")
    o = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert "MAX_ORDER_VALUE" in o.message
    t.risk.update_limits({"max_order_value": 2_500_000, "max_order_notional": 1000}, "tester")
    o = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert "MAX_ORDER_NOTIONAL" in o.message
    assert t.pretrade.describe()["rejections"]["MAX_ORDER_NOTIONAL"] == 1
    hist = t.db.config_history(10)
    assert hist and hist[0]["key"].startswith("risk_limits.")


async def test_throttle_duplicate_and_live_quote_gates(terminal):
    t = terminal
    q = t.chains["BANKNIFTY"].rows[20].pe
    o1 = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert o1.status == OrderStatus.FILLED and o1.arrival_price and o1.slippage_bps is not None and o1.latency_ms is not None
    # identical order inside the idempotency window is refused
    o2 = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert o2.status == OrderStatus.RISK_REJECTED and "DUPLICATE_ORDER" in o2.message
    # rate throttle: many different entries in one second
    t.risk.update_limits({"max_orders_per_second": 2}, "tester")
    rows = t.chains["NIFTY"].rows
    results = [await t.orders.submit(t.orders.build(rows[i].ce.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader") for i in (5, 6, 7, 8)]
    assert any("THROTTLED_PER_SECOND" in r.message for r in results)
    # exits are never throttled to zero
    ex = await t.orders.submit(t.orders.build(q.symbol, Side.BUY, 1, OrderSource.MANUAL), "trader")
    assert ex.status == OrderStatus.FILLED
    # a model placeholder (no broker quote) can never be traded
    q2 = t.chains["NIFTY"].rows[0].ce
    q2.live = False
    o3 = await t.orders.submit(t.orders.build(q2.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert "NO_LIVE_QUOTE" in o3.message


# ---------------------------------------------------------------- portfolio risk
async def test_stress_grid_clusters_and_stress_limit_block_entries(terminal):
    t = terminal
    plan = t.strategies.make_plan("short_straddle", "NIFTY", 2, source=OrderSource.MANUAL)
    await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    await t.risk.evaluate()
    st = t.portfolio_risk.describe()
    assert len(st["grid"]) == 36 and st["worst_loss"] < 0 and "spot" in st["worst_scenario"]
    # sanity: a 2-lot straddle cannot lose more than a 5% move × qty plus vol effect
    spot = t.processor.last_price("NIFTY")
    assert -st["worst_loss"] < spot * 0.05 * 2 * t.universe.get("NIFTY").lot_size * 1.5
    assert "NSE_EQUITY" in st["clusters"] and "NIFTY" in st["clusters"]["NSE_EQUITY"]["members"]
    r = t.risk.snapshot
    assert r.stress_worst_loss == st["worst_loss"] and "NSE_EQUITY" in r.cluster_exposure
    # tighten the stress limit below the worst case -> RED, entries blocked, exits allowed
    t.risk.update_limits({"max_stress_loss": 1.0}, "tester")
    await t.risk.evaluate()
    assert "STRESS_LOSS_LIMIT" in t.risk.snapshot.breaches and t.risk.snapshot.level.value == "RED"
    q = t.chains["BANKNIFTY"].rows[30].ce
    o = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert "PORTFOLIO_STRESS_BREACH" in o.message
    assert not t.risk.check_plan(t.strategies.make_plan("iron_condor", "BANKNIFTY", 1))["allowed"]
    assert t.positions.open_positions()
    await t.orders.flatten_all(OrderSource.MANUAL, "tester", "test")
    await asyncio.sleep(0.2)
    assert not t.positions.open_positions()
    # cluster limit
    t.risk.update_limits({"max_stress_loss": 1e9, "max_cluster_delta_notional": 1.0}, "tester")
    await t.risk.evaluate()
    await asyncio.sleep(1.1)  # gateway rate limit window
    await t.strategies.deploy(t.strategies.make_plan("bull_put_spread", "NIFTY", 1, source=OrderSource.MANUAL), "tester", OrderSource.MANUAL)
    t.positions.mark(t.quote)
    await t.risk.evaluate()
    assert any(b.startswith("CLUSTER_DELTA_NSE_EQUITY") for b in t.risk.snapshot.breaches)


def test_expiry_day_cutoff_rule(terminal):
    import datetime as dt

    from terminal.core.clock import now_ist
    pr = terminal.portfolio_risk
    now = now_ist().replace(hour=14, minute=30, second=0, microsecond=0)
    today = now.date().isoformat()
    terminal.risk.limits["expiry_gamma_cutoff_minutes"] = 90  # NSE closes 15:30 -> inside the last 90 min
    assert pr.expiry_cutoff_active(today, "NSE", now) is True
    assert pr.expiry_cutoff_active(today, "MCX", now) is False  # MCX closes 23:30
    assert pr.expiry_cutoff_active("2030-01-01", "NSE", now) is False
    terminal.risk.limits["expiry_gamma_cutoff_minutes"] = 30
    assert pr.expiry_cutoff_active(today, "NSE", now) is False
    assert pr.expiry_cutoff_active(today, "NSE", now + dt.timedelta(minutes=45)) is True
    assert pr.expiry_cutoff_active(today, "NSE", now + dt.timedelta(hours=2)) is False  # after close: nothing to cut off


# ---------------------------------------------------------------- data quality + journal
async def test_data_quality_gate_holds_outliers_and_records_ticks(terminal):
    t = terminal
    dq = t.dq
    assert dq.accepted > 0 and dq.validate("X", -1) == "NON_POSITIVE_PRICE"
    assert dq.validate("TEST", 100.0) is None
    assert dq.validate("TEST", 130.0) == "PRICE_JUMP"          # 30% jump held
    assert dq.validate("TEST", 101.0) is None                  # normal print accepted
    assert dq.validate("TEST", 131.0) == "PRICE_JUMP"          # new outlier held
    assert dq.validate("TEST", 131.5) is None                  # confirmed regime change accepted
    assert dq.last["TEST"] == 131.5
    assert dq.validate("OPT", 10.0, is_option=True) is None and dq.validate("OPT", 10.8, is_option=True) is None  # tiny absolute moves pass on options
    assert dq.validate("Q", 50.0, bid=51.0, ask=49.0) == "CROSSED_QUOTE"
    d = dq.describe()
    assert d["by_reason"]["PRICE_JUMP"] == 2 and d["recording"]
    dq.flush()
    days = dq.days()
    assert days and days[0]["bytes"] > 0
    rows = list(dq.iter_day(days[0]["day"]))
    assert rows and {r["k"] for r in rows} >= {"T", "O"} and all("s" in r and "t" in r for r in rows[:10])
    with gzip.open(t.dq.dir / f"{days[0]['day']}.jsonl.gz", "rt") as f:
        assert f.readline().startswith("{")


async def test_guardian_raises_data_quality_incident(terminal):
    t = terminal
    for i in range(30):
        t.dq.validate("BAD", 100.0 + (0 if i % 2 else 50.0))  # alternating outliers -> high reject rate
    assert t.dq.bad_symbols(60)[0]["symbol"] == "BAD"
    new = await t.guardian.scan()
    inc = next(i for i in new if i.symptom == "DATA_QUALITY")
    assert inc.remedy == "pause_entries" and inc.status == "AWAITING_APPROVAL"


# ---------------------------------------------------------------- TCA + attribution + latency
async def test_tca_and_attribution_and_latency(terminal):
    t = terminal
    plan = t.strategies.make_plan("short_strangle", "NIFTY", 1, source=OrderSource.MANUAL)
    await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    s = t.tca.summary()
    assert s["fills"] == 2 and s["avg_slippage_bps"] is not None and s["by"]["source"] and s["latency_ms"]["p50"] is not None
    assert len(t.db.tca_rows(10)) == 2 and all("slippage_bps" in r for r in t.db.tca_rows(10))
    for _ in range(3):
        await asyncio.sleep(0.5)
        t.positions.mark(t.quote)
        t.attribution.update()
    a = t.attribution.describe()
    assert set(a["total"]) == {"delta", "gamma", "theta", "vega", "residual"} and plan.id not in a["by_run"]
    assert any(rid for rid in a["by_run"])
    lat = t.latency.stats()
    assert lat["tick_to_mark"]["n"] > 0 and lat["order_submit_to_fill"]["n"] == 2 and lat["tick_to_mark"]["p95"] is not None
    t.latency.slo_ms["tick_to_mark"] = 0.0001
    for _ in range(25):
        t.latency.observe("tick_to_mark", 5.0)
    assert "tick_to_mark" in t.latency.breached()
    new = await t.guardian.scan()
    assert any(i.symptom == "LATENCY_SLO" for i in new)


# ---------------------------------------------------------------- governance, auth, backups, model versions
@pytest.fixture
def governed_app():
    t = make_terminal(make_settings(FOUR_EYES_REQUIRED="true", BIND_HOST="0.0.0.0", TERMINAL_USERS="a1:pw:admin,a2:pw:admin,v:pw:viewer", LOGIN_LOCKOUT_ATTEMPTS="3"), seed=17)
    return create_app(t), t


async def test_four_eyes_governance_and_login_lockout(governed_app):
    app, t = governed_app
    async with app.router.lifespan_context(app):
        await wait_live(t)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            tok = {}
            for u in ("a1", "a2"):
                r = await c.post("/api/auth/login", json={"username": u, "password": "pw"})
                tok[u] = {"Authorization": "Bearer " + r.json()["data"]["token"]}
            old = t.risk.limits["max_lots_per_order"]
            r = await c.post("/api/risk/limits", json={"limits": {"max_lots_per_order": 3}}, headers=tok["a1"])
            assert r.status_code == 200 and r.json()["data"]["pending_approval"]["status"] == "PENDING"
            assert t.risk.limits["max_lots_per_order"] == old, "nothing changes before the second admin approves"
            rid = r.json()["data"]["pending_approval"]["id"]
            r = await c.post(f"/api/governance/{rid}/approve", headers=tok["a1"])
            assert r.status_code >= 400, "the proposer cannot approve their own change"
            r = await c.post(f"/api/governance/{rid}/approve", headers=tok["a2"])
            assert r.json()["data"]["status"] == "APPROVED" and t.risk.limits["max_lots_per_order"] == 3
            r = await c.post("/api/mode", json={"mode": "AUTO"}, headers=tok["a1"])
            assert r.json()["data"]["mode"] == "MANUAL" and r.json()["data"]["pending_approval"]
            r = await c.post("/api/safety/gate", json={"open": True}, headers=tok["a1"])
            assert r.json()["data"]["pending_approval"] and t.governance.describe()["enabled"]
            r = await c.get("/api/governance", headers=tok["a2"])
            assert len(r.json()["data"]["pending"]) == 2 and r.json()["data"]["config_history"]
            assert any(a["event"] == "CHANGE_APPROVED" for a in t.audit.tail(30))
            # login lockout after repeated failures
            for _ in range(3):
                r = await c.post("/api/auth/login", json={"username": "v", "password": "wrong"})
                assert r.status_code == 401
            r = await c.post("/api/auth/login", json={"username": "v", "password": "pw"})
            assert r.status_code == 429
            assert "v" in app.state.auth.describe()["lockout"]["locked_users"]


async def test_backups_and_model_versions(terminal):
    t = terminal
    res = t.backups.run("test")
    assert res["ok"] and (t.backups.dir / res["file"]).exists() and any(p.name.startswith("audit-") for p in t.backups.dir.iterdir())
    t.backups.keep = 1
    t.backups.run("test2")
    assert len(list(t.backups.dir.glob("terminal-*.sqlite3"))) == 1 and t.backups.count == 2
    assert any(a["event"] == "BACKUP" for a in t.audit.tail(10))
    m = t.entry_model
    assert m.versions() == []
    v1 = m._snapshot(trained=1)
    m.model.update([0.5] * len(m.model.w), 1.0)
    v2 = m._snapshot(trained=1)
    assert [v["version"] for v in m.versions()] == [1, 2] and v2["version"] == 2
    m.rollback(1)
    assert m.model.to_dict() == v1["model"] and m.describe()["versions"] == 2
    with pytest.raises(KeyError):
        m.rollback(99)


async def test_institutional_api_surface(terminal):
    t = terminal
    app = create_app(t)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in ("/api/risk/stress", "/api/risk/pretrade", "/api/reports/tca", "/api/reports/attribution", "/api/system/data-quality", "/api/system/latency", "/api/system/backups", "/api/model/versions", "/api/governance"):
            r = await c.get(path)
            assert r.status_code == 200 and r.json()["ok"], path
        r = await c.get("/metrics")
        assert "terminal_stress_worst_loss" in r.text and "terminal_data_reject_rate_1m_pct" in r.text
        snap = t.snapshot()
        assert {"governance", "data_quality", "latency", "tca", "attribution", "backups"} <= set(snap)
