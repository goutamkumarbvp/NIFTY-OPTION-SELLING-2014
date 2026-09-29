"""Tradeable underlyings across NSE, BSE and MCX.

Expiry conventions (as of 2026): NIFTY weekly on Tuesday, SENSEX weekly on
Thursday; BANKNIFTY / FINNIFTY / MIDCPNIFTY / BANKEX monthly on the exchange's
weekday; MCX commodity options monthly. Everything is overridable through
``runtime/universe.json``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from terminal.core.models import Exchange, Underlying

_DEFAULT: List[Underlying] = [
    Underlying(symbol="NIFTY", exchange=Exchange.NSE, name="NIFTY 50", lot_size=75, strike_step=50, expiry_weekday=1, weekly=True, base_spot=24800.0, base_vol=0.13),
    Underlying(symbol="BANKNIFTY", exchange=Exchange.NSE, name="NIFTY BANK", lot_size=35, strike_step=100, expiry_weekday=1, weekly=False, base_spot=55600.0, base_vol=0.16),
    Underlying(symbol="FINNIFTY", exchange=Exchange.NSE, name="NIFTY FIN SERVICE", lot_size=65, strike_step=50, expiry_weekday=1, weekly=False, base_spot=26400.0, base_vol=0.15),
    Underlying(symbol="MIDCPNIFTY", exchange=Exchange.NSE, name="NIFTY MIDCAP SELECT", lot_size=140, strike_step=25, expiry_weekday=1, weekly=False, base_spot=13100.0, base_vol=0.18),
    Underlying(symbol="SENSEX", exchange=Exchange.BSE, name="S&P BSE SENSEX", lot_size=20, strike_step=100, expiry_weekday=3, weekly=True, base_spot=81200.0, base_vol=0.13),
    Underlying(symbol="BANKEX", exchange=Exchange.BSE, name="S&P BSE BANKEX", lot_size=30, strike_step=100, expiry_weekday=3, weekly=False, base_spot=62100.0, base_vol=0.16),
    Underlying(symbol="CRUDEOIL", exchange=Exchange.MCX, name="MCX CRUDE OIL", lot_size=100, strike_step=50, expiry_weekday=1, weekly=False, base_spot=5650.0, base_vol=0.32, session_open="09:00", session_close="23:30"),
    Underlying(symbol="NATURALGAS", exchange=Exchange.MCX, name="MCX NATURAL GAS", lot_size=1250, strike_step=5, expiry_weekday=1, weekly=False, base_spot=262.0, base_vol=0.45, session_open="09:00", session_close="23:30"),
    Underlying(symbol="GOLD", exchange=Exchange.MCX, name="MCX GOLD", lot_size=100, strike_step=100, expiry_weekday=3, weekly=False, base_spot=103500.0, base_vol=0.14, session_open="09:00", session_close="23:30"),
    Underlying(symbol="SILVER", exchange=Exchange.MCX, name="MCX SILVER", lot_size=30, strike_step=250, expiry_weekday=3, weekly=False, base_spot=118000.0, base_vol=0.22, session_open="09:00", session_close="23:30"),
]

VIX_SYMBOL = "INDIAVIX"


class Universe:
    def __init__(self, runtime_dir: Path, markets: List[str]) -> None:
        self._items: Dict[str, Underlying] = {}
        override = runtime_dir / "universe.json"
        source = _DEFAULT
        if override.exists():
            try:
                source = [Underlying(**row) for row in json.loads(override.read_text(encoding="utf-8"))]
            except Exception:
                source = _DEFAULT
        for u in source:
            if u.exchange.value in markets:
                self._items[u.symbol] = u

    def get(self, symbol: str) -> Underlying:
        return self._items[symbol.upper()]

    def has(self, symbol: str) -> bool:
        return symbol.upper() in self._items

    def all(self) -> List[Underlying]:
        return list(self._items.values())

    def by_exchange(self, exchange: Exchange) -> List[Underlying]:
        return [u for u in self._items.values() if u.exchange == exchange]
