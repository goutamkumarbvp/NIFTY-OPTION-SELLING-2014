"""Option chain construction.

For the simulated / derived data path the chain is priced from the underlying
spot with a volatility smile calibrated to the VIX level. Open interest is a
persistent stochastic state so OI change, PCR and max-pain evolve realistically.
When a broker feed supplies real option quotes they replace the model values
through ``apply_broker_quotes``.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from typing import Dict, List, Optional, Tuple

from terminal.core.clock import expiry_series, now_ist, parse_hhmm, year_fraction_to_expiry
from terminal.core.models import ChainRow, OptionChain, OptionQuote, OptionType, Underlying
from terminal.market.pricing import bs_greeks, bs_price, implied_vol, round_to_tick


class OptionChainBuilder:
    def __init__(self, seed: int = 7, strikes_each_side: int = 25) -> None:
        self.rng = random.Random(seed)
        self.strikes_each_side = strikes_each_side
        self._oi: Dict[Tuple[str, str, float, str], int] = {}
        self._oi_day_open: Dict[Tuple[str, str, float, str], int] = {}
        self._broker_quotes: Dict[str, dict] = {}
        self._last_spot: Dict[str, float] = {}

    # ------------------------------------------------------------------ expiries
    def expiries(self, u: Underlying, count: int = 4) -> List[str]:
        now = now_ist()
        start = now.date()
        # an expiry that already settled today is no longer tradeable
        if now.time() >= parse_hhmm(u.session_close):
            start = start + dt.timedelta(days=1)
        return [d.isoformat() for d in expiry_series(start, u.expiry_weekday, u.weekly, count)]

    def nearest_expiry(self, u: Underlying) -> str:
        return self.expiries(u, 1)[0]

    # ------------------------------------------------------------------ smile
    @staticmethod
    def _smile(moneyness: float, base_iv: float, t: float) -> float:
        """Skewed smile: puts richer than calls (index option convention)."""
        k = math.log(moneyness)
        skew = -0.9 * k  # negative skew
        curve = 3.2 * k * k / max(math.sqrt(t * 52), 0.35)
        return max(0.05, base_iv * (1 + skew + curve))

    def _model_oi(self, key: Tuple[str, str, float, str], distance_steps: float, is_put: bool, spot_move: float) -> int:
        if key not in self._oi:
            # writers concentrate 3-6 strikes OTM: CE walls above spot, PE walls below
            peak = -4.0 if is_put else 4.0
            shape = math.exp(-0.16 * abs(distance_steps - peak)) + 0.35 * math.exp(-0.3 * abs(distance_steps))
            base = int(2_200_000 * shape * (1.12 if is_put else 1.0))
            base = int(base * self.rng.uniform(0.7, 1.35))
            self._oi[key] = base
            self._oi_day_open[key] = base
        # writers add OI at OTM strikes; unwind near ATM when spot moves toward them
        drift = self.rng.gauss(0.0, 0.004)
        if abs(distance_steps) < 2:
            drift -= 0.002 * (1 if spot_move != 0 else 0)
        elif 2 <= abs(distance_steps) <= 6:
            drift += 0.0025
        self._oi[key] = max(1000, int(self._oi[key] * (1 + drift)))
        return self._oi[key]

    def apply_broker_quotes(self, quotes: Dict[str, dict]) -> None:
        self._broker_quotes.update(quotes)

    @property
    def broker_quote_count(self) -> int:
        return len(self._broker_quotes)

    @staticmethod
    def option_symbol(underlying: str, expiry_iso: str, strike: float, option_type: str) -> str:
        exp_date = dt.date.fromisoformat(expiry_iso)
        return f"{underlying}{exp_date.strftime('%d%b%y').upper()}{int(strike)}{option_type}"

    # ------------------------------------------------------------------ build
    def build(self, u: Underlying, spot: float, vix: float, expiry: Optional[str] = None, now: Optional[dt.datetime] = None,
              extra_strikes: Optional[set] = None) -> OptionChain:
        """Build the chain around ATM. ``extra_strikes`` (e.g. strikes held in open
        positions) are always included so marks never go stale after a big move."""
        now = now or now_ist()
        expiry = expiry or self.nearest_expiry(u)
        exp_date = dt.date.fromisoformat(expiry)
        t = year_fraction_to_expiry(now, exp_date, u.session_close)
        days = t * 365.0
        step = u.strike_step
        atm = round(spot / step) * step
        base_iv = u.base_vol if u.exchange.value == "MCX" else max(0.07, (vix / 100.0) * (u.base_vol / 0.13))
        # near expiry: weekly IV lifts (event/gamma premium)
        if days < 1.5:
            base_iv *= 1.10
        spot_move = spot - self._last_spot.get(u.symbol, spot)
        self._last_spot[u.symbol] = spot
        rows: List[ChainRow] = []
        tot_ce_oi = tot_pe_oi = 0
        tot_ce_vol = tot_pe_vol = 0
        # window scales with the expected move so 10-20 delta strikes (and their wings)
        # are always inside the chain, even for monthly expiries on high-priced indices
        sigma_pts = spot * base_iv * math.sqrt(max(t, 1e-6))
        half = int(min(60, max(self.strikes_each_side, math.ceil(3.5 * sigma_pts / step) + 5)))
        strikes = {atm + i * step for i in range(-half, half + 1)}
        strikes |= {float(x) for x in (extra_strikes or set())}
        for strike in sorted(strikes):
            if strike <= 0:
                continue
            i = (strike - atm) / step
            quotes = {}
            for ot in (OptionType.CE, OptionType.PE):
                is_call = ot == OptionType.CE
                iv = self._smile(strike / spot, base_iv, t)
                key = (u.symbol, expiry, strike, ot.value)
                sym = f"{u.symbol}{exp_date.strftime('%d%b%y').upper()}{int(strike)}{ot.value}"
                bq = self._broker_quotes.get(sym)
                if bq and float(bq.get("ltp", 0)) > 0:
                    ltp = float(bq.get("ltp", 0))
                    live_iv = float(bq.get("iv", 0) or 0)
                    if live_iv > 1.5:  # broker quotes IV in percent
                        live_iv /= 100.0
                    if live_iv <= 0:
                        live_iv = implied_vol(ltp, spot, strike, t, is_call)
                    iv = live_iv if live_iv > 0 else iv
                    oi = int(bq.get("oi", 0))
                    vol = int(bq.get("volume", 0))
                    oi_change = int(bq.get("oi_change", 0))
                else:
                    ltp = bs_price(spot, strike, t, iv, is_call)
                    oi = self._model_oi(key, i, not is_call, spot_move)
                    oi_change = oi - self._oi_day_open.get(key, oi)
                    vol = int(oi * self.rng.uniform(0.15, 0.6) * math.exp(-0.08 * abs(i)))
                    if abs(i) > half:
                        oi = int(oi * 0.2)
                        vol = int(vol * 0.2)
                ltp = max(0.05, round_to_tick(ltp, u.tick_size))
                g = bs_greeks(spot, strike, t, iv, is_call)
                spread = max(u.tick_size, round_to_tick(ltp * (0.004 + 0.02 * abs(i) / self.strikes_each_side), u.tick_size))
                bid = max(0.05, round_to_tick(ltp - spread / 2, u.tick_size))
                ask = round_to_tick(ltp + spread / 2, u.tick_size)
                q = OptionQuote(symbol=sym, underlying=u.symbol, expiry=expiry, strike=strike, option_type=ot, ltp=ltp, bid=bid, ask=ask,
                                iv=round(iv * 100, 2), delta=round(g["delta"], 4), gamma=round(g["gamma"], 6), theta=round(g["theta"], 2), vega=round(g["vega"], 2),
                                oi=oi, oi_change=oi_change, volume=vol)
                quotes[ot] = q
                if is_call:
                    tot_ce_oi += oi
                    tot_ce_vol += vol
                else:
                    tot_pe_oi += oi
                    tot_pe_vol += vol
            rows.append(ChainRow(strike=strike, ce=quotes[OptionType.CE], pe=quotes[OptionType.PE]))
        pcr = round(tot_pe_oi / tot_ce_oi, 3) if tot_ce_oi else 0.0
        pcr_vol = round(tot_pe_vol / tot_ce_vol, 3) if tot_ce_vol else 0.0
        atm_row = next((r for r in rows if r.strike == atm), rows[len(rows) // 2])
        iv_atm = round((atm_row.ce.iv + atm_row.pe.iv) / 2, 2)
        expected_move = round(spot * (iv_atm / 100.0) * math.sqrt(max(t, 1e-6)), 1)
        return OptionChain(underlying=u.symbol, exchange=u.exchange, expiry=expiry, spot=spot, ts=now.timestamp(), atm_strike=atm, lot_size=u.lot_size,
                           days_to_expiry=round(days, 3), rows=rows, pcr=pcr, pcr_volume=pcr_vol, max_pain=self._max_pain(rows), total_ce_oi=tot_ce_oi,
                           total_pe_oi=tot_pe_oi, iv_atm=iv_atm, expected_move=expected_move)

    @staticmethod
    def _max_pain(rows: List[ChainRow], window: int = 30) -> float:
        # evaluate candidates near the OI mass (centre of the chain) to keep this O(n·window)
        mid = len(rows) // 2
        cands = rows[max(0, mid - window): mid + window + 1]
        best, best_pain = cands[0].strike, float("inf")
        for cand in cands:
            pain = 0.0
            for r in rows:
                pain += max(cand.strike - r.strike, 0) * r.ce.oi + max(r.strike - cand.strike, 0) * r.pe.oi
            if pain < best_pain:
                best, best_pain = cand.strike, pain
        return best

    def reset_day(self) -> None:
        self._oi_day_open = dict(self._oi)
