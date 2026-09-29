"""Phase A: broker reconciliation, restart recovery and option streaming (fake live broker, no network)."""
import asyncio

from terminal.core.models import OrderSource, OrderStatus, Side
from terminal.execution.brokers.paper import PaperBroker
from terminal.market.feed import MarketFeed


class FakeLiveBroker(PaperBroker):
    """Behaves like a live broker: orders are accepted as OPEN and fill later through fetch_order."""

    name = "fake_live"
    live = True

    def __init__(self, capital: float) -> None:
        super().__init__(capital)
        self.book: dict = {}
        self.script: dict = {}  # broker_order_id -> list of updates to serve in order
        self.positions_rows: list = []
        self.margin_used = 123456.0

    async def place(self, order, quote_lookup):
        q = quote_lookup(order.symbol)
        oid = f"LIVE-{len(self.book) + 1}"
        order.broker_order_id = oid
        order.status = OrderStatus.OPEN
        order.message = "ACCEPTED"
        self.book[oid] = {"order": order, "price": q.ltp if q else 100.0}
        return order

    async def cancel(self, order):
        order.status = OrderStatus.CANCELLED
        return order

    async def fetch_order(self, order):
        steps = self.script.get(order.broker_order_id)
        if steps:
            return steps.pop(0)
        return {"status": "OPEN", "filled_qty": order.filled_qty, "avg_price": order.filled_price or 0, "message": "pending"}

    async def broker_positions(self):
        return list(self.positions_rows)

    async def margins(self):
        return {"available": 500000.0, "used": self.margin_used}


async def _install(t):
    t.broker = FakeLiveBroker(t.settings.capital)
    await t.broker.connect()
    return t.broker


async def test_partial_and_full_fills_are_applied_from_broker(terminal):
    t = terminal
    b = await _install(t)
    q = t.chains["NIFTY"].rows[10].ce
    order = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 2, OrderSource.MANUAL), "trader")
    assert order.status == OrderStatus.OPEN and not t.positions.open_positions()
    lot = t.universe.get("NIFTY").lot_size
    b.script[order.broker_order_id] = [{"status": "OPEN", "filled_qty": lot, "avg_price": 50.0, "message": "partial"}, {"status": "FILLED", "filled_qty": 2 * lot, "avg_price": 51.0, "message": "complete"}]
    assert await t.order_reconciler.tick() == 1
    pos = t.positions.positions[q.symbol]
    assert pos.net_qty == -lot and order.status == OrderStatus.OPEN and order.filled_qty == lot
    assert await t.order_reconciler.tick() == 1
    assert order.status == OrderStatus.FILLED and t.positions.positions[q.symbol].net_qty == -2 * lot
    fills = [f for f in t.db.fills() if f["order_id"] == order.id]
    assert sorted(f["qty"] for f in fills) == [lot, lot]
    assert t.orders.trades_today == 1


async def test_broker_rejection_and_exit_guard_retry_on_live(terminal):
    t = terminal
    b = await _install(t)
    q = t.chains["NIFTY"].rows[12].pe
    lot = t.universe.get("NIFTY").lot_size
    entry = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    b.script[entry.broker_order_id] = [{"status": "FILLED", "filled_qty": lot, "avg_price": 40.0}]
    await t.order_reconciler.tick()
    assert t.positions.positions[q.symbol].net_qty == -lot
    # stop-loss exit: first attempt rejected at the broker, guard retries, second fills
    exit1 = await t.exit_guard.request(q.symbol, "STOP_LOSS", OrderSource.STRATEGY)
    b.script[exit1.broker_order_id] = [{"status": "REJECTED", "filled_qty": 0, "avg_price": 0, "message": "insufficient funds"}]
    await t.order_reconciler.tick()
    assert exit1.status == OrderStatus.REJECTED
    for _ in range(30):
        await asyncio.sleep(0.1)
        await t.exit_guard.tick()
        for oid, entry_ in list(b.book.items()):
            o = entry_["order"]
            if o is not exit1 and o.side == Side.BUY and o.status == OrderStatus.OPEN and oid not in b.script:
                b.script[oid] = [{"status": "FILLED", "filled_qty": lot, "avg_price": 41.0}]
        await t.order_reconciler.tick()
        if not t.positions.open_positions():
            break
    assert not t.positions.open_positions()
    buys = [o for o in t.orders.orders.values() if o.side == Side.BUY and o.status == OrderStatus.FILLED]
    assert len(buys) == 1 and buys[0].filled_qty == lot


