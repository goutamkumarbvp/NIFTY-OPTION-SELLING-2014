"""Historical replay source. Everything it emits is labelled HISTORICAL REPLAY and can never satisfy a live-data rule.

CSV columns: ts (epoch seconds or ISO-8601), instrument_key, ltp, bid, ask, volume, oi
"""
from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Iterator
from pathlib import Path

from amrt.core.enums import DataLabel
from amrt.marketdata.models import Quote


def _ts(v: str) -> float:
    try:
        return float(v)
    except ValueError:
        return dt.datetime.fromisoformat(v).timestamp()


def _num(v: str | None):
    if v in (None, ""):
        return None
    return float(v)


def read_replay(path: str | Path) -> Iterator[Quote]:
    p = Path(path)
    with p.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            ts = _ts(row["ts"])
            yield Quote(instrument_key=row["instrument_key"], ltp=float(row["ltp"]), bid=_num(row.get("bid")), ask=_num(row.get("ask")),
                        volume=int(float(row["volume"])) if row.get("volume") else None, oi=int(float(row["oi"])) if row.get("oi") else None,
                        exchange_ts=ts, recv_ts=ts, seq=i, source=f"replay:{p.name}", label=DataLabel.HISTORICAL_REPLAY)
