"""AT-14 numerics: PCR / OI analytics, FII/DII, pricing, charges, P&L, backtest — hand-checked values."""
import datetime as dt
import math

import pytest

from amrt.analytics.fii_dii import ParseError, aggregate_cash, net_positions, parse_fii_dii_cash, parse_participant_oi, participant_series
from amrt.analytics.option_chain import analyze_chain, observe_windows
from amrt.analytics.pricing import bs_greeks, bs_price, implied_vol
from amrt.core.clock import IST, ManualClock
from amrt.core.enums import DataLabel, Exchange, Side
from amrt.marketdata.models import ChainRow, ChainSnapshot, OptionLeg
from amrt.portfolio.ledger import Ledger
from amrt.quant.costs import option_charges, schedule_on

pytestmark = [pytest.mark.quant, pytest.mark.acceptance]
EXP = dt.date(2026, 10, 6)


def snap(oi: dict, spot=25000.0, ts=1000.0, chg: dict | None = None, label=DataLabel.LIVE_UNVERIFIED) -> ChainSnapshot:
    rows = []
    for k in sorted({k for k, _ in oi}):
        legs = {}
        for side in ("CE", "PE"):
            if (k, side) in oi:
                legs[side.lower()] = OptionLeg(instrument_key=f"NSE:NIFTY:{EXP}:{k:g}:{side}", ltp=10.0, oi=oi[(k, side)],
                                               oi_change=(chg or {}).get((k, side)))
        rows.append(ChainRow(strike=k, **legs))
    return ChainSnapshot(underlying="NIFTY", exchange=Exchange.NSE, expiry=EXP, spot=spot, ts=ts, source="t", label=label, rows=rows, strike_step=50)


def full(ce=100, pe=100, n=10, centre=25000):
    d = {}
    for i in range(-n, n + 1):
        k = centre + 50 * i
        d[(k, "CE")], d[(k, "PE")] = ce, pe
    return d


def test_pcr_total_and_change():
    oi = full(ce=100, pe=150)
    chg = {k: (10 if k[1] == "CE" else 30) for k in oi}
    a = analyze_chain(snap(oi, chg=chg))
    assert a.status == "OK" and a.atm_strike == 25000
    assert a.pcr_total_oi.value == 1.5 and a.pcr_total_oi.numerator == 150 * 21
    assert a.pcr_change_oi.value == 3.0 and a.pcr_change_oi.interpretation == "both sides net building"


def test_pcr_zero_and_negative_denominators():
    oi = full()
    a = analyze_chain(snap(oi, chg={k: (0 if k[1] == "CE" else 5) for k in oi}))
    assert a.pcr_change_oi.value is None and "zero" in a.pcr_change_oi.note
    b = analyze_chain(snap(oi, chg={k: (-10 if k[1] == "CE" else 5) for k in oi}))
    assert b.pcr_change_oi.value == -0.5 and "negative denominator" in b.pcr_change_oi.interpretation


def test_max_and_second_oi_with_tie_breaks():
    oi = full()
    oi[(25200, "CE")] = 900
    oi[(24800, "CE")] = 900             # tie: same distance from ATM → lower strike wins
    oi[(25100, "CE")] = 500
    oi[(24900, "PE")] = 700
    a = analyze_chain(snap(oi))
    assert (a.ce_max_oi.strike, a.ce_second_oi.strike) == (24800, 25200)
    assert a.pe_max_oi.strike == 24900 and a.pe_max_oi.value == 700


def test_buildup_unwinding_and_coverage():
    oi = full()
    chg = {k: 0 for k in oi}
    chg[(25150, "PE")] = 5000
    chg[(24850, "CE")] = -4000
    a = analyze_chain(snap(oi, chg=chg))
    assert (a.max_buildup.strike, a.max_buildup.side, a.max_buildup.value) == (25150, "PE", 5000)
    assert (a.max_unwinding.strike, a.max_unwinding.side, a.max_unwinding.value) == (24850, "CE", -4000)
    sparse = {k: v for k, v in full().items() if k[0] <= 24700 or k[0] >= 25050}   # remove strikes around ATM
    s = analyze_chain(snap(sparse))
    assert s.status in ("INCOMPLETE", "DATA UNAVAILABLE") and s.missing_strikes


def test_unavailable_chain_has_no_numbers():
    a = analyze_chain(ChainSnapshot(underlying="NIFTY", exchange=Exchange.NSE, expiry=EXP, spot=None, ts=1, source="t", label=DataLabel.UNAVAILABLE, rows=[]))
    assert a.status == "DATA UNAVAILABLE" and a.pcr_total_oi is None and a.ce_max_oi is None


def test_window_observations_require_history():
    s0 = snap(full(ce=100, pe=100), ts=1000.0)
    s1 = snap(full(ce=110, pe=130), ts=1000.0 + 300)
    w = observe_windows([s0, s1], minutes=(1, 5))
    assert w["1m"]["status"] == "INSUFFICIENT HISTORY"
    assert w["5m"]["status"] == "OK" and w["5m"]["ce_oi_change"] == 10 * 21 and w["5m"]["pe_oi_change"] == 30 * 21


