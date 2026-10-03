"""Option-selling strategy library.

Each strategy turns an option chain + parameters into a list of legs and knows
its payoff profile so the risk layer can size, gate and monitor it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from terminal.core.models import Leg, OptionChain, OptionType, Side


@dataclass
class StrategySpec:
    key: str
    name: str
    description: str
    regime_fit: List[str]  # RANGE / TRENDING_UP / TRENDING_DOWN / VOLATILE
    defined_risk: bool
    params: Dict[str, float] = field(default_factory=dict)


SPECS: Dict[str, StrategySpec] = {
    "short_straddle": StrategySpec("short_straddle", "Short Straddle", "Sell ATM CE + ATM PE. Max theta harvest, undefined risk, needs range-bound market.", ["RANGE"], False, {"stop_loss_pct": 30, "target_pct": 40}),
    "short_strangle": StrategySpec("short_strangle", "Short Strangle (delta)", "Sell OTM CE + OTM PE at a target delta (default 0.15). Wider breakevens than a straddle.", ["RANGE", "VOLATILE"], False, {"delta": 0.15, "stop_loss_pct": 35, "target_pct": 50}),
    "iron_condor": StrategySpec("iron_condor", "Iron Condor", "Short strangle with long wings. Defined risk, lower margin.", ["RANGE"], True, {"delta": 0.18, "wing_steps": 4, "stop_loss_pct": 45, "target_pct": 50}),
    "iron_fly": StrategySpec("iron_fly", "Iron Butterfly", "ATM straddle with protective wings. Defined risk straddle.", ["RANGE"], True, {"wing_steps": 5, "stop_loss_pct": 35, "target_pct": 40}),
    "bull_put_spread": StrategySpec("bull_put_spread", "Bull Put Credit Spread", "Sell OTM PE, buy lower PE. Bullish / supportive OI wall below.", ["TRENDING_UP", "RANGE"], True, {"delta": 0.25, "wing_steps": 3, "stop_loss_pct": 50, "target_pct": 55}),
    "bear_call_spread": StrategySpec("bear_call_spread", "Bear Call Credit Spread", "Sell OTM CE, buy higher CE. Bearish / resistance OI wall above.", ["TRENDING_DOWN", "RANGE"], True, {"delta": 0.25, "wing_steps": 3, "stop_loss_pct": 50, "target_pct": 55}),
    "jade_lizard": StrategySpec("jade_lizard", "Jade Lizard", "Short put + bear call spread. No upside risk when credit exceeds call-spread width.", ["TRENDING_UP", "RANGE"], False, {"delta": 0.2, "wing_steps": 2, "stop_loss_pct": 40, "target_pct": 50}),
}


def _strike_offset(chain: OptionChain, strike: float, steps: int) -> float:
    """Strike `steps` away; clamped to the chain's edge so a wing always exists."""
    step = chain.rows[1].strike - chain.rows[0].strike if len(chain.rows) > 1 else 50
    target = strike + steps * step
    strikes = [r.strike for r in chain.rows]
    if target in strikes:
        return target
    candidates = [k for k in strikes if (k > strike if steps > 0 else k < strike)]
    if not candidates:
        raise ValueError(f"NO_WING_AVAILABLE_BEYOND:{strike}")
    return max(candidates) if steps > 0 else min(candidates)


