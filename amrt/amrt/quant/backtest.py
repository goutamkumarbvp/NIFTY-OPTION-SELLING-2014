"""Backtesting for the rolling ATM structures (straddle / iron butterfly).

Method (docs/BACKTEST_METHODOLOGY.md):
* Input: chain snapshots with timestamps, labelled with their provenance. A result
  inherits the input label; SIMULATED input yields a SIMULATED backtest that is
  never used as evidence.
* Decisions at snapshot t use only snapshots ≤ t. Fills happen at the NEXT
  snapshot (fill_delay=1) at the touch — buy at ask, sell at bid — plus slippage;
  a missing quote at fill time skips the trade (no invented price).
* Entry at the first snapshot at/after entry_time inside the entry window; exit
  at the first snapshot at/after exit_time (else the day's last snapshot, flagged).
* Optional per-leg stop on short legs (premium up stop_pct %) checked on every snapshot.
* Costs: dated Indian F&O charge schedule (quant/costs.py) on every fill.
* Metrics: net P&L, win rate, profit factor, max drawdown, Sharpe/Sortino (daily,
  √252), 5 % CVaR, worst days. Past results do not predict future results.
"""
from __future__ import annotations

import datetime as dt
import itertools
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from amrt.analytics.option_chain import atm_strike
from amrt.analytics.pricing import bs_price
from amrt.core.clock import IST
from amrt.core.enums import DataLabel
from amrt.marketdata.instruments import spec as und_spec
from amrt.marketdata.models import ChainSnapshot, OptionLeg
from amrt.quant.costs import option_charges
from amrt.quant.strategies import StrategySpec, hhmm_minutes

BACKTEST_VERSION = "backtest/1.0"


@dataclass(frozen=True)
class BTParams:
    wing_offset_strikes: int = 4
    stop_pct: float | None = None          # short-leg stop: premium rises this % above entry
    slippage_bps: float = 10.0
    cost_multiplier: float = 1.0
    entry_shift_min: int = 0
    fill_delay: int = 1
    lots: int = 1


@dataclass
class Trade:
    day: str
    entry_ts: float
    exit_ts: float
    legs: list[dict]
    gross_pnl: float
    charges: float
    net_pnl: float
    exit_reason: str
    flags: list[str] = field(default_factory=list)


def _ist(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, IST)


def _mins(ts: float) -> int:
    t = _ist(ts)
    return t.hour * 60 + t.minute


def _leg(snap: ChainSnapshot, strike: float, ot: str) -> OptionLeg | None:
    for r in snap.rows:
        if r.strike == strike:
            return r.ce if ot == "CE" else r.pe
    return None


def _touch(leg: OptionLeg | None, side: str, slip_bps: float) -> float | None:
    """Executable price: buy at ask, sell at bid (ltp only if no two-sided quote), plus slippage against us."""
    if leg is None:
        return None
    if side == "BUY":
        base = leg.ask if leg.ask and leg.ask > 0 else leg.ltp
        return None if not base else base * (1 + slip_bps / 1e4)
    base = leg.bid if leg.bid and leg.bid > 0 else leg.ltp
    return None if not base else max(0.05, base * (1 - slip_bps / 1e4))


def _structure(strat: StrategySpec, snap: ChainSnapshot, p: BTParams) -> list[tuple[float, str, str]] | None:
    if snap.spot is None or not snap.rows:
        return None
    strikes = sorted(r.strike for r in snap.rows)
    atm = atm_strike(strikes, snap.spot)
    i = strikes.index(atm)
    legs = [(atm, "CE", "SELL"), (atm, "PE", "SELL")]
    if strat.structure == "IRON_BUTTERFLY":
        w = p.wing_offset_strikes
        if i - w < 0 or i + w >= len(strikes):
            return None
        legs = [(strikes[i + w], "CE", "BUY"), (strikes[i - w], "PE", "BUY")] + legs
    return legs