# ------------------------------------------------------------------ pricing & costs
def test_black_scholes_parity_and_iv_roundtrip():
    s, k, t, v, r = 25000, 25100, 7 / 365, 0.13, 0.065
    c, p = bs_price(s, k, t, v, True, r), bs_price(s, k, t, v, False, r)
    assert math.isclose(c - p, s - k * math.exp(-r * t), rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(implied_vol(c, s, k, t, True, r), v, abs_tol=1e-4)
    g = bs_greeks(s, k, t, v, True, r)
    assert 0 < g["delta"] < 1 and g["gamma"] > 0 and g["theta"] < 0
    assert implied_vol(0.0, s, k, t, True) is None


def test_charges_follow_effective_dates():
    assert schedule_on(dt.date(2024, 9, 30)).stt_sell_option_pct == 0.0625
    assert schedule_on(dt.date(2024, 10, 1)).stt_sell_option_pct == 0.1
    sell = option_charges(Exchange.NSE, Side.SELL, 100_000, dt.date(2026, 1, 5))
    buy = option_charges(Exchange.NSE, Side.BUY, 100_000, dt.date(2026, 1, 5))
    assert sell["stt"] == 100.0 and buy["stt"] == 0.0 and buy["stamp"] > 0 and sell["stamp"] == 0
    mcx = option_charges(Exchange.MCX, Side.SELL, 100_000, dt.date(2026, 1, 5))
    assert mcx["stt"] == 0 and mcx["ctt"] > 0


# ------------------------------------------------------------------ ledger P&L
def test_ledger_realized_unrealized_and_missing_marks():
    clock = ManualClock(dt.datetime(2026, 10, 5, 10, 0, tzinfo=IST))
    L = Ledger(None, clock)
    k = "NSE:NIFTY:2026-10-06:25000:CE"
    L.apply_fill("A", k, Side.SELL, 130, 100.0, 20.0, 65, simulated=True)
    L.apply_fill("A", k, Side.BUY, 65, 80.0, 10.0, 65, simulated=True)
    v = L.valuation("A", lambda key: 90.0, lambda key: 25000.0)
    assert v["realized_today"] == 65 * 20.0 and v["unrealized"] == round((90 - 100) * -65, 2)
    assert v["net_pnl_today"] == round(1300 + 650 - 30, 2)
    m = L.valuation("A", lambda key: None, lambda key: 25000.0)
    assert m["unrealized"] is None and m["net_pnl_today"] is None and m["marks_missing"] == [k]
    L.apply_fill("A", k, Side.BUY, 130, 70.0, 0, 65, simulated=True)       # flip through zero
    p = L.position("A", k)
    assert p.net_qty == 65 and p.avg_price == 70.0


# ------------------------------------------------------------------ FII / DII
PART = """Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 03, 2026
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts,Total Short Contracts
Client,100,50,10,10,10,10,10,10,1,1,1,1,142,92
DII,10,20,10,10,0,0,0,0,0,0,0,0,20,30
FII,40,80,10,10,5,5,5,5,0,0,0,0,60,100
Pro,10,10,10,10,5,5,5,5,0,0,0,0,30,30
TOTAL,160,160,40,40,20,20,20,20,1,1,1,1,252,252
"""


def test_participant_oi_parse_and_nets():
    recs = parse_participant_oi(PART)
    assert recs[0]["date"] == "2026-10-03" and len(recs) == 5
    fii = next(r for r in recs if r["client_type"] == "FII")
    n = net_positions(fii["figures"])
    assert n["index_futures_net"] == -40 and n["index_futures_long_pct"] == round(100 * 40 / 120, 2)
    bad = PART.replace("FII,40,80", "FII,41,80")
    with pytest.raises(ParseError):
        parse_participant_oi(bad)
    day2 = parse_participant_oi(PART.replace("Oct 03, 2026", "Oct 05, 2026").replace("FII,40,80", "FII,50,80").replace("Client,100,50", "Client,90,50"))
    s = participant_series(recs + day2, "FII")
    assert s[-1]["change_vs_previous"]["index_futures_net"] == 10


def test_cash_parse_and_aggregation_completeness():
    raw = "Category,Date,Buy Value,Sell Value,Net Value\nFII/FPI,01-Oct-2026,1000,1200,-200\nDII,01-Oct-2026,900,700,200\nFII/FPI,02-Oct-2026,800,500,300\n"
    recs = parse_fii_dii_cash(raw)
    assert {r["category"] for r in recs} == {"FII", "DII"}
    with pytest.raises(ParseError):
        parse_fii_dii_cash(raw.replace("-200", "-150"))
    d = aggregate_cash(recs, "FII", "D")
    assert [r["net_value"] for r in d] == [-200, 300]
    w = aggregate_cash(recs, "FII", "W")
    assert w[0]["net_value"] == 100 and w[0]["status"] == "INCOMPLETE"      # 2 of 5 trading days
