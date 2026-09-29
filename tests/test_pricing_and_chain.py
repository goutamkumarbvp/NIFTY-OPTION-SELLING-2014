import datetime as dt

import pytest

from terminal.core.clock import expiry_series, last_weekday_of_month, next_weekday_expiry
from terminal.market.chain import OptionChainBuilder
from terminal.market.pricing import bs_greeks, bs_price, implied_vol
from terminal.market.universe import Universe
from terminal.core.models import OptionType


def test_black_scholes_put_call_parity():
    s, k, t, v, r = 24800, 24800, 7 / 365, 0.13, 0.065
    c, p = bs_price(s, k, t, v, True, r), bs_price(s, k, t, v, False, r)
    assert abs((c - p) - (s - k * pow(2.718281828, -r * t))) < 0.05


def test_greeks_are_sane():
    g = bs_greeks(24800, 24800, 7 / 365, 0.13, True)
    assert 0.45 < g["delta"] < 0.6
    assert g["gamma"] > 0 and g["vega"] > 0 and g["theta"] < 0


def test_implied_vol_roundtrip():
    px = bs_price(24800, 25000, 5 / 365, 0.16, True)
    assert abs(implied_vol(px, 24800, 25000, 5 / 365, True) - 0.16) < 1e-3


def test_expiry_calendar():
    tue = next_weekday_expiry(dt.date(2026, 9, 30), 1)
    assert tue.weekday() in (0, 1)  # Tuesday, or Monday if holiday-shifted
    assert tue >= dt.date(2026, 9, 30)
    monthly = last_weekday_of_month(2026, 10, 1)
    assert monthly.month == 10 and monthly.weekday() <= 1
    assert len(expiry_series(dt.date(2026, 9, 30), 3, True, 4)) == 4


def test_chain_structure(tmp_path):
    u = Universe(tmp_path, ["NSE"]).get("NIFTY")
    ch = OptionChainBuilder(seed=1).build(u, 24800, 13.0, "2026-10-06")
    assert ch.atm_strike == 24800
    assert len(ch.rows) == 31
    atm = ch.find(24800, OptionType.CE)
    assert atm is not None and 0.4 < atm.delta < 0.6
    assert ch.pcr > 0 and ch.total_ce_oi > 0
    # deltas monotonic: further OTM call has smaller delta
    assert ch.find(25200, OptionType.CE).delta < atm.delta
    q = ch.by_delta(OptionType.PE, 0.15)
    assert q is not None and abs(abs(q.delta) - 0.15) < 0.08
    assert ch.max_pain in {r.strike for r in ch.rows}
