"""Fast strategy backtester.

Simulates day-by-day deployment of a strategy on a synthetic (or supplied)
price path, repricing legs with the Black-Scholes model as spot / time / vol
evolve intraday, applying the same stop-loss / target / square-off rules the
live engine uses. It is a research tool for parameter selection and sizing, not
a tick-accurate replay.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from typing import Dict, List, Optional

from terminal.core.models import Side, Underlying
from terminal.market.chain import OptionChainBuilder
from terminal.market.pricing import bs_price
from terminal.strategy.library import SPECS, build_legs, net_credit_per_lot


def run_backtest(u: Underlying, strategy: str, days: int = 60, lots: int = 1, params: Optional[dict] = None, seed: int = 42, vix: float = 13.5,
                 daily_vol_override: Optional[float] = None, price_path: Optional[List[float]] = None) -> Dict:
    if strategy not in SPECS:
        raise ValueError("UNKNOWN_STRATEGY")
    p = {**SPECS[strategy].params, **(params or {})}
    rng = random.Random(seed)
    builder = OptionChainBuilder(seed=seed)
    spot = u.base_spot
    daily_vol = (daily_vol_override or u.base_vol) / math.sqrt(252)
    results: List[dict] = []
    equity, cum, peak, max_dd = [], 0.0, 0.0, 0.0
    start = dt.datetime.now().replace(hour=9, minute=20, second=0, microsecond=0) - dt.timedelta(days=days)
    for d in range(days):
        day = start + dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        # expiry chosen so days_to_expiry is 1..7 like a weekly seller would see
        dte = rng.choice([1, 2, 3, 4, 5])
        expiry = (day + dt.timedelta(days=dte)).date().isoformat()
        chain = builder.build(u, spot, vix * rng.uniform(0.85, 1.2), expiry=expiry, now=day.replace(tzinfo=None).astimezone() if day.tzinfo else day.astimezone())
        legs = build_legs(strategy, chain, lots, p)
        credit = net_credit_per_lot(legs, u.lot_size) * lots
        sl_amt = abs(credit) * p.get("stop_loss_pct", 35) / 100
        tgt_amt = abs(credit) * p.get("target_pct", 50) / 100
        t0 = chain.days_to_expiry / 365
        # intraday path: 75 five-minute steps
        s = spot
        pnl, exit_reason = 0.0, "SQUARE_OFF"
        step_vol = daily_vol / math.sqrt(75)
        drift = rng.choice([-1, 0, 0, 1]) * daily_vol * 0.3
        for i in range(1, 76):
            s *= math.exp(rng.gauss(drift / 75, step_vol))
            t = max(t0 - (i / 75) * (6.25 / 24) / 365, 1e-5)
            mtm = 0.0
            for l in legs:
                iv = (chain.find(l.strike, l.option_type).iv if chain.find(l.strike, l.option_type) else chain.iv_atm) / 100
                px = bs_price(s, l.strike, t, iv, l.option_type.value == "CE")
                sign = 1 if l.side == Side.SELL else -1
                mtm += sign * (l.entry_price - px) * u.lot_size * l.lots
            pnl = mtm
            if mtm <= -sl_amt:
                exit_reason = "STOP_LOSS"
                break
            if mtm >= tgt_amt:
                exit_reason = "TARGET"
                break
        charges = 40.0 * len(legs) * lots * 2
        pnl -= charges
        cum += pnl
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        results.append({"day": day.date().isoformat(), "spot_open": round(spot, 1), "spot_close": round(s, 1), "credit": round(credit, 0), "pnl": round(pnl, 0), "exit": exit_reason, "dte": dte})
        equity.append(round(cum, 0))
        spot = s * math.exp(rng.gauss(0, daily_vol * 0.35))  # overnight gap
    pnls = [r["pnl"] for r in results]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    return {
        "strategy": strategy, "underlying": u.symbol, "days": len(results), "lots": lots, "params": p,
        "net_pnl": round(sum(pnls), 0), "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 0) if wins else None, "avg_loss": round(sum(losses) / len(losses), 0) if losses else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None, "max_drawdown": round(max_dd, 0),
        "exits": {k: sum(1 for r in results if r["exit"] == k) for k in ("TARGET", "STOP_LOSS", "SQUARE_OFF")},
        "equity": equity, "trades": results[-120:],
    }
