import asyncio

import pytest

from terminal.core.models import OrderSource, OrderStatus, Side, TerminalMode


async def test_manual_deploy_monitor_exit(terminal):
    t = terminal
    plan = t.strategies.make_plan("short_strangle", "NIFTY", 1, source=OrderSource.MANUAL)
    assert plan.premium_collected > 0
    run = await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    assert run.status == "ACTIVE" and len(t.positions.open_positions()) == 2
    assert t.audit.verify()
    await t.risk.evaluate()
    assert t.risk.snapshot.open_lots == 2 and t.risk.snapshot.margin_used > 0
    await t.strategies.exit_run(run.id, "tester", "TEST_EXIT")
    assert run.status == "CLOSED" and not t.positions.open_positions()
    trades = t.db.trades()
    assert len(trades) == 2 and all(tr["strategy_run_id"] == run.id for tr in trades)


async def test_agent_entry_requires_approval_in_manual(terminal):
    t = terminal
    assert t.mode == TerminalMode.MANUAL
    q = t.chains["NIFTY"].rows[10].ce
    order = t.orders.build(q.symbol, Side.SELL, 1, OrderSource.AUTO, tag="short_strangle", reason="agent")
    order = await t.orders.submit(order, "council")
    assert order.status == OrderStatus.PENDING_APPROVAL
    assert t.orders.pending_approval()
    await t.orders.reject(order.id, "operator", "no thanks")
    assert order.status == OrderStatus.REJECTED
    order2 = t.orders.build(q.symbol, Side.SELL, 1, OrderSource.AUTO, tag="short_strangle")
    order2 = await t.orders.submit(order2, "council")
    await t.orders.approve(order2.id, "operator")
    assert order2.status == OrderStatus.FILLED


async def test_manual_orders_fill_immediately(terminal):
    t = terminal
    q = t.chains["BANKNIFTY"].rows[5].pe
    order = t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL, tag="manual")
    order = await t.orders.submit(order, "trader")
    assert order.status == OrderStatus.FILLED and order.charges > 0
    pos = t.positions.positions[q.symbol]
    assert pos.net_qty == -t.universe.get("BANKNIFTY").lot_size


async def test_safety_gate_blocks_entries_but_allows_exits(terminal):
    t = terminal
    q = t.chains["NIFTY"].rows[12].pe
    o = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert o.status == OrderStatus.FILLED
    t.risk.set_gate(False, "operator")
    blocked = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert blocked.status == OrderStatus.RISK_REJECTED and "SAFETY_GATE_CLOSED" in blocked.message
    exit_ = await t.orders.submit(t.orders.build(q.symbol, Side.BUY, 1, OrderSource.MANUAL), "trader")
    assert exit_.status == OrderStatus.FILLED


async def test_risk_limits_enforced(terminal):
    t = terminal
    q = t.chains["NIFTY"].rows[12].ce
    big = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 99, OrderSource.MANUAL), "trader")
    assert big.status == OrderStatus.RISK_REJECTED and "MAX_LOTS_PER_ORDER" in big.message
    t.risk.update_limits({"max_open_lots": 1}, "operator")
    ok = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert ok.status == OrderStatus.FILLED
    over = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert "MAX_OPEN_LOTS" in over.message


async def test_kill_switch_flattens_and_forces_manual(terminal):
    t = terminal
    await t.set_mode(TerminalMode.AUTO, "operator")
    plan = t.strategies.make_plan("iron_condor", "NIFTY", 1)
    await t.strategies.deploy(plan, "council", OrderSource.AUTO)
    assert t.positions.open_positions()
    await t.risk.kill("operator")
    assert not t.positions.open_positions()
    assert t.mode == TerminalMode.MANUAL and t.risk.kill_switch and not t.risk.safety_gate_open
    with pytest.raises(ValueError):
        await t.set_mode(TerminalMode.AUTO, "operator")
    t.risk.reset_kill("operator")
    t.risk.set_gate(True, "operator")
    await t.set_mode(TerminalMode.AUTO, "operator")
    assert t.mode == TerminalMode.AUTO


async def test_daily_loss_halt(terminal):
    t = terminal
    plan = t.strategies.make_plan("short_straddle", "NIFTY", 1)
    run = await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    # isolate the risk-manager path from the Sentinel's protective exit and from the per-trade
    # stop-loss, which now re-evaluates on every option tick and would otherwise win the race
    t.paused = True
    run.stop_loss_pct = 100000.0
    # force a catastrophic mark-to-market by shocking the scripted test feed
    t.risk.update_limits({"max_daily_loss": 500}, "operator")
    t.feed.shock("NIFTY", 6.0)
    for _ in range(40):
        await asyncio.sleep(0.15)
        if t.risk.halted_reason:
            break
    assert t.risk.halted_reason == "DAILY_LOSS_LIMIT"
    assert not t.positions.open_positions()
    assert not t.risk.safety_gate_open and t.paused
    assert t.strategies.runs[run.id].status == "CLOSED"


async def test_audit_chain_tamper_detected(terminal):
    t = terminal
    t.audit.record("TEST", {"x": 1})
    assert t.audit.verify()
    lines = t.audit.path.read_text().splitlines()
    lines[-1] = lines[-1].replace('"x":1', '"x":2')
    t.audit.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(RuntimeError):
        t.audit.verify()
