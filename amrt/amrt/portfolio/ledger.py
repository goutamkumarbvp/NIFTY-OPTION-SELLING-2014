"""Portfolio accounting: positions from fills, realised/unrealised P&L, exposure.

* Quantities are in units (lots × lot size), signed: + long, − short.
* Realised P&L is booked when a fill reduces or flips a position.
* Unrealised P&L uses a fresh mark (mid, else LTP). If ANY open position lacks a
  fresh mark, the portfolio's unrealised P&L is `None` (DATA UNAVAILABLE) — it is
  never estimated — and the Risk Kernel treats that as missing evidence.
* Paper (SIMULATED) and live accounts are separate ledgers; nothing moves between them.
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import asdict, dataclass

from sqlalchemy import delete, select

from amrt.core.enums import Side
from amrt.storage.db import Database, positions


@dataclass
class Position:
    account_id: str
    instrument_key: str
    net_qty: int
    avg_price: float
    realized_pnl: float
    charges: float
    lot_size: int
    updated_at: float
    simulated: bool

    def to_dict(self) -> dict:
        return asdict(self)


class Ledger:
    def __init__(self, db: Database | None, clock) -> None:
        self.db = db
        self.clock = clock
        self.positions: dict[tuple[str, str], Position] = {}
        self.realized_today: dict[str, float] = {}
        self.charges_today: dict[str, float] = {}
        self._day: dict[str, dt.date] = {}
        self._lock = threading.RLock()
        if db is not None:
            self._load()

    def _load(self) -> None:
        with self.db.engine.connect() as conn:
            for r in conn.execute(select(positions)):
                p = Position(**{k: r._mapping[k] for k in ("account_id", "instrument_key", "net_qty", "avg_price", "realized_pnl", "charges", "lot_size", "updated_at", "simulated")})
                self.positions[(p.account_id, p.instrument_key)] = p

    def _persist(self, p: Position) -> None:
        if self.db is None:
            return
        with self.db.tx() as conn:
            conn.execute(delete(positions).where(positions.c.account_id == p.account_id, positions.c.instrument_key == p.instrument_key))
            conn.execute(positions.insert().values(**p.to_dict()))

    def _roll(self, account: str) -> None:
        today = self.clock.ist().date()
        if self._day.get(account) != today:
            self._day[account] = today
            self.realized_today[account] = 0.0
            self.charges_today[account] = 0.0

    def apply_fill(self, account_id: str, instrument_key: str, side: Side, qty: int, price: float, charges: float, lot_size: int, simulated: bool) -> Position:
        if qty <= 0 or price < 0:
            raise ValueError("fill quantity must be positive and price non-negative")
        with self._lock:
            self._roll(account_id)
            key = (account_id, instrument_key)
            p = self.positions.get(key) or Position(account_id, instrument_key, 0, 0.0, 0.0, 0.0, lot_size, self.clock.ts(), simulated)
            signed = qty if side == Side.BUY else -qty
            realized = 0.0
            if p.net_qty == 0 or (p.net_qty > 0) == (signed > 0):
                total = abs(p.net_qty) + qty
                p.avg_price = (abs(p.net_qty) * p.avg_price + qty * price) / total
                p.net_qty += signed
            else:
                closing = min(abs(p.net_qty), qty)
                realized = closing * (price - p.avg_price) * (1 if p.net_qty > 0 else -1)
                p.net_qty += signed
                if p.net_qty == 0:
                    p.avg_price = 0.0
                elif (p.net_qty > 0) == (signed > 0):   # flipped through zero
                    p.avg_price = price
            p.realized_pnl += realized
            p.charges += charges
            p.updated_at = self.clock.ts()
            self.realized_today[account_id] = self.realized_today.get(account_id, 0.0) + realized
            self.charges_today[account_id] = self.charges_today.get(account_id, 0.0) + charges
            self.positions[key] = p
            self._persist(p)
            return p

    def adopt(self, account_id: str, instrument_key: str, net_qty: int, avg_price: float, lot_size: int, simulated: bool) -> Position:
        """Reconciliation: make the ledger match the broker (the source of truth for live accounts)."""
        with self._lock:
            key = (account_id, instrument_key)
            p = self.positions.get(key) or Position(account_id, instrument_key, 0, 0.0, 0.0, 0.0, lot_size, self.clock.ts(), simulated)
            p.net_qty, p.avg_price, p.updated_at = int(net_qty), float(avg_price), self.clock.ts()
            self.positions[key] = p
            self._persist(p)
            return p

    def open_positions(self, account_id: str | None = None) -> list[Position]:
        return [p for (a, _), p in self.positions.items() if p.net_qty != 0 and (account_id is None or a == account_id)]

    def position(self, account_id: str, instrument_key: str) -> Position | None:
        return self.positions.get((account_id, instrument_key))

    def valuation(self, account_id: str, mark_fn, spot_fn) -> dict:
        """mark_fn(instrument_key) -> float | None (fresh mark); spot_fn(instrument_key) -> float | None (underlying spot)."""
        self._roll(account_id)
        rows, unrealized, missing, gross_notional, premium_exposure = [], 0.0, [], 0.0, 0.0
        notional_known = True
        for p in self.open_positions(account_id):
            mark = mark_fn(p.instrument_key)
            spot = spot_fn(p.instrument_key)
            u = None if mark is None else round((mark - p.avg_price) * p.net_qty, 2)
            if u is None:
                missing.append(p.instrument_key)
            else:
                unrealized += u
                premium_exposure += abs(p.net_qty) * mark
            if spot is None:
                notional_known = False
            else:
                gross_notional += abs(p.net_qty) * spot
            rows.append({**p.to_dict(), "mark": mark, "unrealized_pnl": u, "lots": abs(p.net_qty) // max(1, p.lot_size)})
        realized = round(self.realized_today.get(account_id, 0.0), 2)
        charges = round(self.charges_today.get(account_id, 0.0), 2)
        unreal = None if missing else round(unrealized, 2)
        return {"account_id": account_id, "positions": rows, "realized_today": realized, "charges_today": charges,
                "unrealized": unreal, "net_pnl_today": None if unreal is None else round(realized + unreal - charges, 2),
                "marks_missing": missing, "gross_notional": round(gross_notional, 2) if notional_known else None,
                "premium_exposure": round(premium_exposure, 2) if not missing else None,
                "open_lots": sum(abs(p.net_qty) // max(1, p.lot_size) for p in self.open_positions(account_id))}
