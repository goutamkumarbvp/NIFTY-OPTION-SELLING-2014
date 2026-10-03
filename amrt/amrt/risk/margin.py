"""Margin estimate used only where the broker's own figure is not available (pre-trade sizing, paper).

ESTIMATE, not SPAN: short option ≈ 12 % of underlying notional (floor 5 %), long
option = premium paid. Live risk decisions prefer the broker-reported margin and
treat this estimate as the incremental requirement of the proposed order.
"""
from __future__ import annotations

from amrt.core.enums import Side

SHORT_PCT = 0.12
SHORT_FLOOR_PCT = 0.05


def estimate_option_margin(side: Side, qty: int, spot: float | None, premium: float | None) -> float | None:
    if qty <= 0:
        return 0.0
    if side == Side.BUY:
        return None if premium is None else round(qty * premium, 2)
    if spot is None:
        return None
    return round(max(SHORT_PCT, SHORT_FLOOR_PCT) * spot * qty, 2)
