"""Strategy definitions shared by the Quantitative Strategy Agent and the backtester.

A strategy here is a deterministic recipe: which strikes, which sides, when to
enter and exit. It does not decide whether to trade — the Master Agent, the
owner (or a pre-approved automation policy) and the Risk Kernel do.

Structural max loss is reported only for defined-risk structures and only at
expiry: (wing width − net credit) × quantity. It excludes gaps, slippage,
early exits and liquidity, and it is not a guarantee.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict

from amrt.analytics.option_chain import atm_strike
from amrt.core.enums import OptionType, OrderPurpose, Side
from amrt.marketdata.models import ChainSnapshot, OptionLeg


class StrategySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    version: str
    description: str
    structure: Literal["SHORT_STRADDLE", "IRON_BUTTERFLY"]
    entry_time: str = "09:20"          # IST, first minute an entry may be proposed
    entry_window_minutes: int = 10     # proposals expire if not acted on inside this window
    exit_time: str = "15:05"           # IST, flatten time
    wing_offset_strikes: int = 4       # IRON_BUTTERFLY only
    roll_threshold_strikes: int = 2    # propose a roll when ATM moves this many strikes from the short strike
    lots: int = 1
    exchanges: tuple[str, ...] = ("NSE", "BSE")


ROLLING_ATM_STRADDLE = StrategySpec(
    strategy_id="ROLLING_ATM_STRADDLE", version="1.0",
    description="Short ATM straddle entered at 09:20 IST, rolled when ATM drifts, flat by 15:05 IST. Undefined risk.",
    structure="SHORT_STRADDLE")
ROLLING_ATM_IRON_FLY = StrategySpec(
    strategy_id="ROLLING_ATM_IRON_FLY", version="1.0",
    description="Short ATM straddle with long wings 4 strikes out (iron butterfly). Defined risk at expiry.",
    structure="IRON_BUTTERFLY")
MCX_IRON_FLY = StrategySpec(
    strategy_id="MCX_ATM_IRON_FLY", version="1.0",
    description="Iron butterfly on MCX options; entry 09:05, flat by 23:00 IST.",
    structure="IRON_BUTTERFLY", entry_time="09:05", exit_time="23:00", exchanges=("MCX",))

REGISTRY: dict[tuple[str, str], StrategySpec] = {(s.strategy_id, s.version): s for s in (ROLLING_ATM_STRADDLE, ROLLING_ATM_IRON_FLY, MCX_IRON_FLY)}


def get(strategy_id: str, version: str) -> StrategySpec | None:
    return REGISTRY.get((strategy_id, version))


def _round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


def leg_price(leg: OptionLeg | None, side: Side, tick: float = 0.05, marketable: bool = False) -> float | None:
    """Limit price: mid when a sane two-sided quote exists (the touch when marketable), else last traded price."""
    if leg is None:
        return None
    if leg.bid and leg.ask and leg.ask >= leg.bid > 0:
        if marketable:   # never round a marketable price back inside the touch
            px = leg.ask if side == Side.BUY else leg.bid
            steps = px / tick
            return round((math.ceil(steps - 1e-9) if side == Side.BUY else math.floor(steps + 1e-9)) * tick, 2)
        return _round_tick((leg.bid + leg.ask) / 2, tick)
    return _round_tick(leg.ltp, tick) if leg.ltp else None


def build_entry(spec: StrategySpec, snap: ChainSnapshot, lots: int | None = None) -> tuple[list[dict] | None, str]:
    """Return (legs, "") or (None, reason). Legs are hedges (BUY) first."""
    if snap.spot is None:
        return None, "underlying price unavailable"
    rows = snap.sorted_rows()
    strikes = [r.strike for r in rows]
    if not strikes:
        return None, "empty chain"
    atm = atm_strike(strikes, snap.spot)
    i = strikes.index(atm)
    by_strike = {r.strike: r for r in rows}
    want: list[tuple[float, OptionType, Side, OrderPurpose]] = [(atm, OptionType.CE, Side.SELL, OrderPurpose.ENTRY), (atm, OptionType.PE, Side.SELL, OrderPurpose.ENTRY)]
    if spec.structure == "IRON_BUTTERFLY":
        w = spec.wing_offset_strikes
        if i - w < 0 or i + w >= len(strikes):
            return None, f"wing strikes {w} away from ATM are not listed in the loaded chain"
        want = [(strikes[i + w], OptionType.CE, Side.BUY, OrderPurpose.HEDGE), (strikes[i - w], OptionType.PE, Side.BUY, OrderPurpose.HEDGE)] + want
    legs = []
    for strike, ot, side, purpose in want:
        row = by_strike[strike]
        leg = row.ce if ot == OptionType.CE else row.pe
        price = leg_price(leg, side, marketable=purpose == OrderPurpose.HEDGE)   # hedges priced to fill before shorts are sent
        if leg is None or price is None:
            return None, f"no usable quote for {strike:g} {ot.value}"
        legs.append({"instrument_key": leg.instrument_key, "strike": strike, "option_type": ot.value, "side": side.value, "lots": lots or spec.lots,
                     "order_type": "LIMIT", "limit_price": price, "purpose": purpose.value, "reduce_only": False,
                     "bid": leg.bid, "ask": leg.ask, "ltp": leg.ltp})
    return legs, ""


def build_exit(positions: list[dict], snap: ChainSnapshot | None, purpose: OrderPurpose = OrderPurpose.EXIT) -> list[dict]:
    """Close every open position on the chain's underlying; shorts are bought back first."""
    quotes: dict[str, OptionLeg] = {}
    if snap is not None:
        for r in snap.rows:
            for leg in (r.ce, r.pe):
                if leg is not None:
                    quotes[leg.instrument_key] = leg
    legs = []
    for p in sorted(positions, key=lambda p: 0 if p["net_qty"] < 0 else 1):
        side = Side.BUY if p["net_qty"] < 0 else Side.SELL
        lots = abs(p["net_qty"]) // max(1, p["lot_size"])
        if lots <= 0:
            continue
        price = leg_price(quotes.get(p["instrument_key"]), side, marketable=True)
        legs.append({"instrument_key": p["instrument_key"], "side": side.value, "lots": lots, "order_type": "LIMIT" if price else "MARKET",
                     "limit_price": price, "purpose": purpose.value, "reduce_only": True})
    return legs


