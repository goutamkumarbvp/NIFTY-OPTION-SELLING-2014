"""Instrument master: canonical instruments, lot sizes with effective dates, and per-broker references.

Lot sizes and strike steps change over time (exchange circulars). The table
below holds known defaults with an `effective_from` date; the broker's own
instrument master overrides it at runtime when loaded. Unknown underlyings are
rejected rather than guessed.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from amrt.core.enums import Exchange, InstrumentKind, OptionType, Segment
from amrt.marketdata.models import Instrument


@dataclass(frozen=True)
class UnderlyingSpec:
    symbol: str
    exchange: Exchange
    option_segment: Segment
    strike_step: float
    lot_sizes: tuple[tuple[str, int], ...]   # (effective_from ISO date, lot size) ascending
    session: tuple[str, str]
    tick_size: float = 0.05

    def lot_size_on(self, day: dt.date) -> int:
        size = self.lot_sizes[0][1]
        for eff, lot in self.lot_sizes:
            if dt.date.fromisoformat(eff) <= day:
                size = lot
        return size


# Defaults only; verify against the exchange circular / broker master before live use.
UNDERLYINGS: dict[str, UnderlyingSpec] = {
    "NIFTY": UnderlyingSpec("NIFTY", Exchange.NSE, Segment.NSE_FO, 50.0, (("2021-01-01", 50), ("2024-11-20", 75), ("2025-12-30", 65)), ("09:15", "15:30")),
    "BANKNIFTY": UnderlyingSpec("BANKNIFTY", Exchange.NSE, Segment.NSE_FO, 100.0, (("2021-01-01", 25), ("2024-11-20", 30), ("2025-12-30", 30)), ("09:15", "15:30")),
    "SENSEX": UnderlyingSpec("SENSEX", Exchange.BSE, Segment.BSE_FO, 100.0, (("2023-05-15", 10), ("2024-11-20", 20)), ("09:15", "15:30")),
    "CRUDEOIL": UnderlyingSpec("CRUDEOIL", Exchange.MCX, Segment.MCX_FO, 50.0, (("2021-01-01", 100),), ("09:00", "23:30"), tick_size=0.1),
    "NATURALGAS": UnderlyingSpec("NATURALGAS", Exchange.MCX, Segment.MCX_FO, 5.0, (("2021-01-01", 1250),), ("09:00", "23:30"), tick_size=0.1),
    "GOLD": UnderlyingSpec("GOLD", Exchange.MCX, Segment.MCX_FO, 100.0, (("2021-01-01", 1),), ("09:00", "23:30"), tick_size=0.5),
    "SILVER": UnderlyingSpec("SILVER", Exchange.MCX, Segment.MCX_FO, 250.0, (("2021-01-01", 1),), ("09:00", "23:30"), tick_size=1.0),
}


def spec(symbol: str) -> UnderlyingSpec:
    try:
        return UNDERLYINGS[symbol.upper()]
    except KeyError:
        raise KeyError(f"UNKNOWN_UNDERLYING:{symbol}") from None


class InstrumentMaster:
    def __init__(self) -> None:
        self.instruments: dict[str, Instrument] = {}
        self.lot_override: dict[str, int] = {}
        self.loaded_from: dict[str, str] = {}

    def underlying(self, symbol: str) -> Instrument:
        s = spec(symbol)
        key = Instrument.underlying_key(s.exchange, s.symbol)
        inst = self.instruments.get(key)
        if inst is None:
            seg = Segment.NSE_INDEX if s.exchange == Exchange.NSE else (Segment.BSE_INDEX if s.exchange == Exchange.BSE else Segment.MCX_FO)
            kind = InstrumentKind.INDEX if s.exchange != Exchange.MCX else InstrumentKind.FUTURE
            inst = Instrument(key=key, exchange=s.exchange, segment=seg, kind=kind, underlying=s.symbol, lot_size=1, tick_size=s.tick_size, trading_symbol=s.symbol)
            self.instruments[key] = inst
        return inst

    def option(self, symbol: str, expiry: dt.date, strike: float, ot: OptionType | str, on: dt.date | None = None) -> Instrument:
        s = spec(symbol)
        key = Instrument.option_key(s.exchange, s.symbol, expiry, strike, ot)
        inst = self.instruments.get(key)
        if inst is None:
            lot = self.lot_override.get(s.symbol) or s.lot_size_on(on or expiry)
            inst = Instrument(key=key, exchange=s.exchange, segment=s.option_segment, kind=InstrumentKind.OPTION, underlying=s.symbol, expiry=expiry,
                              strike=float(strike), option_type=OptionType(ot), lot_size=lot, tick_size=s.tick_size,
                              trading_symbol=f"{s.symbol}{expiry.strftime('%d%b%y').upper()}{float(strike):g}{OptionType(ot).value}")
            self.instruments[key] = inst
        return inst

    def add(self, inst: Instrument, source: str) -> None:
        self.instruments[inst.key] = inst
        self.loaded_from[inst.key] = source

    def set_broker_ref(self, key: str, broker: str, ref: dict[str, str]) -> Instrument:
        inst = self.instruments[key]
        refs = dict(inst.broker_refs)
        refs[broker] = ref
        new = inst.model_copy(update={"broker_refs": refs})
        self.instruments[key] = new
        return new

    def get(self, key: str) -> Instrument:
        try:
            return self.instruments[key]
        except KeyError:
            raise KeyError(f"UNKNOWN_INSTRUMENT:{key}") from None

    @staticmethod
    def parse_key(key: str) -> dict:
        parts = key.split(":")
        if len(parts) == 3:
            return {"exchange": parts[0], "underlying": parts[1], "kind": parts[2]}
        if len(parts) == 5:
            return {"exchange": parts[0], "underlying": parts[1], "expiry": dt.date.fromisoformat(parts[2]), "strike": float(parts[3]), "option_type": parts[4]}
        raise ValueError(f"bad instrument key {key}")
