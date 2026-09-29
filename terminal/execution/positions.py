"""Position manager: fills -> positions, MTM, realised P&L, greeks, trade book."""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from terminal.core.models import Exchange, Order, OptionQuote, OptionType, Position, Side


class PositionManager:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.positions: Dict[str, Position] = {}
        self.realized_today: float = 0.0
        self.charges_today: float = 0.0
        self._day = time.strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        day = time.strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self.realized_today = 0.0
            self.charges_today = 0.0

    def apply_fill(self, order: Order) -> Position:
        self._roll_day()
        qty = order.filled_qty * (1 if order.side == Side.BUY else -1)
        px = float(order.filled_price or 0.0)
        pos = self.positions.get(order.symbol)
        self.charges_today += order.charges
        if pos is None:
            pos = Position(symbol=order.symbol, underlying=order.underlying, exchange=order.exchange, expiry=order.expiry, strike=order.strike,
                           option_type=order.option_type, lot_size=order.lot_size, net_qty=qty, avg_price=px, ltp=px, strategy_run_id=order.strategy_run_id,
                           realized_pnl=-order.charges)
            self.positions[order.symbol] = pos
            return pos
        same_dir = (pos.net_qty >= 0 and qty > 0) or (pos.net_qty <= 0 and qty < 0)
        if same_dir or pos.net_qty == 0:
            total = abs(pos.net_qty) + abs(qty)
            pos.avg_price = (pos.avg_price * abs(pos.net_qty) + px * abs(qty)) / total if total else px
            pos.net_qty += qty
        else:
            closing = min(abs(qty), abs(pos.net_qty))
            direction = 1 if pos.net_qty > 0 else -1
            pnl = (px - pos.avg_price) * closing * direction
            pos.realized_pnl += pnl
            self.realized_today += pnl
            self.t.db.add_trade(ts_open=pos.opened_at, ts_close=time.time(), symbol=pos.symbol, underlying=pos.underlying, exchange=pos.exchange.value,
                                side="LONG" if direction > 0 else "SHORT", qty=closing, entry=pos.avg_price, exit=px, pnl=round(pnl - order.charges, 2), charges=order.charges,
                                strategy=order.tag or "manual", strategy_run_id=order.strategy_run_id or pos.strategy_run_id, source=order.source.value, reason=order.reason)
            remaining = abs(qty) - closing
            pos.net_qty += qty
            if remaining > 0:  # flipped
                pos.avg_price = px
                pos.opened_at = time.time()
        pos.realized_pnl -= order.charges
        self.realized_today -= order.charges
        if pos.net_qty == 0:
            del self.positions[order.symbol]
        pos.ltp = px
        return pos

    def mark(self, quote_lookup) -> None:
        for pos in self.positions.values():
            q: Optional[OptionQuote] = quote_lookup(pos.symbol)
            if q is None:
                continue
            pos.ltp = q.ltp
            pos.unrealized_pnl = round((q.ltp - pos.avg_price) * pos.net_qty, 2)
            pos.delta = round(q.delta * pos.net_qty, 2)
            pos.gamma = round(q.gamma * pos.net_qty, 4)
            pos.theta = round(q.theta * pos.net_qty, 2)
            pos.vega = round(q.vega * pos.net_qty, 2)

    # ------------------------------------------------------------- aggregates
    def open_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.net_qty != 0]

    def unrealized(self) -> float:
        return round(sum(p.unrealized_pnl for p in self.open_positions()), 2)

    def daily_pnl(self) -> float:
        self._roll_day()
        return round(self.realized_today + self.unrealized(), 2)

    def open_lots(self) -> int:
        return sum(p.lots for p in self.open_positions())

    def lots_by_market(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for p in self.open_positions():
            out[p.exchange.value] = out.get(p.exchange.value, 0) + p.lots
        return out

    def greeks(self) -> Dict[str, float]:
        ps = self.open_positions()
        return {"delta": round(sum(p.delta for p in ps), 2), "gamma": round(sum(p.gamma for p in ps), 4), "theta": round(sum(p.theta for p in ps), 2), "vega": round(sum(p.vega for p in ps), 2)}

    def net_short_naked(self) -> int:
        """Number of short option units without a same-type long on the same underlying."""
        naked = 0
        for p in self.open_positions():
            if p.net_qty < 0:
                hedge = any(o.net_qty > 0 and o.underlying == p.underlying and o.option_type == p.option_type and o.expiry == p.expiry for o in self.open_positions())
                if not hedge:
                    naked += abs(p.net_qty)
        return naked

    def snapshot(self) -> List[dict]:
        return [p.model_dump(mode="json") | {"lots": p.lots} for p in self.open_positions()]