def structure_metrics(spec: StrategySpec, legs: list[dict], lot_size: int) -> dict:
    """Net credit per unit, structural max loss at expiry (defined risk only) and breakevens."""
    credit = sum((leg["limit_price"] or 0.0) * (1 if leg["side"] == "SELL" else -1) for leg in legs)
    lots = min(leg["lots"] for leg in legs) if legs else 0
    qty = lots * lot_size
    shorts = [leg for leg in legs if leg["side"] == "SELL"]
    centre = shorts[0]["strike"] if shorts else None
    out = {"net_credit_per_unit": round(credit, 2), "quantity": qty, "net_credit_inr": round(credit * qty, 2),
           "breakevens": [round(centre - credit, 2), round(centre + credit, 2)] if centre is not None else [],
           "max_loss_inr": None, "max_loss_basis": "undefined risk: naked short options have no structural maximum loss"}
    if spec.structure == "IRON_BUTTERFLY":
        wings = [leg for leg in legs if leg["side"] == "BUY"]
        if centre is not None and len(wings) == 2:
            width = max(abs(w["strike"] - centre) for w in wings)
            out["max_loss_inr"] = round(max(0.0, width - credit) * qty, 2)
            out["max_loss_basis"] = "structural loss at expiry = (wing width − net credit) × quantity; excludes gaps, slippage, early exit; not a guarantee"
    return out


def ist_minutes(t: dt.datetime) -> int:
    return t.hour * 60 + t.minute


def hhmm_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)
