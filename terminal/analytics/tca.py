"""Transaction-cost analysis and Greek P&L attribution.

TCA: every order captures its arrival price (LTP / mid at submission) and, on fill,
the implementation shortfall in basis points (signed: positive = paid more / received
less than arrival), the half-spread cost and submit→fill latency; aggregated by source,
strategy and underlying. Attribution: between consecutive marks the P&L of each
position is decomposed into delta, gamma, theta and vega terms with the residual
kept explicit, accumulated per day and per strategy run.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from typing import Deque, Dict, List

from terminal.core.models import Order, Side


class TransactionCostAnalysis:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.rows: Deque[Dict] = deque(maxlen=5000)
        self._arrival: Dict[str, Dict] = {}

    def on_submit(self, order: Order) -> None:
        q = self.t.quote(order.symbol)
        if q is None:
            return
        mid = (q.bid + q.ask) / 2 if q.bid > 0 and q.ask > 0 else q.ltp
        self._arrival[order.id] = {"ts": time.time(), "ltp": q.ltp, "mid": mid, "bid": q.bid, "ask": q.ask}
        order.arrival_price = round(mid, 2)

    def on_fill(self, order: Order, price: float | None = None, qty: int | None = None) -> Dict | None:
        a = self._arrival.get(order.id)
        px = price if price is not None else order.filled_price
        if a is None or not px or a["mid"] <= 0:
            return None
        sign = 1 if order.side == Side.BUY else -1
        slippage_bps = sign * (px - a["mid"]) / a["mid"] * 1e4
        spread_bps = ((a["ask"] - a["bid"]) / a["mid"] * 1e4 / 2) if a["ask"] > 0 and a["bid"] > 0 else 0.0
        latency_ms = (time.time() - a["ts"]) * 1000
        units = qty if qty is not None else order.filled_qty
        row = {"ts": time.time(), "order_id": order.id, "symbol": order.symbol, "underlying": order.underlying, "side": order.side.value, "source": order.source.value,
               "strategy_run_id": order.strategy_run_id, "qty": units, "arrival": round(a["mid"], 2), "fill": round(px, 2), "slippage_bps": round(slippage_bps, 1),
               "half_spread_bps": round(spread_bps, 1), "latency_ms": round(latency_ms, 1), "cost_inr": round(sign * (px - a["mid"]) * units, 2), "charges": order.charges}
        self.rows.append(row)
        order.slippage_bps = row["slippage_bps"]
        order.latency_ms = row["latency_ms"]
        try:
            self.t.db.save_tca(row)
        except Exception:
            pass
        return row

    @staticmethod
    def _pct(values: List[float], p: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        return round(s[min(len(s) - 1, int(p * (len(s) - 1)))], 1)

    def summary(self) -> Dict:
        rows = list(self.rows)
        if not rows:
            return {"fills": 0, "avg_slippage_bps": None, "p90_slippage_bps": None, "total_cost_inr": 0.0, "total_charges": 0.0, "latency_ms": {"p50": None, "p95": None}, "by": {}}
        sl = [r["slippage_bps"] for r in rows]
        lat = [r["latency_ms"] for r in rows]

        def bucket(key):
            out = {}
            for r in rows:
                k = r.get(key) or "—"
                b = out.setdefault(k, {"fills": 0, "slippage": [], "cost_inr": 0.0})
                b["fills"] += 1
                b["slippage"].append(r["slippage_bps"])
                b["cost_inr"] += r["cost_inr"]
            return {k: {"fills": v["fills"], "avg_slippage_bps": round(statistics.mean(v["slippage"]), 1), "cost_inr": round(v["cost_inr"], 0)} for k, v in out.items()}

        return {"fills": len(rows), "avg_slippage_bps": round(statistics.mean(sl), 1), "p90_slippage_bps": self._pct(sl, 0.9), "total_cost_inr": round(sum(r["cost_inr"] for r in rows), 0),
                "total_charges": round(sum(r["charges"] for r in rows), 0), "latency_ms": {"p50": self._pct(lat, 0.5), "p95": self._pct(lat, 0.95)},
                "by": {"source": bucket("source"), "underlying": bucket("underlying"), "side": bucket("side")}, "recent": rows[-20:][::-1]}


class PnLAttribution:
    """Greek decomposition of mark-to-market changes: dP ≈ Δ·dS + ½Γ·dS² + Θ·dt + V·dσ (+ residual)."""

    KEYS = ("delta", "gamma", "theta", "vega", "residual")

    def __init__(self, terminal) -> None:
        self.t = terminal
        self.day = time.strftime("%Y-%m-%d")
        self.total: Dict[str, float] = dict.fromkeys(self.KEYS, 0.0)
        self.by_run: Dict[str, Dict[str, float]] = {}
        self._prev: Dict[str, Dict] = {}  # symbol -> {ltp, spot, iv, delta, gamma, theta, vega, qty, ts}

    def _roll(self) -> None:
        d = time.strftime("%Y-%m-%d")
        if d != self.day:
            self.day = d
            self.total = dict.fromkeys(self.KEYS, 0.0)
            self.by_run = {}
            self._prev = {}

    def update(self) -> None:
        self._roll()
        now = time.time()
        seen = set()
        for p in self.t.positions.open_positions():
            q = self.t.quote(p.symbol)
            spot = self.t.processor.last_price(p.underlying)
            if q is None or not spot or not p.ltp:
                continue
            seen.add(p.symbol)
            prev = self._prev.get(p.symbol)
            cur = {"ltp": p.ltp, "spot": spot, "iv": q.iv, "delta": q.delta, "gamma": q.gamma, "theta": q.theta, "vega": q.vega, "qty": p.net_qty, "ts": now}
            if prev and prev["qty"] == p.net_qty:
                ds = spot - prev["spot"]
                dsig = q.iv - prev["iv"]  # both in vol points; vega is per 1 vol point
                dt_days = (now - prev["ts"]) / 86400.0
                qty = p.net_qty
                parts = {"delta": prev["delta"] * ds * qty, "gamma": 0.5 * prev["gamma"] * ds * ds * qty, "theta": prev["theta"] * dt_days * qty, "vega": prev["vega"] * dsig * qty}
                actual = (p.ltp - prev["ltp"]) * qty
                parts["residual"] = actual - sum(parts.values())
                for k, v in parts.items():
                    self.total[k] += v
                if p.strategy_run_id:
                    b = self.by_run.setdefault(p.strategy_run_id, dict.fromkeys(self.KEYS, 0.0))
                    for k, v in parts.items():
                        b[k] += v
            self._prev[p.symbol] = cur
        for sym in list(self._prev):
            if sym not in seen:
                self._prev.pop(sym, None)

    def describe(self) -> Dict:
        return {"day": self.day, "total": {k: round(v, 0) for k, v in self.total.items()}, "explained": round(sum(v for k, v in self.total.items() if k != "residual"), 0),
                "by_run": {rid: {k: round(v, 0) for k, v in b.items()} for rid, b in list(self.by_run.items())[-20:]}}
