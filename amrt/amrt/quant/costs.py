"""Indian exchange-traded option transaction costs with effective dates.

Rates change by regulation; the schedule is versioned and every computed cost
records which schedule it used. Defaults below must be checked against the
broker's current tariff and exchange circulars before relying on them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from amrt.core.enums import Exchange, Side


@dataclass(frozen=True)
class ChargeSchedule:
    effective_from: str
    brokerage_per_order: float = 20.0
    stt_sell_option_pct: float = 0.1            # on option premium, sell side (NSE/BSE)
    ctt_sell_option_pct_mcx: float = 0.05       # commodity transaction tax, sell side (MCX)
    exchange_txn_pct: dict = field(default_factory=lambda: {"NSE": 0.03503, "BSE": 0.0325, "MCX": 0.0418})
    sebi_per_crore: float = 10.0
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0
    verified: bool = False


SCHEDULES: list[ChargeSchedule] = [
    ChargeSchedule(effective_from="2023-04-01", stt_sell_option_pct=0.0625),
    ChargeSchedule(effective_from="2024-10-01", stt_sell_option_pct=0.1),
]


def schedule_on(day: dt.date) -> ChargeSchedule:
    s = SCHEDULES[0]
    for c in SCHEDULES:
        if dt.date.fromisoformat(c.effective_from) <= day:
            s = c
    return s


def option_charges(exchange: Exchange | str, side: Side | str, premium_value: float, day: dt.date, brokerage: float | None = None) -> dict:
    sch = schedule_on(day)
    ex = Exchange(exchange).value
    side = Side(side)
    br = sch.brokerage_per_order if brokerage is None else brokerage
    txn = premium_value * sch.exchange_txn_pct[ex] / 100.0
    stt = premium_value * sch.stt_sell_option_pct / 100.0 if side == Side.SELL and ex != "MCX" else 0.0
    ctt = premium_value * sch.ctt_sell_option_pct_mcx / 100.0 if side == Side.SELL and ex == "MCX" else 0.0
    sebi = premium_value * sch.sebi_per_crore / 1e7
    stamp = premium_value * sch.stamp_buy_pct / 100.0 if side == Side.BUY else 0.0
    gst = (br + txn + sebi) * sch.gst_pct / 100.0
    total = br + txn + stt + ctt + sebi + stamp + gst
    return {"brokerage": round(br, 2), "exchange_txn": round(txn, 2), "stt": round(stt, 2), "ctt": round(ctt, 2), "sebi": round(sebi, 4),
            "stamp": round(stamp, 2), "gst": round(gst, 2), "total": round(total, 2), "schedule": sch.effective_from, "schedule_verified": sch.verified}
