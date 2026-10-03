"""Option-chain analytics: observed facts first, inferences labelled as such.

Definitions (also in docs/ANALYTICS_METHODOLOGY.md):
* ATM strike  = listed strike nearest the underlying price (ties → lower strike).
* Window      = ATM ± N listed strikes (default N = 10).
* Total OI PCR        = Σ PE OI ÷ Σ CE OI over the scope.
* Change-in-OI PCR    = Σ PE ΔOI ÷ Σ CE ΔOI over the scope.
  - denominator 0           → undefined (None), never a number.
  - denominator < 0         → value reported with interpretation "CE net unwinding";
    the ratio is not comparable with the usual reading.
* Max / second-highest OI: ties broken by distance to ATM, then lower strike.
* Buildup / unwinding: largest positive / most negative ΔOI across CE and PE.
* Support / resistance zones (INFERENCE): top-2 PE OI strikes at or below spot,
  top-2 CE OI strikes at or above spot. OI does not reveal whether positions were
  bought or sold and does not predict direction.
* A scope with < 50 % of strikes carrying OI is INCOMPLETE and its ratios are None.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from amrt.core.enums import DataLabel
from amrt.marketdata.models import ChainRow, ChainSnapshot

ANALYTICS_VERSION = "chain/1.0"
INFERENCE_NOTE = "Inference from open interest; OI does not show whether contracts were bought or sold and does not predict direction."


class Ratio(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: float | None
    numerator: float
    denominator: float
    scope: str
    interpretation: str = ""
    note: str = ""


class StrikeValue(BaseModel):
    model_config = ConfigDict(frozen=True)
    strike: float
    side: str
    value: float


class ChainAnalytics(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: str = ANALYTICS_VERSION
    underlying: str
    expiry: str
    as_of: float
    source: str
    label: DataLabel
    status: str                            # OK | INCOMPLETE | DATA UNAVAILABLE
    spot: float | None
    atm_strike: float | None
    strike_step: float | None
    strikes_each_side: int
    window: list[float]
    missing_strikes: list[float]
    oi_coverage_pct: float
    ce_max_oi: StrikeValue | None = None
    ce_second_oi: StrikeValue | None = None
    pe_max_oi: StrikeValue | None = None
    pe_second_oi: StrikeValue | None = None
    max_buildup: StrikeValue | None = None
    max_unwinding: StrikeValue | None = None
    totals: dict[str, float | None] = Field(default_factory=dict)
    pcr_total_oi: Ratio | None = None
    pcr_change_oi: Ratio | None = None
    pcr_total_oi_full: Ratio | None = None
    pcr_change_oi_full: Ratio | None = None
    concentration: dict[str, float | None] = Field(default_factory=dict)
    support_zones: list[StrikeValue] = Field(default_factory=list)
    resistance_zones: list[StrikeValue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _strike_step(strikes: list[float]) -> float | None:
    diffs = sorted({round(b - a, 6) for a, b in zip(strikes, strikes[1:]) if b > a})
    return diffs[0] if diffs else None


def atm_strike(strikes: list[float], spot: float) -> float:
    return min(strikes, key=lambda k: (abs(k - spot), k))


def _ratio(num: float, den: float, scope: str, change: bool) -> Ratio:
    if den == 0:
        return Ratio(value=None, numerator=num, denominator=den, scope=scope, note="denominator is zero — ratio undefined")
    v = round(num / den, 4)
    interp = ""
    if change:
        if den < 0 and num < 0:
            interp = "both CE and PE net unwinding"
        elif den < 0:
            interp = "CE net unwinding (negative denominator) — not comparable with the usual reading"
        elif num < 0:
            interp = "PE net unwinding while CE builds"
        else:
            interp = "both sides net building"
    return Ratio(value=v, numerator=num, denominator=den, scope=scope, interpretation=interp)


def _top2(values: list[tuple[float, float]], atm: float, side: str) -> tuple[StrikeValue | None, StrikeValue | None]:
    vals = [(k, v) for k, v in values if v is not None]
    if not vals:
        return None, None
    ordered = sorted(vals, key=lambda kv: (-kv[1], abs(kv[0] - atm), kv[0]))
    first = StrikeValue(strike=ordered[0][0], side=side, value=ordered[0][1])
    second = StrikeValue(strike=ordered[1][0], side=side, value=ordered[1][1]) if len(ordered) > 1 else None
    return first, second


def _hhi(xs: list[float]) -> float | None:
    tot = sum(xs)
    if tot <= 0:
        return None
    return round(sum((x / tot) ** 2 for x in xs), 4)


def _top3_share(xs: list[float]) -> float | None:
    tot = sum(xs)
    if tot <= 0:
        return None
    return round(sum(sorted(xs, reverse=True)[:3]) / tot, 4)


def analyze_chain(snap: ChainSnapshot, strikes_each_side: int = 10, label: DataLabel | None = None) -> ChainAnalytics:
    rows = snap.sorted_rows()
    base: dict[str, Any] = {"underlying": snap.underlying, "expiry": snap.expiry.isoformat(), "as_of": snap.ts, "source": snap.source,
                            "label": label or snap.label, "spot": snap.spot, "strikes_each_side": strikes_each_side, "notes": [INFERENCE_NOTE]}
    if not rows or snap.spot is None or snap.spot <= 0:
        return ChainAnalytics(status="DATA UNAVAILABLE", atm_strike=None, strike_step=None, window=[], missing_strikes=[], oi_coverage_pct=0.0, **base)
    strikes = [r.strike for r in rows]
    step = snap.strike_step or _strike_step(strikes)
    atm = atm_strike(strikes, snap.spot)
    i = strikes.index(atm)
    win_rows = rows[max(0, i - strikes_each_side): i + strikes_each_side + 1]
    lo, hi = win_rows[0].strike, win_rows[-1].strike
    missing: list[float] = []
    if step:
        expected = [round(atm + k * step, 6) for k in range(-strikes_each_side, strikes_each_side + 1)]
        have = {round(s, 6) for s in strikes}
        missing = [s for s in expected if s not in have and s > 0]
    with_oi = [r for r in win_rows if (r.ce and r.ce.oi is not None) or (r.pe and r.pe.oi is not None)]
    expected_n = (2 * strikes_each_side + 1)
    coverage = round(100.0 * len(with_oi) / expected_n, 1)
    status = "OK" if coverage >= 80.0 and not missing else ("INCOMPLETE" if coverage >= 50.0 else "DATA UNAVAILABLE")

    def sums(rs: Iterable[ChainRow]) -> dict[str, float]:
        out = {"ce_oi": 0.0, "pe_oi": 0.0, "ce_oi_change": 0.0, "pe_oi_change": 0.0, "ce_volume": 0.0, "pe_volume": 0.0}
        for r in rs:
            for side, leg in (("ce", r.ce), ("pe", r.pe)):
                if leg is None:
                    continue
                out[f"{side}_oi"] += float(leg.oi or 0)
                out[f"{side}_oi_change"] += float(leg.oi_change or 0)
                out[f"{side}_volume"] += float(leg.volume or 0)
        return out

    w = sums(win_rows)
    full = sums(rows)
    has_change = any((r.ce and r.ce.oi_change is not None) or (r.pe and r.pe.oi_change is not None) for r in win_rows)
    scope = f"ATM±{strikes_each_side} ({lo:g}–{hi:g})"
    usable = status != "DATA UNAVAILABLE"
    pcr_total = _ratio(w["pe_oi"], w["ce_oi"], scope, False) if usable else None
    pcr_change = _ratio(w["pe_oi_change"], w["ce_oi_change"], scope, True) if usable and has_change else None
    pcr_total_full = _ratio(full["pe_oi"], full["ce_oi"], "full chain", False)
    pcr_change_full = _ratio(full["pe_oi_change"], full["ce_oi_change"], "full chain", True) if has_change else None

    ce_oi = [(r.strike, float(r.ce.oi)) for r in win_rows if r.ce and r.ce.oi is not None]
    pe_oi = [(r.strike, float(r.pe.oi)) for r in win_rows if r.pe and r.pe.oi is not None]
    ce1, ce2 = _top2(ce_oi, atm, "CE")
    pe1, pe2 = _top2(pe_oi, atm, "PE")
    changes = [(r.strike, "CE", float(r.ce.oi_change)) for r in win_rows if r.ce and r.ce.oi_change is not None] + \
              [(r.strike, "PE", float(r.pe.oi_change)) for r in win_rows if r.pe and r.pe.oi_change is not None]
    pos = [c for c in changes if c[2] > 0]
    neg = [c for c in changes if c[2] < 0]
    buildup = max(pos, key=lambda c: (c[2], -abs(c[0] - atm))) if pos else None
    unwinding = min(neg, key=lambda c: (c[2], abs(c[0] - atm))) if neg else None
    spot = snap.spot
    support = sorted([StrikeValue(strike=k, side="PE", value=v) for k, v in pe_oi if k <= spot], key=lambda s: (-s.value, -s.strike))[:2]
    resist = sorted([StrikeValue(strike=k, side="CE", value=v) for k, v in ce_oi if k >= spot], key=lambda s: (-s.value, s.strike))[:2]
    notes = list(base.pop("notes"))
    if missing:
        notes.append(f"{len(missing)} expected strikes missing from the source in the window")
    if not has_change:
        notes.append("source provided no OI change — change-in-OI PCR and buildup/unwinding unavailable")
    return ChainAnalytics(status=status, atm_strike=atm, strike_step=step, window=[lo, hi], missing_strikes=missing, oi_coverage_pct=coverage,
                          ce_max_oi=ce1, ce_second_oi=ce2, pe_max_oi=pe1, pe_second_oi=pe2,
                          max_buildup=StrikeValue(strike=buildup[0], side=buildup[1], value=buildup[2]) if buildup else None,
                          max_unwinding=StrikeValue(strike=unwinding[0], side=unwinding[1], value=unwinding[2]) if unwinding else None,
                          totals={**w, **{f"full_{k}": v for k, v in full.items()}},
                          pcr_total_oi=pcr_total, pcr_change_oi=pcr_change, pcr_total_oi_full=pcr_total_full, pcr_change_oi_full=pcr_change_full,
                          concentration={"ce_top3_share": _top3_share([v for _, v in ce_oi]), "pe_top3_share": _top3_share([v for _, v in pe_oi]),
                                         "ce_hhi": _hhi([v for _, v in ce_oi]), "pe_hhi": _hhi([v for _, v in pe_oi])},
                          support_zones=support if usable else [], resistance_zones=resist if usable else [], notes=notes, **base)


def _oi_map(snap: ChainSnapshot) -> dict[tuple[float, str], tuple[float | None, float | None]]:
    out = {}
    for r in snap.rows:
        for side, leg in (("CE", r.ce), ("PE", r.pe)):
            if leg is not None:
                out[(r.strike, side)] = (None if leg.oi is None else float(leg.oi), None if leg.volume is None else float(leg.volume))
    return out


def observe_windows(history: list[ChainSnapshot], minutes: Iterable[int] = (1, 5, 15, 60), strikes_each_side: int = 10) -> dict[str, dict]:
    """Change over each look-back window between the latest snapshot and the one closest to (now − window).

    A window is reported only when a snapshot exists within max(30 s, 20 % of the window) of its start;
    otherwise it is INSUFFICIENT HISTORY. Nothing is interpolated.
    """
    out: dict[str, dict] = {}
    if not history:
        return {f"{m}m": {"status": "INSUFFICIENT HISTORY"} for m in minutes}
    latest = history[-1]
    a_now = analyze_chain(latest, strikes_each_side)
    window = set(s for s in [r.strike for r in latest.rows] if a_now.window and a_now.window[0] <= s <= a_now.window[1])
    for m in minutes:
        target = latest.ts - m * 60
        tol = max(30.0, 0.2 * m * 60)
        cands = [s for s in history[:-1] if abs(s.ts - target) <= tol]
        if not cands:
            out[f"{m}m"] = {"status": "INSUFFICIENT HISTORY", "needed_ts": target}
            continue
        base = min(cands, key=lambda s: abs(s.ts - target))
        a_then = analyze_chain(base, strikes_each_side)
        then_map, now_map = _oi_map(base), _oi_map(latest)
        deltas = []
        vol_delta = {"CE": 0.0, "PE": 0.0}
        for key, (oi, vol) in now_map.items():
            if key[0] not in window or key not in then_map:
                continue
            oi0, vol0 = then_map[key]
            if oi is not None and oi0 is not None:
                deltas.append((key[0], key[1], oi - oi0))
            if vol is not None and vol0 is not None:
                vol_delta[key[1]] += vol - vol0
        pos = [d for d in deltas if d[2] > 0]
        neg = [d for d in deltas if d[2] < 0]
        pcr_now = a_now.pcr_total_oi.value if a_now.pcr_total_oi else None
        pcr_then = a_then.pcr_total_oi.value if a_then.pcr_total_oi else None
        out[f"{m}m"] = {
            "status": "OK", "from_ts": base.ts, "to_ts": latest.ts,
            "max_buildup": ({"strike": max(pos, key=lambda d: d[2])[0], "side": max(pos, key=lambda d: d[2])[1], "oi_change": max(pos, key=lambda d: d[2])[2]} if pos else None),
            "max_unwinding": ({"strike": min(neg, key=lambda d: d[2])[0], "side": min(neg, key=lambda d: d[2])[1], "oi_change": min(neg, key=lambda d: d[2])[2]} if neg else None),
            "ce_oi_change": sum(d[2] for d in deltas if d[1] == "CE"), "pe_oi_change": sum(d[2] for d in deltas if d[1] == "PE"),
            "volume_change": vol_delta, "pcr_total_oi_then": pcr_then, "pcr_total_oi_now": pcr_now,
            "pcr_total_oi_change": None if pcr_now is None or pcr_then is None else round(pcr_now - pcr_then, 4),
        }
    return out


def days_to_expiry(expiry: dt.date, now: dt.datetime, close_hhmm: str = "15:30") -> float:
    hh, mm = (int(x) for x in close_hhmm.split(":"))
    from amrt.core.clock import IST
    exp_dt = dt.datetime(expiry.year, expiry.month, expiry.day, hh, mm, tzinfo=IST)
    return max(0.0, (exp_dt - now.astimezone(IST)).total_seconds() / 86400.0)
