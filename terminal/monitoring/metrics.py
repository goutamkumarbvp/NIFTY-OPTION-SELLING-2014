"""Prometheus text-format metrics assembled from the terminal's own state (no client library needed)."""
from __future__ import annotations

import time
from typing import List


class Metrics:
    def __init__(self, terminal) -> None:
        self.t = terminal

    def render(self) -> str:
        t = self.t
        r = t.risk.snapshot
        orders = list(t.orders.orders.values())
        lines: List[str] = []

        def g(name: str, value, help_: str, labels: str = "") -> None:
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{{{labels}}} {value}" if labels else f"{name} {value}")

        g("terminal_up", 1, "terminal process is up")
        g("terminal_uptime_seconds", round(time.time() - t.started_at), "seconds since start")
        g("terminal_mode_auto", 1 if t.mode.value == "AUTO" else 0, "1 when in AUTO mode")
        g("terminal_paused", 1 if t.paused else 0, "1 when the engine is paused")
        g("terminal_feed_connected", 1 if t.feed.connected else 0, "market feed connected")
        g("terminal_feed_tick_age_seconds", round(time.time() - t.feed.last_tick_ts, 2) if t.feed.last_tick_ts else -1, "age of the last tick")
        g("terminal_broker_connected", 1 if t.broker.connected else 0, "broker connected")
        g("terminal_risk_level", {"GREEN": 0, "AMBER": 1, "RED": 2, "HALTED": 3}.get(r.level.value, 0), "0 green, 1 amber, 2 red, 3 halted")
        g("terminal_safety_gate_open", 1 if r.safety_gate_open else 0, "safety gate open")
        g("terminal_kill_switch", 1 if r.kill_switch else 0, "kill switch engaged")
        g("terminal_daily_pnl", r.daily_pnl, "daily P&L in INR")
        g("terminal_margin_used", r.margin_used, "margin used in INR")
        g("terminal_margin_utilisation_pct", r.margin_utilisation_pct, "margin utilisation percent")
        g("terminal_open_lots", r.open_lots, "open lots")
        g("terminal_open_positions", r.open_positions, "open positions")
        g("terminal_portfolio_delta", r.portfolio_delta, "portfolio delta")
        g("terminal_portfolio_vega", r.portfolio_vega, "portfolio vega")
        g("terminal_orders_total", len(orders), "orders this session")
        g("terminal_orders_filled_total", sum(1 for o in orders if o.status.value == "FILLED"), "filled orders this session")
        g("terminal_orders_rejected_total", sum(1 for o in orders if "REJECT" in o.status.value), "rejected orders this session")
        g("terminal_exits_pending", len(t.exit_guard.pending()), "square-offs being retried")
        g("terminal_active_runs", len(t.strategies.active_runs()), "active strategy runs")
        g("terminal_council_cycles_total", t.council.cycle, "council cycles")
        g("terminal_loop_lag_ms", t.loop_lag_ms, "chain loop lag in ms")
        g("terminal_ws_clients", len(t.ws_clients), "dashboard websocket clients")
        for m in t.market_overview():
            if m["ltp"] is not None:
                g("terminal_spot", m["ltp"], "spot price", f'symbol="{m["symbol"]}",exchange="{m["exchange"]}"')
        return "\n".join(lines) + "\n"
