"""Market data schemas with provenance. A value without source, timestamps and label is never shown as live."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, model_validator

from amrt.core.enums import DataLabel, Exchange, InstrumentKind, OptionType, Segment

SCHEMA_VERSION = "md/1"


class Instrument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    exchange: Exchange
    segment: Segment
    kind: InstrumentKind
    underlying: str
    expiry: dt.date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    lot_size: int = 1
    tick_size: float = 0.05
    trading_symbol: str = ""
    broker_refs: dict[str, dict[str, str]] = Field(default_factory=dict)

    @staticmethod
    def option_key(exchange: Exchange | str, underlying: str, expiry: dt.date, strike: float, ot: OptionType | str) -> str:
        return f"{Exchange(exchange).value}:{underlying.upper()}:{expiry.isoformat()}:{float(strike):g}:{OptionType(ot).value}"

    @staticmethod
    def underlying_key(exchange: Exchange | str, underlying: str) -> str:
        return f"{Exchange(exchange).value}:{underlying.upper()}:SPOT"


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_key: str
    ltp: float
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    oi: int | None = None
    exchange_ts: float | None = None   # time stamped by the exchange / broker, if provided
    recv_ts: float                     # time this process received it
    seq: int | None = None
    source: str                        # e.g. "kotak", "zerodha", "replay:file.csv"
    label: DataLabel
    schema_version: str = SCHEMA_VERSION

    @model_validator(mode="after")
    def _check(self) -> Quote:
        if not (self.ltp > 0):
            raise ValueError("ltp must be positive")
        if self.bid is not None and self.bid < 0 or self.ask is not None and self.ask < 0:
            raise ValueError("negative bid/ask")
        return self

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.ask >= self.bid:
            return (self.bid + self.ask) / 2
        return None


class OptionLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_key: str
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    oi: int | None = None
    oi_change: int | None = None
    iv: float | None = None        # percent, as published by the source
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


class ChainRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strike: float
    ce: OptionLeg | None = None
    pe: OptionLeg | None = None


class ChainSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    underlying: str
    exchange: Exchange
    expiry: dt.date
    spot: float | None
    spot_ts: float | None = None
    ts: float                  # snapshot receive time
    exchange_ts: float | None = None
    source: str
    label: DataLabel
    rows: list[ChainRow]
    strike_step: float | None = None
    schema_version: str = SCHEMA_VERSION

    def sorted_rows(self) -> list[ChainRow]:
        return sorted(self.rows, key=lambda r: r.strike)


class Freshness(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_key: str
    age_ms: float | None
    max_age_ms: float
    fresh: bool
    label: DataLabel
    source: str | None = None
    reason: str = ""