def run_day(strat: StrategySpec, snaps: list[ChainSnapshot], p: BTParams, lot_size: int, checks: list[dict]) -> Trade | None:
    snaps = sorted(snaps, key=lambda s: s.ts)
    if len(snaps) < 3:
        return None
    entry_m = hhmm_minutes(strat.entry_time) + p.entry_shift_min
    exit_m = hhmm_minutes(strat.exit_time)
    dec_idx = next((k for k, s in enumerate(snaps) if entry_m <= _mins(s.ts) < entry_m + strat.entry_window_minutes), None)
    if dec_idx is None:
        return None
    decision = snaps[dec_idx]
    structure = _structure(strat, decision, p)
    if structure is None:
        return None
    fill_idx = dec_idx + p.fill_delay
    if fill_idx >= len(snaps):
        return None
    fill = snaps[fill_idx]
    checks.append({"check": "no_lookahead_entry", "passed": fill.ts >= decision.ts and decision.ts <= fill.ts, "day": _ist(decision.ts).date().isoformat()})
    qty = p.lots * lot_size
    day = _ist(decision.ts).date()
    ex = und_spec(decision.underlying).exchange
    open_legs = []
    charges = 0.0
    for strike, ot, side in structure:
        px = _touch(_leg(fill, strike, ot), side, p.slippage_bps)
        if px is None:
            return None              # no executable quote at fill time: no trade, never an invented price
        charges += option_charges(ex, side, px * qty, day)["total"] * p.cost_multiplier
        open_legs.append({"strike": strike, "ot": ot, "side": side, "entry": px, "qty": qty, "open": True})
    reason, flags, exit_snap = "TIME_EXIT", [], None
    for k in range(fill_idx + 1, len(snaps)):
        s = snaps[k]
        if p.stop_pct is not None:
            for leg in open_legs:
                if leg["open"] and leg["side"] == "SELL":
                    cur = _leg(s, leg["strike"], leg["ot"])
                    mark = cur.ltp if cur is not None else None
                    if mark and mark >= leg["entry"] * (1 + p.stop_pct / 100):
                        nxt = snaps[min(k + p.fill_delay, len(snaps) - 1)]
                        px = _touch(_leg(nxt, leg["strike"], leg["ot"]), "BUY", p.slippage_bps)
                        if px is None:
                            flags.append(f"stop on {leg['strike']:g}{leg['ot']} could not fill (no quote); held")
                            continue
                        leg.update(open=False, exit=px, exit_ts=nxt.ts, exit_reason="LEG_STOP")
                        charges += option_charges(ex, "BUY", px * qty, day)["total"] * p.cost_multiplier
        if _mins(s.ts) >= exit_m:
            exit_snap = s
            break
    if exit_snap is None:
        exit_snap = snaps[-1]
        flags.append("no snapshot at exit time; closed at the day's last snapshot")
        reason = "LAST_SNAPSHOT"
    for leg in open_legs:
        if leg["open"]:
            close_side = "BUY" if leg["side"] == "SELL" else "SELL"
            px = _touch(_leg(exit_snap, leg["strike"], leg["ot"]), close_side, p.slippage_bps)
            if px is None:
                px = leg["entry"] if leg["side"] == "BUY" else leg["entry"] * 3   # conservative: longs worthless-ish, shorts at 3x
                flags.append(f"no exit quote for {leg['strike']:g}{leg['ot']}; conservative mark used")
            leg.update(open=False, exit=px, exit_ts=exit_snap.ts, exit_reason=reason)
            charges += option_charges(ex, close_side, px * qty, day)["total"] * p.cost_multiplier
    gross = sum((leg["entry"] - leg["exit"]) * leg["qty"] if leg["side"] == "SELL" else (leg["exit"] - leg["entry"]) * leg["qty"] for leg in open_legs)
    checks.append({"check": "exit_after_entry", "passed": all(leg["exit_ts"] >= fill.ts for leg in open_legs), "day": day.isoformat()})
    return Trade(day=day.isoformat(), entry_ts=fill.ts, exit_ts=exit_snap.ts, legs=open_legs, gross_pnl=round(gross, 2), charges=round(charges, 2),
                 net_pnl=round(gross - charges, 2), exit_reason=reason if not any(leg.get("exit_reason") == "LEG_STOP" for leg in open_legs) else "LEG_STOP",
                 flags=flags)


