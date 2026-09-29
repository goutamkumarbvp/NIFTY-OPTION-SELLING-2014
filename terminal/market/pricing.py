"""Black-Scholes pricing, greeks and implied volatility (pure python, no numpy)."""
from __future__ import annotations

import math
from typing import Dict


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(spot: float, strike: float, t: float, vol: float, is_call: bool, r: float = 0.065) -> float:
    if t <= 0 or vol <= 0:
        intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
        return intrinsic
    sq = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / sq
    d2 = d1 - sq
    if is_call:
        return spot * _ncdf(d1) - strike * math.exp(-r * t) * _ncdf(d2)
    return strike * math.exp(-r * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_greeks(spot: float, strike: float, t: float, vol: float, is_call: bool, r: float = 0.065) -> Dict[str, float]:
    if t <= 0 or vol <= 0:
        itm = (spot > strike) if is_call else (spot < strike)
        return {"delta": (1.0 if is_call else -1.0) if itm else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / sq
    d2 = d1 - sq
    pdf = _npdf(d1)
    delta = _ncdf(d1) if is_call else _ncdf(d1) - 1.0
    gamma = pdf / (spot * sq)
    vega = spot * pdf * math.sqrt(t) / 100.0  # per 1 vol point
    if is_call:
        theta = (-(spot * pdf * vol) / (2 * math.sqrt(t)) - r * strike * math.exp(-r * t) * _ncdf(d2)) / 365.0
    else:
        theta = (-(spot * pdf * vol) / (2 * math.sqrt(t)) + r * strike * math.exp(-r * t) * _ncdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(price: float, spot: float, strike: float, t: float, is_call: bool, r: float = 0.065) -> float:
    """Bisection IV solver. Returns 0 when price is below intrinsic."""
    intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    if price <= intrinsic + 1e-9 or t <= 0:
        return 0.0
    lo, hi = 0.01, 5.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p = bs_price(spot, strike, t, mid, is_call, r)
        if abs(p - price) < 1e-6:
            return mid
        if p > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def round_to_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)
