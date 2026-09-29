"""Position manager: fills -> positions, MTM, realised P&L, greeks, trade book."""
from __future__ import annotations

import time
from typing import Dict, List

from terminal.core.models import Exchange, OptionQuote, OptionType, Order, Position, Side


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

    def apply_fill(self, order: Order, qty_units: int | None = None, price: float | None = None, charges: float | None = None) -> Position:
        """Apply a (possibly partial) fill. Defaults to the order's full filled quantity."""
        self._roll_day()
        units = order.filled_qty if qty_units is None else qty_units
        qty = units * (1 if order.side == Side.BUY else -1)
        px = float(order.filled_price if price is None else price) or 0.0
        chg = order.charges if charges is None else charges
        pos = self.positions.get(order.symbol)
        self.charges_today += chg
        if pos is None:
            pos = Position(symbol=order.symbol, underlying=order.underlying, exchange=order.exchange, expiry=order.expiry, strike=order.strike,
                           option_type=order.option_type, lot_size=order.lot_size, net_qty=qty, avg_price=px, ltp=px, strategy_run_id=order.strategy_run_id,
                           realized_pnl=-chg)
            self.positions[order.symbol] = pos
            self._persist()
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
                                side="LONG" if direction > 0 else "SHORT", qty=closing, entry=pos.avg_price, exit=px, pnl=round(pnl - chg, 2), charges=chg,
                                strategy=order.tag or "manual", strategy_run_id=order.strategy_run_id or pos.strategy_run_id, source=order.source.value, reason=order.reason)
            remaining = abs(qty) - closing
            pos.net_qty += qty
            if remaining > 0:  # flipped
                pos.avg_price = px
                pos.opened_at = time.time()
        pos.realized_pnl -= chg
        self.realized_today -= chg
        if pos.net_qty == 0:
            del self.positions[order.symbol]
        pos.ltp = px
        self._persist()
        return pos

    # ------------------------------------------------------------- persistence / recovery
    def _persist(self) -> None:
        try:
            self.t.db.save_positions([p.model_dump(mode="json") for p in self.positions.values()])
        except Exception:
            pass

    def restore(self) -> int:
        """Reload the open book after a restart (positions snapshot + today's realised P&L)."""
        rows = self.t.db.load_positions()
        n = 0
        for row in rows:
            try:
                pos = Position(**row)
            except Exception:
                continue
            if pos.net_qty != 0:
                self.positions[pos.symbol] = pos
                n += 1
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        today = self.t.db.trades(limit=5000, since=day_start)
        self.realized_today = round(sum(float(t["pnl"]) for t in today), 2)
        self.charges_today = round(sum(float(t.get("charges") or 0) for t in today), 2)
        return n

    def adopt(self, symbol: str, underlying: str, exchange: Exchange, expiry: str, strike: float, option_type: OptionType, lot_size: int, net_qty: int, avg_price: float) -> Position:
        """Create/overwrite a position from the broker book (reconciliation with adopt=true)."""
        pos = Position(symbol=symbol, underlying=underlying, exchange=exchange, expiry=expiry, strike=strike, option_type=option_type, lot_size=lot_size, net_qty=net_qty, avg_price=avg_price, ltp=avg_price)
        if net_qty == 0:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = pos
        self._persist()
        return pos

    def mark(self, quote_lookup) -> None:
        for pos in self.positions.values():
            q: OptionQuote | None = quote_lookup(pos.symbol)
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