def metrics(daily: list[float]) -> dict[str, Any]:
    if not daily:
        return {"days": 0}
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in daily:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    wins = [x for x in daily if x > 0]
    losses = [x for x in daily if x < 0]
    mean = statistics.fmean(daily)
    sd = statistics.pstdev(daily) if len(daily) > 1 else 0.0
    dsd = math.sqrt(sum(x * x for x in losses) / len(daily)) if losses else 0.0
    tail = sorted(daily)[: max(1, len(daily) // 20)]
    return {"days": len(daily), "net_pnl": round(sum(daily), 2), "mean_daily": round(mean, 2), "win_rate": round(len(wins) / len(daily), 4),
            "avg_win": round(statistics.fmean(wins), 2) if wins else None, "avg_loss": round(statistics.fmean(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None, "max_drawdown": round(mdd, 2),
            "sharpe": round(mean / sd * math.sqrt(252), 3) if sd > 0 else None, "sortino": round(mean / dsd * math.sqrt(252), 3) if dsd > 0 else None,
            "cvar_5pct": round(statistics.fmean(tail), 2), "worst_day": round(min(daily), 2), "best_day": round(max(daily), 2)}


def group_days(snaps: list[ChainSnapshot]) -> dict[str, list[ChainSnapshot]]:
    days: dict[str, list[ChainSnapshot]] = defaultdict(list)
    for s in snaps:
        days[_ist(s.ts).date().isoformat()].append(s)
    return dict(sorted(days.items()))


def label_of(snaps: list[ChainSnapshot]) -> str:
    labels = {s.label for s in snaps}
    if DataLabel.SIMULATED in labels:
        return "SIMULATED BACKTEST — not evidence"
    if labels <= {DataLabel.LIVE_VERIFIED, DataLabel.LIVE_UNVERIFIED}:
        return "HISTORICAL BACKTEST (recorded live data)"
    return "HISTORICAL BACKTEST (replay data)"


def backtest(strat: StrategySpec, snaps: list[ChainSnapshot], p: BTParams | None = None, lot_size: int | None = None) -> dict:
    p = p or BTParams(wing_offset_strikes=strat.wing_offset_strikes)
    days = group_days(snaps)
    checks: list[dict] = []
    trades = []
    for day, ds in days.items():
        lot = lot_size or und_spec(ds[0].underlying).lot_size_on(dt.date.fromisoformat(day))
        t = run_day(strat, ds, p, lot, checks)
        if t is not None:
            trades.append(t)
    daily = [t.net_pnl for t in trades]
    return {"version": BACKTEST_VERSION, "label": label_of(snaps), "strategy": strat.model_dump(), "params": p.__dict__, "days_in_data": len(days),
            "days_traded": len(trades), "metrics": metrics(daily), "costs_total": round(sum(t.charges for t in trades), 2),
            "trades": [t.__dict__ for t in trades], "leakage_checks": {"total": len(checks), "failed": [c for c in checks if not c["passed"]]},
            "disclaimer": "Backtest on past data under stated assumptions; it is not a forecast and does not bound future losses."}


def walk_forward(strat: StrategySpec, snaps: list[ChainSnapshot], folds: int = 4, grid: dict | None = None) -> dict:
    """Chronological folds: pick parameters on fold k (in-sample), evaluate on fold k+1 (out-of-sample)."""
    grid = grid or {"wing_offset_strikes": [2, 4, 6], "stop_pct": [None, 50.0, 100.0]}
    days = list(group_days(snaps).items())
    if len(days) < folds + 1:
        return {"status": "INSUFFICIENT DATA", "days": len(days), "needed": folds + 1}
    size = len(days) // (folds + 1)
    chunks = [days[i * size:(i + 1) * size] for i in range(folds + 1)]
    combos = [dict(zip(grid, v, strict=True)) for v in itertools.product(*grid.values())]
    rows, oos_daily = [], []
    for k in range(folds):
        ins = [s for _, d in chunks[k] for s in d]
        oos = [s for _, d in chunks[k + 1] for s in d]
        scored = []
        for c in combos:
            r = backtest(strat, ins, BTParams(**c))
            m = r["metrics"]
            scored.append((m.get("mean_daily") or -1e18, c))
        best = max(scored, key=lambda x: x[0])[1]
        r_oos = backtest(strat, oos, BTParams(**best))
        oos_daily += [t["net_pnl"] for t in r_oos["trades"]]
        rows.append({"fold": k + 1, "in_sample": [chunks[k][0][0], chunks[k][-1][0]], "out_of_sample": [chunks[k + 1][0][0], chunks[k + 1][-1][0]],
                     "chosen": best, "oos_metrics": r_oos["metrics"]})
    hit = sum(1 for r in rows if (r["oos_metrics"].get("net_pnl") or 0) > 0) / len(rows)
    return {"status": "OK", "label": label_of(snaps), "folds": rows, "oos_metrics": metrics(oos_daily), "oos_hit_rate": round(hit, 3),
            "note": "parameters are chosen only on in-sample folds; out-of-sample folds are never used for selection"}


def sensitivity(strat: StrategySpec, snaps: list[ChainSnapshot], base: BTParams | None = None) -> dict:
    base = base or BTParams(wing_offset_strikes=strat.wing_offset_strikes)
    variants = {"base": base, "costs_x1.5": BTParams(**{**base.__dict__, "cost_multiplier": 1.5}),
                "slippage_x2": BTParams(**{**base.__dict__, "slippage_bps": base.slippage_bps * 2}),
                "entry_+5min": BTParams(**{**base.__dict__, "entry_shift_min": 5}), "fill_delay_2": BTParams(**{**base.__dict__, "fill_delay": 2})}
    return {k: backtest(strat, snaps, v)["metrics"] for k, v in variants.items()}


def stress(strat: StrategySpec, snap: ChainSnapshot, p: BTParams | None = None, shocks=(-0.04, -0.02, 0.02, 0.04), vol_shocks=(0.0, 0.5)) -> dict:
    """Instantaneous spot / IV shocks applied to the entry structure on one snapshot (Black-Scholes repricing)."""
    p = p or BTParams(wing_offset_strikes=strat.wing_offset_strikes)
    legs = _structure(strat, snap, p)
    if legs is None or snap.spot is None:
        return {"status": "INSUFFICIENT DATA"}
    lot = und_spec(snap.underlying).lot_size_on(_ist(snap.ts).date())
    qty = p.lots * lot
    exp_dt = dt.datetime(snap.expiry.year, snap.expiry.month, snap.expiry.day, 15, 30, tzinfo=IST)
    t = max((exp_dt - _ist(snap.ts)).total_seconds(), 600.0) / (365 * 86400)
    out = []
    for ds, dv in itertools.product(shocks, vol_shocks):
        pnl = 0.0
        for strike, ot, side in legs:
            leg = _leg(snap, strike, ot)
            iv = (leg.iv / 100) if leg is not None and leg.iv else 0.15
            now_px = leg.ltp if leg is not None and leg.ltp else bs_price(snap.spot, strike, t, iv, ot == "CE")
            new_px = bs_price(snap.spot * (1 + ds), strike, t, iv * (1 + dv), ot == "CE")
            pnl += (now_px - new_px) * qty if side == "SELL" else (new_px - now_px) * qty
        out.append({"spot_shock_pct": ds * 100, "iv_shock_pct": dv * 100, "pnl": round(pnl, 2)})
    return {"status": "OK", "label": "MODEL STRESS (Black-Scholes repricing) — excludes liquidity and gaps beyond the shocks", "scenarios": out,
            "worst": min(out, key=lambda r: r["pnl"])}


def validation_summary(wf: dict, label: str, min_days: int = 60) -> dict | None:
    """Only real-data walk-forward results with enough days become 'validated' evidence for the Strategy Agent."""
    if wf.get("status") != "OK" or "SIMULATED" in label:
        return None
    days = sum(r["oos_metrics"].get("days", 0) for r in wf["folds"])
    if days < min_days:
        return None
    return {"oos_hit_rate": wf["oos_hit_rate"], "oos_days": days, "oos_net_pnl": wf["oos_metrics"].get("net_pnl"), "label": label}

