"""Black-Scholes pricing, implied volatility and Greeks (European, continuous dividend 0).

Greeks are only reported when the implied volatility solves inside sane bounds;
otherwise the caller shows them as unavailable instead of inventing values.
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)


def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(spot: float, strike: float, t_years: float, vol: float, is_call: bool, r: float = 0.065) -> float:
    if t_years <= 0 or vol <= 0:
        return max(0.0, (spot - strike) if is_call else (strike - spot))
    sq = vol * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t_years) / sq
    d2 = d1 - sq
    if is_call:
        return spot * _n(d1) - strike * math.exp(-r * t_years) * _n(d2)
    return strike * math.exp(-r * t_years) * _n(-d2) - spot * _n(-d1)


def bs_greeks(spot: float, strike: float, t_years: float, vol: float, is_call: bool, r: float = 0.065) -> dict[str, float]:
    """delta, gamma, theta (per calendar day), vega (per 1 vol point)."""
    sq = vol * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t_years) / sq
    d2 = d1 - sq
    gamma = _pdf(d1) / (spot * sq)
    vega = spot * _pdf(d1) * math.sqrt(t_years) / 100.0
    if is_call:
        delta = _n(d1)
        theta = (-spot * _pdf(d1) * vol / (2 * math.sqrt(t_years)) - r * strike * math.exp(-r * t_years) * _n(d2)) / 365.0
    else:
        delta = _n(d1) - 1.0
        theta = (-spot * _pdf(d1) * vol / (2 * math.sqrt(t_years)) + r * strike * math.exp(-r * t_years) * _n(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(price: float, spot: float, strike: float, t_years: float, is_call: bool, r: float = 0.065) -> float | None:
    """Bisection IV in [0.5%, 500%]; None when the price is outside the no-arbitrage band."""
    if price is None or price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None
    intrinsic = max(0.0, (spot - strike * math.exp(-r * t_years)) if is_call else (strike * math.exp(-r * t_years) - spot))
    if price < intrinsic - 1e-9:
        return None
    lo, hi = 0.005, 5.0
    if bs_price(spot, strike, t_years, hi, is_call, r) < price:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t_years, mid, is_call, r) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)
