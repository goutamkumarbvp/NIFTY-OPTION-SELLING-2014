"""Synthetic open-interest model used by the simulator (and as a fallback when
a live broker supplies prices but no OI). Kept out of the pricing path so the
production chain builder only prices and overlays quotes."""
from __future__ import annotations

import math
import random
from typing import Dict, Tuple


class SimulatedOIModel:
    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self._oi: Dict[Tuple[str, str, float, str], int] = {}
        self._day_open: Dict[Tuple[str, str, float, str], int] = {}

    def oi(self, key: Tuple[str, str, float, str], distance_steps: float, is_put: bool, spot_move: float, in_window: bool = True) -> Tuple[int, int]:
        """Returns (oi, oi_change_since_day_open). Writers concentrate 3-6 strikes OTM:
        CE walls above spot, PE walls below; near-ATM OI unwinds when spot moves."""
        if key not in self._oi:
            peak = -4.0 if is_put else 4.0
            shape = math.exp(-0.16 * abs(distance_steps - peak)) + 0.35 * math.exp(-0.3 * abs(distance_steps))
            base = int(2_200_000 * shape * (1.12 if is_put else 1.0) * self.rng.uniform(0.7, 1.35))
            self._oi[key] = base
            self._day_open[key] = base
        drift = self.rng.gauss(0.0, 0.004)
        if abs(distance_steps) < 2:
            drift -= 0.002 * (1 if spot_move != 0 else 0)
        elif 2 <= abs(distance_steps) <= 6:
            drift += 0.0025
        self._oi[key] = max(1000, int(self._oi[key] * (1 + drift)))
        oi = self._oi[key] if in_window else int(self._oi[key] * 0.2)
        return oi, oi - self._day_open.get(key, oi)

    def volume(self, oi: int, distance_steps: float) -> int:
        return int(oi * self.rng.uniform(0.15, 0.6) * math.exp(-0.08 * abs(distance_steps)))

    def reset_day(self) -> None:
        self._day_open = dict(self._oi)
