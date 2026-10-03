"""Backtester: hand-computable P&L, next-snapshot fills (no look-ahead), labels, walk-forward separation."""
import datetime as dt

import pytest

from amrt.core.clock import IST
from amrt.core.enums import DataLabel, Exchange, Side
from amrt.marketdata.models import ChainRow, ChainSnapshot, OptionLeg
from amrt.quant.backtest import BTParams, backtest, stress, validation_summary, walk_forward
from amrt.quant.costs import option_charges
from amrt.quant.strategies import ROLLING_ATM_IRON_FLY, ROLLING_ATM_STRADDLE, build_entry, structure_metrics

pytestmark = [pytest.mark.quant, pytest.mark.acceptance]
EXP = dt.date(2026, 10, 6)


def chain(t: dt.datetime, prem: float, label=DataLabel.HISTORICAL_REPLAY, spot=25000.0) -> ChainSnapshot:
    rows = []
    for i in range(-6, 7):
        k = 25000 + 50 * i
        p = max(1.0, prem - 10 * abs(i))
        legs = {s: OptionLeg(instrument_key=f"NSE:NIFTY:{EXP}:{k}:{s.upper()}", ltp=p, bid=p, ask=p, oi=1000, iv=13.0) for s in ("ce", "pe")}
        rows.append(ChainRow(strike=k, **legs))
    return ChainSnapshot(underlying="NIFTY", exchange=Exchange.NSE, expiry=EXP, spot=spot, ts=t.timestamp(), source="t", label=label, rows=rows, strike_step=50)


def day(d: dt.date, start_prem: float, end_prem: float, label=DataLabel.HISTORICAL_REPLAY):
    out = []
    times = [(9, 15), (9, 20), (9, 25), (11, 0), (13, 0), (15, 5), (15, 10)]
    for j, (h, m) in enumerate(times):
        prem = start_prem if j <= 2 else end_prem
        out.append(chain(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST), prem, label))
    return out


def test_straddle_pnl_is_hand_computable():
    d = dt.date(2026, 10, 5)
    r = backtest(ROLLING_ATM_STRADDLE, day(d, 100.0, 60.0), BTParams(slippage_bps=0.0), lot_size=65)
    t = r["trades"][0]
    qty = 65
    gross = (100 - 60) * qty * 2
    ch = 2 * option_charges("NSE", Side.SELL, 100 * qty, d)["total"] + 2 * option_charges("NSE", Side.BUY, 60 * qty, d)["total"]
    assert t["gross_pnl"] == gross and t["charges"] == pytest.approx(ch, abs=0.02) and t["net_pnl"] == pytest.approx(gross - ch, abs=0.02)
    assert t["exit_reason"] == "TIME_EXIT" and r["leakage_checks"]["failed"] == []


def test_fill_is_at_next_snapshot_not_the_decision_snapshot():
    d = dt.date(2026, 10, 5)
    snaps = day(d, 100.0, 100.0)
    snaps[2] = chain(dt.datetime(2026, 10, 5, 9, 25, tzinfo=IST), 120.0)      # price changes after the 09:20 decision
    t = backtest(ROLLING_ATM_STRADDLE, snaps, BTParams(slippage_bps=0.0), lot_size=65)["trades"][0]
    assert all(leg["entry"] == 120.0 for leg in t["legs"])
    assert t["entry_ts"] == snaps[2].ts


def test_iron_fly_structural_max_loss():
    snap = chain(dt.datetime(2026, 10, 5, 9, 20, tzinfo=IST), 100.0)
    legs, why = build_entry(ROLLING_ATM_IRON_FLY, snap)
    assert why == "" and [x["side"] for x in legs] == ["BUY", "BUY", "SELL", "SELL"]
    m = structure_metrics(ROLLING_ATM_IRON_FLY, legs, 65)
    credit = 100 + 100 - 60 - 60
    assert m["net_credit_per_unit"] == credit and m["max_loss_inr"] == (200 - credit) * 65
    assert "not a guarantee" in m["max_loss_basis"]
    assert structure_metrics(ROLLING_ATM_STRADDLE, legs[2:], 65)["max_loss_inr"] is None


def test_missing_quote_means_no_trade():
    snaps = day(dt.date(2026, 10, 5), 100, 60)
    snaps[2] = snaps[2].model_copy(update={"rows": [r for r in snaps[2].rows if r.strike != 25000]})
    assert backtest(ROLLING_ATM_STRADDLE, snaps, lot_size=65)["days_traded"] == 0


def test_labels_and_validation_gate():
    sim = backtest(ROLLING_ATM_STRADDLE, day(dt.date(2026, 10, 5), 100, 60, DataLabel.SIMULATED), lot_size=65)
    assert "SIMULATED" in sim["label"]
    days = []
    d = dt.date(2026, 6, 1)
    while len(days) < 70 * 7:
        if d.weekday() < 5:
            days += day(d, 100, 60 + (d.toordinal() % 7) * 10)
        d += dt.timedelta(days=1)
    wf = walk_forward(ROLLING_ATM_STRADDLE, days, folds=3, grid={"wing_offset_strikes": [4], "stop_pct": [None, 50.0]})
    assert wf["status"] == "OK"
    for f in wf["folds"]:
        assert f["in_sample"][1] < f["out_of_sample"][0]           # selection never sees the evaluation period
    assert validation_summary(wf, "SIMULATED BACKTEST — not evidence") is None
    assert validation_summary(wf, wf["label"]) is None or validation_summary(wf, wf["label"])["oos_days"] >= 60


def test_stress_scenarios():
    s = stress(ROLLING_ATM_IRON_FLY, chain(dt.datetime(2026, 10, 5, 9, 20, tzinfo=IST), 100.0))
    assert s["status"] == "OK" and len(s["scenarios"]) == 8 and s["worst"]["pnl"] < 0
