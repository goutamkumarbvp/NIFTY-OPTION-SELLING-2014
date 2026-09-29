"""Exit guard: no duplicate buy-backs across stop-loss layers, retry until flat,
operating window and end-of-day square-off."""
import asyncio
import time

import pytest

from terminal.core.models import OrderSource, OrderStatus, Side
from terminal.execution.brokers.paper import PaperBroker


class StallingPaperBroker(PaperBroker):
    """Leaves the first `stall` reducing (BUY-to-cover) orders OPEN, like a live
    broker that has not filled a market order yet."""

    def __init__(self, capital: float, stall: int = 2) -> None:
        super().__init__(capital)
        self.stall = stall
        self.stalled = 0
        self.cancelled = 0

    async def place(self, order, quote_lookup):
        if order.side == Side.BUY and order.tag != "entry" and self.stalled < self.stall:
            self.stalled += 1
            order.status = OrderStatus.OPEN
            order.message = "STALLED_AT_BROKER"
            return order
        return await super().place(order, quote_lookup)

    async def cancel(self, order):
        self.cancelled += 1
        return await super().cancel(order)


async def _short_strangle(t):
    plan = t.strategies.make_plan("short_strangle", "NIFTY", 1, source=OrderSource.MANUAL)
    return await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)


def _buy_fills(t, symbol=None):
    return [f for f in t.db.fills() if f["side"] == "BUY" and (symbol is None or f["symbol"] == symbol)]


async def test_multiple_stop_loss_layers_never_duplicate_buybacks(terminal):
    t = terminal
    run = await _short_strangle(t)
    short_qty = {p.symbol: -p.net_qty for p in t.positions.open_positions()}
    # three layers fire at once: strategy stop-loss, risk flatten and a manual close
    await asyncio.gather(
        t.strategies.exit_run(run.id, "strategy-engine", "STOP_LOSS"),
        t.orders.flatten_all(OrderSource.SENTINEL, "risk-manager", "HALT:DAILY_LOSS_LIMIT"),
        *[t.exit_guard.request(sym, "MANUAL_CLOSE", OrderSource.MANUAL, actor="operator") for sym in short_qty],
    )
    assert not t.positions.open_positions()
    for sym, qty in short_qty.items():
        assert sum(f["qty"] for f in _buy_fills(t, sym)) == qty, "bought back exactly the short quantity, never more"
    assert t.strategies.runs[run.id].status == "CLOSED"
    events = [r["event"] for r in t.audit.tail(200)]
    assert {"EXIT_DUPLICATE_SUPPRESSED", "EXIT_SUPPRESSED_ALREADY_FLAT", "EXIT_WITHOUT_POSITION_SUPPRESSED"} & set(events), "later layers were recorded as suppressed"


async def test_exit_after_flat_never_becomes_a_new_position(terminal):
    t = terminal
    run = await _short_strangle(t)
    sym = run.legs[0].symbol
    await t.strategies.exit_run(run.id, "tester", "TARGET")
    assert not t.positions.open_positions()
    late = t.orders.build(sym, Side.BUY, 1, OrderSource.STRATEGY, reason="late stop-loss")
    late = await t.orders.submit(late, "sentinel", protective=True)
    assert late.status == OrderStatus.REJECTED and late.message == "NO_POSITION_TO_EXIT"
    assert not t.positions.open_positions()
    assert await t.exit_guard.request(sym, "STOP_LOSS", OrderSource.STRATEGY) is None


async def test_exit_quantity_is_capped_to_open_quantity(terminal):
    t = terminal
    run = await _short_strangle(t)
    sym = run.legs[0].symbol
    big = t.orders.build(sym, Side.BUY, 5, OrderSource.MANUAL, reason="oversized close")
    big = await t.orders.submit(big, "operator")
    assert big.status == OrderStatus.FILLED and big.lots == 1
    assert sym not in t.positions.positions  # flat, not flipped long


async def test_unfilled_stop_loss_is_retried_until_flat(terminal):
    t = terminal
    t.broker = StallingPaperBroker(t.settings.capital, stall=2)
    await t.broker.connect()
    run = await _short_strangle(t)
    short_qty = {p.symbol: -p.net_qty for p in t.positions.open_positions()}
    t0 = time.time()
    await t.strategies.exit_run(run.id, "strategy-engine", "STOP_LOSS")
    # first attempts stalled at the broker: position still open, run EXITING, intents pending
    assert t.positions.open_positions() and t.strategies.runs[run.id].status == "EXITING"
    assert len(t.exit_guard.pending()) == 2
    # a second stop-loss layer during the stall is suppressed, not duplicated
    dup = await t.exit_guard.request(run.legs[0].symbol, "HALT", OrderSource.SENTINEL)
    assert dup is None and t.exit_guard.intents[run.legs[0].symbol].suppressed == 1
    for _ in range(80):
        await asyncio.sleep(0.1)
        if not t.positions.open_positions():
            break
    assert not t.positions.open_positions(), "guard retried after the retry interval and flattened"
    assert time.time() - t0 >= t.settings.exit_retry_seconds
    assert t.broker.cancelled >= 1, "stale working exit was cancelled before re-sending"
    assert t.strategies.runs[run.id].status == "CLOSED"
    for sym, qty in short_qty.items():
        assert sum(f["qty"] for f in _buy_fills(t, sym)) == qty
    assert all(h["attempts"] >= 2 for h in t.exit_guard.history[-2:])


@pytest.mark.parametrize("terminal", [{"TERMINAL_START_TIME": "00:00", "TERMINAL_END_TIME": "00:01"}], indirect=True)
async def test_outside_operating_window_blocks_entries_and_squares_off(terminal):
    t = terminal
    assert not t.scheduler.in_operating_window()
    q = t.chains["NIFTY"].rows[10].ce
    entry = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert entry.status == OrderStatus.RISK_REJECTED and "OUTSIDE_OPERATING_HOURS" in entry.message
    # a multi-leg plan is refused before any leg (no wing bought and rolled back)
    plan = t.strategies.make_plan("iron_condor", "NIFTY", 1, source=OrderSource.MANUAL)
    with pytest.raises(ValueError, match="OUTSIDE_OPERATING_HOURS"):
        await t.strategies.deploy(plan, "trader", OrderSource.MANUAL)
    assert not [o for o in t.orders.orders.values() if o.status == OrderStatus.FILLED]
    # a position that somehow exists past the end time is squared off by the session check
    t.scheduler.overrides["terminal_end_time"] = "23:59"
    filled = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert filled.status == OrderStatus.FILLED
    t.scheduler.overrides["terminal_end_time"] = "00:01"
    t._eod_done_day = ""
    await t._end_of_day_check()
    assert not t.positions.open_positions()
    d = (await t.council.run_cycle(["NIFTY"], force=True))[0]
    assert d.decision in ("AVOID", "VETO")


def test_operating_window_accepts_operator_key_names():
    from tests.conftest import make_settings
    s = make_settings(ENTRY_START="09:05", EXIT_TIME="23:25")
    assert (s.terminal_start_time, s.terminal_end_time) == ("09:05", "23:25")
