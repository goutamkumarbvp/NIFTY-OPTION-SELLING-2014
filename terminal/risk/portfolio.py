"""Portfolio-level risk: scenario stress, Greek limits, beta-weighted cluster exposure,
expiry-day gamma cut-off and liquidity concentration — evaluated every risk cycle.

Stress re-prices every open option under a grid of spot × volatility shocks with the
same Black-Scholes model the chain uses, so the worst-case number is consistent with
the marks the desk sees. It is a limit (blocks new entries, RED) not an auto-flatten:
a stress breach means "reduce", which is the operator's or the council's call.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Dict, List

from terminal.core.clock import now_ist
from terminal.market.pricing import bs_price

DEFAULT_LIMITS: Dict[str, float] = {
    "portfolio_gamma_limit": 50.0,          # |gamma| in units
    "max_stress_loss": 60_000.0,            # worst scenario loss (₹); default set from capital in RiskManager
    "max_cluster_delta_notional": 30_000_000.0,  # beta-weighted delta notional per cluster (₹)
    "expiry_gamma_cutoff_minutes": 90,      # no new shorts on a contract expiring today within N min of its close
}

CLUSTERS: Dict[str, Dict[str, float]] = {  # beta to the cluster's reference index
    "NSE_EQUITY": {"NIFTY": 1.0, "BANKNIFTY": 1.2, "FINNIFTY": 1.05, "MIDCPNIFTY": 1.15, "SENSEX": 1.0, "BANKEX": 1.2},
    "ENERGY": {"CRUDEOIL": 1.0, "NATURALGAS": 0.6},
    "METALS": {"GOLD": 1.0, "SILVER": 1.4},
}
SCENARIOS: List[tuple] = [(s, v) for s in (-5.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 5.0) for v in (-20.0, 0.0, 20.0, 50.0)]


class PortfolioRisk:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.last: Dict = {"worst_loss": 0.0, "worst_scenario": "", "grid": [], "clusters": {}, "expiring_today": [], "oi_participation": [], "ts": 0.0}

    def _limit(self, key: str) -> float:
        return float(self.t.risk.limits.get(key, DEFAULT_LIMITS[key]))

    # ---------------------------------------------------------------- stress
    def stress(self) -> Dict:
        positions = self.t.positions.open_positions()
        if not positions:
            self.last = {"worst_loss": 0.0, "worst_scenario": "", "grid": [], "clusters": self.clusters(), "expiring_today": [], "oi_participation": [], "ts": time.time()}
            return self.last
        rows = []
        worst, worst_name = 0.0, ""
        for ds, dv in SCENARIOS:
            pnl = 0.0
            for p in positions:
                q = self.t.quote(p.symbol)
                spot = self.t.processor.last_price(p.underlying)
                if q is None or not spot:
                    continue
                ch = self.t.chains.get(p.underlying)
                tt = max((ch.days_to_expiry if ch and ch.expiry == p.expiry else self._dte(p.expiry)) / 365.0, 1e-4)
                iv = max(0.03, q.iv / 100.0 * (1 + dv / 100.0))  # quote IV is in vol points
                new_px = bs_price(spot * (1 + ds / 100.0), p.strike, tt, iv, p.option_type.value == "CE")
                pnl += (new_px - p.ltp) * p.net_qty
            rows.append({"spot_pct": ds, "vol_pct": dv, "pnl": round(pnl, 0)})
            if pnl < worst:
                worst, worst_name = pnl, f"spot {ds:+g}% / vol {dv:+g}%"
        self.last = {"worst_loss": round(worst, 0), "worst_scenario": worst_name, "grid": rows, "clusters": self.clusters(), "expiring_today": self.expiring_today(), "oi_participation": self.oi_participation(), "ts": time.time()}
        return self.last

    @staticmethod
    def _dte(expiry: str) -> float:
        try:
            return max(0.0, (dt.date.fromisoformat(expiry) - now_ist().date()).days + 0.5)
        except ValueError:
            return 1.0

    # -------------------------------------------------------------- clusters
    def clusters(self) -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        for name, members in CLUSTERS.items():
            notional = 0.0
            syms = []
            for p in self.t.positions.open_positions():
                beta = members.get(p.underlying)
                if beta is None:
                    continue
                spot = self.t.processor.last_price(p.underlying) or 0.0
                notional += p.delta * spot * beta  # p.delta is already × net_qty
                syms.append(p.underlying)
            if syms:
                out[name] = {"delta_notional": round(notional, 0), "members": sorted(set(syms)), "limit": self._limit("max_cluster_delta_notional")}
        return out

    def cluster_breaches(self) -> List[str]:
        lim = self._limit("max_cluster_delta_notional")
        return [f"CLUSTER_DELTA_{k}" for k, v in self.clusters().items() if abs(v["delta_notional"]) > lim]

    # ------------------------------------------------------------ expiry day
    def expiring_today(self) -> List[str]:
        today = now_ist().date().isoformat()
        return sorted({p.symbol for p in self.t.positions.open_positions() if p.expiry == today})

    def expiry_cutoff_active(self, expiry: str, exchange: str, now: dt.datetime | None = None) -> bool:
        now = now or now_ist()
        if expiry != now.date().isoformat():
            return False
        close = "23:30" if exchange == "MCX" else "15:30"
        hh, mm = (int(x) for x in close.split(":"))
        close_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        remaining = (close_dt - now).total_seconds()
        return 0 <= remaining <= self._limit("expiry_gamma_cutoff_minutes") * 60

    # -------------------------------------------------------------- liquidity
    def oi_participation(self) -> List[Dict]:
        out = []
        for p in self.t.positions.open_positions():
            q = self.t.quote(p.symbol)
            if q is not None and q.oi > 0:
                pct = abs(p.net_qty) / q.oi * 100
                if pct > 1.0:
                    out.append({"symbol": p.symbol, "pct": round(pct, 2)})
        return out

    # ----------------------------------------------------------------- limits
    def evaluate(self, greeks: Dict[str, float]) -> Dict[str, List[str]]:
        """Returns breaches (block entries, RED) and warnings (AMBER)."""
        breaches: List[str] = []
        warnings: List[str] = []
        st = self.stress()
        if -st["worst_loss"] > self._limit("max_stress_loss"):
            breaches.append("STRESS_LOSS_LIMIT")
        elif -st["worst_loss"] > self._limit("max_stress_loss") * 0.75:
            warnings.append("STRESS_LOSS_NEAR_LIMIT")
        if abs(greeks.get("gamma", 0.0)) > self._limit("portfolio_gamma_limit"):
            warnings.append("PORTFOLIO_GAMMA")
        breaches += self.cluster_breaches()
        if st["expiring_today"]:
            warnings.append(f"EXPIRY_DAY_POSITIONS:{len(st['expiring_today'])}")
        if any(x["pct"] > self.t.risk.limits.get("max_oi_participation_pct", 5.0) for x in st["oi_participation"]):
            warnings.append("OI_CONCENTRATION")
        return {"breaches": breaches, "warnings": warnings}

    def describe(self) -> Dict:
        return dict(self.last)
