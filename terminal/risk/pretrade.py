"""Institution-grade order-level pre-trade controls (SEBI algo-trading style).

Every order — agent, copilot, UI, Telegram — passes through these in addition to the
portfolio gates in RiskManager: price collar, order value / notional caps, per-underlying
notional, working-order cap, rate throttling, order-to-trade ratio, duplicate
(idempotency) window, liquidity (spread, live quote, OI participation) and fat-finger
size checks. Reducing (exit) orders skip the entry-only gates but still obey the
collar and a higher throttle so that a runaway loop can never spam the broker.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from typing import Deque, Dict, List, Tuple

from terminal.core.models import Order, OrderType, Side

DEFAULT_LIMITS: Dict[str, float] = {
    "price_collar_pct": 5.0,            # limit price must be within ±x% of LTP
    "max_order_value": 2_500_000.0,     # premium value (price × qty) per order
    "max_order_notional": 50_000_000.0,  # underlying notional (spot × qty) per order
    "max_underlying_notional": 150_000_000.0,  # gross notional held per underlying
    "max_working_orders": 20,
    "max_orders_per_second": 10,          # gateway message rate; multi-leg deploys burst several legs at once
    "max_orders_per_minute": 60,
    "max_order_to_trade_ratio": 10.0,   # orders / fills once > 50 orders in the day
    "duplicate_window_seconds": 2.0,
    "max_spread_pct": 8.0,              # (ask-bid)/mid for entries
    "max_oi_participation_pct": 5.0,    # our qty / open interest per contract
    "fat_finger_lot_multiple": 4.0,     # lots > multiple × median recent lots (and > 5) is refused
    "quote_max_age_seconds": 5.0,
}


class PreTradeControls:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self._sent: Deque[float] = deque(maxlen=5000)
        self._recent: Deque[Tuple[float, str]] = deque(maxlen=500)  # (ts, idempotency key)
        self._lots_hist: Deque[int] = deque(maxlen=50)
        self.rejections: Dict[str, int] = {}
        self.checked = 0

    # ---------------------------------------------------------------- helpers
    def _limit(self, key: str) -> float:
        return float(self.t.risk.limits.get(key, DEFAULT_LIMITS[key]))

    @staticmethod
    def idempotency_key(order: Order) -> str:
        return f"{order.symbol}|{order.side.value}|{order.lots}|{order.source.value}|{order.strategy_run_id or ''}|{order.tag}"

    def record_sent(self, order: Order) -> None:
        now = time.time()
        self._sent.append(now)
        self._recent.append((now, self.idempotency_key(order)))
        self._lots_hist.append(order.lots)

    def _underlying_notional(self, underlying: str, spot: float) -> float:
        return sum(abs(p.net_qty) * spot for p in self.t.positions.open_positions() if p.underlying == underlying)

    # ---------------------------------------------------------------- checks
    def check(self, order: Order, reducing: bool) -> List[str]:
        self.checked += 1
        reasons: List[str] = []
        now = time.time()
        q = self.t.quote(order.symbol)
        spot = self.t.processor.last_price(order.underlying) or 0.0
        # rate throttling (exits get 3× headroom, never zero)
        per_sec = sum(1 for ts in self._sent if now - ts <= 1.0)
        per_min = sum(1 for ts in self._sent if now - ts <= 60.0)
        mult = 3 if reducing else 1
        if per_sec >= self._limit("max_orders_per_second") * mult:
            reasons.append("THROTTLED_PER_SECOND")
        if per_min >= self._limit("max_orders_per_minute") * mult:
            reasons.append("THROTTLED_PER_MINUTE")
        # price collar / quote sanity (applies to every order)
        if q is None or q.ltp <= 0:
            reasons.append("NO_QUOTE")
        else:
            if order.order_type == OrderType.LIMIT and order.limit_price:
                collar = self._limit("price_collar_pct") / 100.0
                if abs(order.limit_price - q.ltp) > q.ltp * collar:
                    reasons.append(f"PRICE_COLLAR_{self._limit('price_collar_pct'):g}%")
            if not reducing:
                # entries need a real broker quote, not a model placeholder
                if not q.live:
                    reasons.append("NO_LIVE_QUOTE")
                mid = (q.bid + q.ask) / 2 if q.bid > 0 and q.ask > 0 else q.ltp
                if q.bid > 0 and q.ask > 0 and q.ask < q.bid:
                    reasons.append("CROSSED_QUOTE")
                elif mid > 0 and q.bid > 0 and q.ask > 0 and (q.ask - q.bid) > 0.25 and (q.ask - q.bid) / mid * 100 > self._limit("max_spread_pct"):
                    reasons.append("ILLIQUID_SPREAD")  # cheap contracts: a few ticks of spread is normal, so an absolute floor applies
                if q.oi > 0 and order.quantity / q.oi * 100 > self._limit("max_oi_participation_pct"):
                    reasons.append("OI_PARTICIPATION")
                if order.lots > 5 and len(self._lots_hist) >= 5 and order.lots > statistics.median(self._lots_hist) * self._limit("fat_finger_lot_multiple"):
                    reasons.append("FAT_FINGER_SIZE")
        if not reducing:
            px = (order.limit_price or (q.ltp if q else 0.0))
            if px * order.quantity > self._limit("max_order_value"):
                reasons.append("MAX_ORDER_VALUE")
            if spot and spot * order.quantity > self._limit("max_order_notional"):
                reasons.append("MAX_ORDER_NOTIONAL")
            if spot and self._underlying_notional(order.underlying, spot) + spot * order.quantity > self._limit("max_underlying_notional"):
                reasons.append("MAX_UNDERLYING_NOTIONAL")
            working = sum(1 for o in self.t.orders.orders.values() if o.status.value in ("OPEN", "PENDING", "PENDING_APPROVAL"))
            if working >= self._limit("max_working_orders"):
                reasons.append("MAX_WORKING_ORDERS")
            key = self.idempotency_key(order)
            win = self._limit("duplicate_window_seconds")
            if any(k == key and now - ts <= win for ts, k in self._recent):
                reasons.append("DUPLICATE_ORDER")
            total = len(self._sent)
            fills = self.t.orders.trades_today
            if total > 50 and fills > 0 and total / fills > self._limit("max_order_to_trade_ratio"):
                reasons.append("ORDER_TO_TRADE_RATIO")
            elif total > 50 and fills == 0:
                reasons.append("ORDER_TO_TRADE_RATIO")
            # expiry-day gamma cut-off: no new short options on a contract expiring today after the cut-off
            if order.side == Side.SELL and self.t.portfolio_risk.expiry_cutoff_active(order.expiry, order.exchange.value):
                reasons.append("EXPIRY_DAY_CUTOFF")
        for r in reasons:
            self.rejections[r] = self.rejections.get(r, 0) + 1
        return reasons

    def describe(self) -> dict:
        now = time.time()
        return {"checked": self.checked, "sent_last_minute": sum(1 for ts in self._sent if now - ts <= 60.0), "sent_today": len(self._sent),
                "rejections": dict(sorted(self.rejections.items(), key=lambda kv: -kv[1])), "limits": {k: self._limit(k) for k in DEFAULT_LIMITS}}