async def test_position_reconciler_detects_and_adopts(terminal):
    t = terminal
    b = await _install(t)
    # give the live session a way to map broker symbols (fake session on the terminal)
    class FakeSession:
        authenticated = True
        def __init__(self):
            self.map = {}
        def lookup_trading_symbol(self, ts):
            return self.map.get(ts.upper())
    t.live = FakeSession()
    q = t.chains["NIFTY"].rows[9].ce
    t.live.map["NIFTY-BROKER-CE"] = ("NIFTY", q.expiry, q.strike, "CE")
    b.positions_rows = [{"trading_symbol": "NIFTY-BROKER-CE", "token": "1", "exchange": "NFO", "net_qty": -75, "avg_price": 55.0}]
    res = await t.position_reconciler.tick()
    assert res["ok"] is False and res["broker_only"][0]["symbol"] == q.symbol
    assert t.position_reconciler.margin["used"] == 123456.0
    assert t.risk.margin_used() == 123456.0  # broker figure replaces the estimate while fresh
    res = await t.position_reconciler.tick(adopt=True)
    assert t.positions.positions[q.symbol].net_qty == -75
    res = await t.position_reconciler.tick()
    assert res["ok"] is True


async def test_restart_recovery_restores_positions_orders_and_runs(terminal):
    from terminal.app import Terminal
    t = terminal
    plan = t.strategies.make_plan("short_strangle", "NIFTY", 1, source=OrderSource.MANUAL)
    run = await t.strategies.deploy(plan, "tester", OrderSource.MANUAL)
    symbols = {p.symbol: p.net_qty for p in t.positions.open_positions()}
    realized_before = t.positions.realized_today
    # a working (unfilled) order at a "live" broker
    await _install(t)
    q = t.chains["BANKNIFTY"].rows[8].ce
    working = await t.orders.submit(t.orders.build(q.symbol, Side.SELL, 1, OrderSource.MANUAL), "trader")
    assert working.status == OrderStatus.OPEN
    await t.stop()
    t2 = Terminal(settings=t.settings, seed=9)
    await t2.start()
    try:
        assert {p.symbol: p.net_qty for p in t2.positions.open_positions()} == symbols
        assert run.id in t2.strategies.runs and t2.strategies.runs[run.id].status == "ACTIVE"
        assert working.id in t2.orders.orders and t2.orders.orders[working.id].status == OrderStatus.OPEN
        assert t2.recovery["positions"] == 2 and t2.recovery["runs"] == 1 and t2.recovery["orders"] == 1
        assert abs(t2.positions.realized_today - realized_before) < 1e-6
        for _ in range(30):
            await asyncio.sleep(0.1)
            if t2.chains:
                break
        await t2.risk.evaluate()
        assert t2.risk.snapshot.open_lots == 2
    finally:
        await t2.stop()


async def test_option_stream_subscriptions_and_quote_overlay(terminal):
    t = terminal

    class FakeStreamFeed(MarketFeed):
        name = "fake"
        def __init__(self):
            super().__init__()
            self._option_tokens = {}
            self.connected = True
        async def start(self): ...
        async def stop(self): ...
        async def subscribe_options(self, entries):
            new = [e for e in entries if e["token"] not in self._option_tokens]
            for e in new:
                self._option_tokens[e["token"]] = e["symbol"]
            return len(new)

    class FakeLive:
        authenticated = True
        async def resolve_option(self, u, expiry, strike, ot):
            return {"token": f"{u.symbol}-{expiry}-{int(strike)}-{ot.value}", "segment": "nse_fo", "exchange": "NFO"}

    feed = FakeStreamFeed()
    feed.on_option_quote(t._on_option_quote)
    t.feed, t.live = feed, FakeLive()
    t._stop.clear()
    # one pass of the stream loop body (without waiting 8 s): call the resolution logic directly
    task = asyncio.create_task(t._stream_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    # emulate what the loop does after its initial delay
    ch = t.chains["NIFTY"]
    atm = next(r for r in ch.rows if r.strike == ch.atm_strike)
    hit = await t.live.resolve_option(t.universe.get("NIFTY"), ch.expiry, atm.strike, atm.ce.option_type)
    n = await feed.subscribe_options([{"symbol": atm.ce.symbol, "token": hit["token"], "segment": "nse_fo"}])
    assert n == 1 and feed.streamed_options() == 1
    await feed._emit_option(atm.ce.symbol, {"ltp": 999.5, "oi": 777, "volume": 5})
    assert t.stream_stats["quotes"] == 1
    rebuilt = t.chain_builder.build(t.universe.get("NIFTY"), ch.spot, 13.0, ch.expiry)
    assert rebuilt.find(atm.strike, atm.ce.option_type).ltp == 999.5
    assert rebuilt.find(atm.strike, atm.ce.option_type).oi == 777