def build_legs(key: str, chain: OptionChain, lots: int, params: Dict[str, float] | None = None) -> List[Leg]:
    spec = SPECS[key]
    p = {**spec.params, **(params or {})}
    legs: List[Leg] = []
    exp = chain.expiry

    def leg(ot: OptionType, side: Side, strike: float) -> Leg:
        q = chain.find(strike, ot)
        if q is None:
            raise ValueError(f"STRIKE_NOT_IN_CHAIN:{strike}{ot.value}")
        if not q.live and not p.get("allow_model_prices"):
            raise ValueError(f"NO_LIVE_QUOTE:{q.symbol}")
        return Leg(option_type=ot, side=side, strike=strike, lots=lots, symbol=q.symbol, entry_price=q.ltp, ltp=q.ltp, expiry=exp)

    atm = chain.atm_strike
    if key == "short_straddle":
        legs = [leg(OptionType.CE, Side.SELL, atm), leg(OptionType.PE, Side.SELL, atm)]
    elif key == "short_strangle":
        ce = chain.by_delta(OptionType.CE, p["delta"])
        pe = chain.by_delta(OptionType.PE, p["delta"])
        legs = [leg(OptionType.CE, Side.SELL, ce.strike), leg(OptionType.PE, Side.SELL, pe.strike)]
    elif key == "iron_condor":
        ce = chain.by_delta(OptionType.CE, p["delta"])
        pe = chain.by_delta(OptionType.PE, p["delta"])
        w = int(p["wing_steps"])
        legs = [leg(OptionType.CE, Side.SELL, ce.strike), leg(OptionType.PE, Side.SELL, pe.strike),
                leg(OptionType.CE, Side.BUY, _strike_offset(chain, ce.strike, w)), leg(OptionType.PE, Side.BUY, _strike_offset(chain, pe.strike, -w))]
    elif key == "iron_fly":
        w = int(p["wing_steps"])
        legs = [leg(OptionType.CE, Side.SELL, atm), leg(OptionType.PE, Side.SELL, atm),
                leg(OptionType.CE, Side.BUY, _strike_offset(chain, atm, w)), leg(OptionType.PE, Side.BUY, _strike_offset(chain, atm, -w))]
    elif key == "bull_put_spread":
        pe = chain.by_delta(OptionType.PE, p["delta"])
        legs = [leg(OptionType.PE, Side.SELL, pe.strike), leg(OptionType.PE, Side.BUY, _strike_offset(chain, pe.strike, -int(p["wing_steps"])))]
    elif key == "bear_call_spread":
        ce = chain.by_delta(OptionType.CE, p["delta"])
        legs = [leg(OptionType.CE, Side.SELL, ce.strike), leg(OptionType.CE, Side.BUY, _strike_offset(chain, ce.strike, int(p["wing_steps"])))]
    elif key == "jade_lizard":
        pe = chain.by_delta(OptionType.PE, p["delta"])
        ce = chain.by_delta(OptionType.CE, p["delta"])
        legs = [leg(OptionType.PE, Side.SELL, pe.strike), leg(OptionType.CE, Side.SELL, ce.strike), leg(OptionType.CE, Side.BUY, _strike_offset(chain, ce.strike, int(p["wing_steps"])))]
    else:
        raise ValueError(f"UNKNOWN_STRATEGY:{key}")
    return legs


def net_credit_per_lot(legs: List[Leg], lot_size: int) -> float:
    credit = 0.0
    for l in legs:
        credit += (l.entry_price if l.side == Side.SELL else -l.entry_price) * lot_size
    return credit


def payoff_at(legs: List[Leg], spot: float, lot_size: int) -> float:
    """Payoff at expiry for the total position."""
    total = 0.0
    for l in legs:
        intrinsic = max(spot - l.strike, 0) if l.option_type == OptionType.CE else max(l.strike - spot, 0)
        sign = -1 if l.side == Side.SELL else 1
        total += sign * (intrinsic - l.entry_price) * lot_size * l.lots
    return total


def payoff_profile(legs: List[Leg], spot: float, lot_size: int, points: int = 61) -> dict:
    lo, hi = spot * 0.93, spot * 1.07
    xs = [lo + (hi - lo) * i / (points - 1) for i in range(points)]
    ys = [payoff_at(legs, x, lot_size) for x in xs]
    # breakevens by sign change
    bes = []
    for i in range(1, len(xs)):
        if (ys[i - 1] < 0 <= ys[i]) or (ys[i - 1] >= 0 > ys[i]):
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            bes.append(round(x0 + (0 - y0) * (x1 - x0) / (y1 - y0), 1) if y1 != y0 else round(x0, 1))
    strikes = sorted({l.strike for l in legs})
    ends = [payoff_at(legs, spot * 0.5, lot_size), payoff_at(legs, spot * 1.5, lot_size)]
    mids = [payoff_at(legs, s, lot_size) for s in strikes]
    max_profit = max(mids + ends + ys)
    max_loss = min(mids + ends + ys)
    # undefined risk = some option type is net short (more units sold than bought); a ratio
    # heuristic on the payoff ends mislabels narrow-credit spreads close to expiry
    net = {}
    for leg in legs:
        net[leg.option_type] = net.get(leg.option_type, 0) + (leg.lots if leg.side == Side.BUY else -leg.lots)
    unlimited = any(v < 0 for v in net.values())
    return {"x": [round(x, 1) for x in xs], "y": [round(y, 1) for y in ys], "breakevens": bes, "max_profit": round(max_profit, 1), "max_loss": round(max_loss, 1), "undefined_risk": unlimited}


def estimate_margin(legs: List[Leg], spot: float, lot_size: int, lots: int) -> float:
    """SPAN + exposure approximation calibrated to NSE index options: a naked
    short leg ≈ 7% of notional, a hedged leg ≈ 40% of that, and a two-sided
    short (straddle / strangle) gets the exchange's ~40% offset benefit."""
    shorts = [l for l in legs if l.side == Side.SELL]
    longs = [l for l in legs if l.side == Side.BUY]
    margin = 0.0
    for s in shorts:
        base = spot * lot_size * lots * 0.07
        hedged = any(l.option_type == s.option_type for l in longs)
        margin += base * (0.4 if hedged else 1.0)
    # short straddle/strangle benefit: exchange gives ~35% offset on the second leg
    if len(shorts) >= 2 and not longs:
        margin *= 0.6
    premium = sum(l.entry_price * lot_size * lots for l in longs)
    return round(margin + premium, 0)
