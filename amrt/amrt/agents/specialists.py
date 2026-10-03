"""Specialist agents (advisory only).

Stance is the agent's view on taking NEW risk now: SUPPORTIVE, NEUTRAL, CAUTION,
BLOCK, or INSUFFICIENT_DATA. Confidence figures are heuristic coverage scores,
not calibrated probabilities; each output says how it was computed.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics

from amrt.agents.base import Agent, AgentContext, AgentOutput, insufficient
from amrt.analytics.fii_dii import participant_series
from amrt.analytics.option_chain import INFERENCE_NOTE, days_to_expiry
from amrt.analytics.pricing import bs_greeks, implied_vol
from amrt.core.clock import IST
from amrt.core.enums import DataLabel, Stance
from amrt.security.identity import PrincipalKind

HEURISTIC = "heuristic coverage score, not a calibrated probability"


def _sv(x) -> dict | None:
    return None if x is None else x.model_dump()


# ---------------------------------------------------------------- 1. market
class MarketIntelligenceAgent(Agent):
    name = "market_intelligence"
    critical = True

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        if ctx.chain is None or ctx.chain.spot is None:
            return insufficient("underlying price unavailable")
        spot = ctx.chain.spot
        hist = [h for h in ctx.spot_history if h[1] and h[1] > 0]
        obs: dict = {"spot": spot, "history_points": len(hist)}
        for m in (5, 15, 60):
            target = ctx.now_ts - m * 60
            cands = [h for h in hist if abs(h[0] - target) <= max(30.0, 0.2 * m * 60)]
            obs[f"return_{m}m_pct"] = None if not cands else round((spot / min(cands, key=lambda h: abs(h[0] - target))[1] - 1) * 100, 3)
        sampled, last_ts = [], None
        for ts, px in hist:
            if last_ts is None or ts - last_ts >= 50:
                sampled.append((ts, px))
                last_ts = ts
        rets = [math.log(b[1] / a[1]) for a, b in zip(sampled, sampled[1:], strict=False)]
        rv = None
        if len(rets) >= 10:
            spacing_min = max(1.0, statistics.median(b[0] - a[0] for a, b in zip(sampled, sampled[1:], strict=False)) / 60)
            rv = round(statistics.pstdev(rets) * math.sqrt(252 * 375 / spacing_min) * 100, 2)
        obs["realized_vol_annual_pct"] = rv
        today = ctx.now_ist.date()
        day = [px for ts, px in hist if dt.datetime.fromtimestamp(ts, IST).date() == today] + [spot]
        obs["day_high"], obs["day_low"] = max(day), min(day)
        obs["day_range_pct"] = round((max(day) / min(day) - 1) * 100, 3)
        atm_iv = None
        a = ctx.analytics
        if a is not None and a.atm_strike is not None:
            row = next((r for r in ctx.chain.rows if r.strike == a.atm_strike), None)
            ivs = []
            if row is not None:
                t = days_to_expiry(ctx.chain.expiry, ctx.now_ist) / 365.0
                for leg, is_call in ((row.ce, True), (row.pe, False)):
                    if leg is None:
                        continue
                    if leg.iv:
                        ivs.append(leg.iv)
                    elif leg.ltp and t > 0:
                        v = implied_vol(leg.ltp, spot, row.strike, t, is_call)
                        if v:
                            ivs.append(v * 100)
            atm_iv = round(sum(ivs) / len(ivs), 2) if ivs else None
        obs["atm_iv_pct"] = atm_iv
        r60, r15 = obs["return_60m_pct"], obs["return_15m_pct"]
        inferences, warnings = [], []
        trending = r60 is not None and abs(r60) >= 0.6
        if trending:
            regime = "TRENDING_UP" if r60 > 0 else "TRENDING_DOWN"
        elif rv is not None and atm_iv is not None and rv > atm_iv * 1.1:
            regime = "REALIZED_ABOVE_IMPLIED"
        elif r60 is None:
            regime = "UNCLEAR"
        else:
            regime = "RANGE_BOUND"
        obs["regime"] = regime
        inferences.append(f"INFERENCE: regime {regime} from 60-minute return and realized vs implied volatility; past movement does not predict the next move.")
        if regime in ("TRENDING_UP", "TRENDING_DOWN", "REALIZED_ABOVE_IMPLIED") or (r15 is not None and abs(r15) >= 0.4):
            stance = Stance.CAUTION
            warnings.append("price moving faster than short-premium structures usually tolerate")
        elif regime == "RANGE_BOUND" and len(hist) >= 30:
            stance = Stance.SUPPORTIVE
        else:
            stance = Stance.NEUTRAL
        if len(hist) < 30:
            warnings.append(f"only {len(hist)} price points in history; returns and volatility are partial")
        return {"status": "OK" if len(hist) >= 30 else "DEGRADED", "stance": stance, "confidence": round(0.3 + 0.4 * min(1.0, len(hist) / 60), 3),
                "confidence_method": f"0.3 + 0.4 × min(1, history points ÷ 60); {HEURISTIC}", "observations": obs, "inferences": inferences, "warnings": warnings,
                "assumptions": ["annualisation uses 252 sessions × 375 minutes", "ATM IV from source IV when published, else Black-76-style implied vol at r=6.5%"],
                "invalidation": ["a 15-minute move ≥ 0.4 %", "realized volatility rising above ATM implied volatility"]}


# ---------------------------------------------------------------- 2. chain
class OptionChainIntelligenceAgent(Agent):
    name = "option_chain_intelligence"
    critical = True

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        a = ctx.analytics
        if a is None or a.status == "DATA UNAVAILABLE":
            return insufficient("option chain unavailable or below 50 % OI coverage")
        obs = {"status": a.status, "atm_strike": a.atm_strike, "strike_step": a.strike_step, "oi_coverage_pct": a.oi_coverage_pct,
               "ce_max_oi": _sv(a.ce_max_oi), "ce_second_oi": _sv(a.ce_second_oi), "pe_max_oi": _sv(a.pe_max_oi), "pe_second_oi": _sv(a.pe_second_oi),
               "max_buildup": _sv(a.max_buildup), "max_unwinding": _sv(a.max_unwinding), "concentration": a.concentration,
               "support_zones": [z.model_dump() for z in a.support_zones], "resistance_zones": [z.model_dump() for z in a.resistance_zones],
               "missing_strikes": a.missing_strikes, "analytics_version": a.version}
        inferences, warnings = [INFERENCE_NOTE], list(a.notes)
        stance = Stance.NEUTRAL
        if a.status == "INCOMPLETE":
            stance = Stance.CAUTION
            warnings.append("chain incomplete inside ATM ± window")
        elif a.ce_max_oi and a.pe_max_oi and a.spot is not None:
            lo, hi = a.pe_max_oi.strike, a.ce_max_oi.strike
            obs["max_oi_range"] = [lo, hi]
            step = a.strike_step or 1.0
            if lo >= hi:
                pin = (lo + hi) / 2
                far = abs(a.spot - pin) > 2 * step
                inferences.append(f"INFERENCE: max CE and PE OI sit at {hi:g}/{lo:g} — positioning concentrated around {pin:g} rather than bounding a range"
                                  + ("; spot has moved more than two strikes away." if far else "."))
                stance = Stance.CAUTION if far else Stance.NEUTRAL
            elif lo <= a.spot <= hi:
                inferences.append(f"INFERENCE: spot {a.spot:g} lies inside the max-OI range {lo:g}–{hi:g}; such ranges often coincide with short-option positioning.")
                width_steps = (hi - lo) / step
                stance = Stance.SUPPORTIVE if width_steps >= 2 else Stance.NEUTRAL
            else:
                inferences.append(f"INFERENCE: spot {a.spot:g} is outside the max-OI range {lo:g}–{hi:g}; positioning walls have been crossed.")
                stance = Stance.CAUTION
        return {"status": "OK" if a.status == "OK" else "DEGRADED", "stance": stance,
                "confidence": round(0.5 * a.oi_coverage_pct / 100 + (0.1 if a.status == "OK" else 0.0), 3),
                "confidence_method": f"0.5 × OI coverage + 0.1 when the window is complete; {HEURISTIC}", "observations": obs,
                "inferences": inferences, "warnings": warnings, "assumptions": ["OI as published by the source at snapshot time"],
                "invalidation": ["spot crossing the max CE or max PE OI strike", "large unwinding at the max-OI strikes"]}


# ---------------------------------------------------------------- 3. PCR
class PCRPositioningAgent(Agent):
    name = "pcr_positioning"

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        a = ctx.analytics
        if a is None or a.pcr_total_oi is None or a.pcr_total_oi.value is None:
            return insufficient("PCR undefined (chain unavailable, incomplete or zero CE OI)")
        obs = {"pcr_total_oi": a.pcr_total_oi.model_dump(), "pcr_change_oi": _sv(a.pcr_change_oi),
               "pcr_total_oi_full_chain": _sv(a.pcr_total_oi_full), "pcr_change_oi_full_chain": _sv(a.pcr_change_oi_full), "windows": {}}
        for k, w in (ctx.windows or {}).items():
            obs["windows"][k] = {"status": w.get("status"), "pcr_then": w.get("pcr_total_oi_then"), "pcr_now": w.get("pcr_total_oi_now"),
                                 "change": w.get("pcr_total_oi_change")}
        v = a.pcr_total_oi.value
        warnings, inferences = [], [f"INFERENCE: PCR is a positioning ratio; {INFERENCE_NOTE}"]
        stance = Stance.NEUTRAL
        if v < 0.6 or v > 1.5:
            stance = Stance.CAUTION
            warnings.append(f"total-OI PCR {v:.2f} is outside 0.6–1.5 (one-sided positioning)")
        w15 = (ctx.windows or {}).get("15m", {})
        if w15.get("status") == "OK" and w15.get("pcr_total_oi_change") is not None and abs(w15["pcr_total_oi_change"]) >= 0.15:
            stance = Stance.CAUTION
            warnings.append(f"PCR moved {w15['pcr_total_oi_change']:+.2f} in 15 minutes")
        have = sum(1 for w in (ctx.windows or {}).values() if w.get("status") == "OK")
        return {"status": "OK" if have else "DEGRADED", "stance": stance, "confidence": round(0.3 + 0.1 * have, 3),
                "confidence_method": f"0.3 + 0.1 per look-back window with history; {HEURISTIC}", "observations": obs, "inferences": inferences,
                "warnings": warnings + ([] if have else ["no look-back history yet; trend in PCR unknown"]),
                "invalidation": ["PCR leaving the 0.6–1.5 band", "PCR change of ≥ 0.15 in 15 minutes"]}


# ---------------------------------------------------------------- 4. FII/DII
class FIIDIIAgent(Agent):
    name = "fii_dii"
    uses_market_data = False
    MAX_AGE_DAYS = 4

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        if not ctx.fii_participant and not ctx.fii_cash:
            return insufficient("no official FII/DII data imported (NSE participant OI / FII-DII cash)")
        obs: dict = {}
        warnings: list[str] = []
        latest = None
        if ctx.fii_participant:
            series = participant_series(ctx.fii_participant, "FII")
            if series:
                last = series[-1]
                latest = last["date"]
                obs["participant_oi"] = {"date": last["date"], "net": last["net"], "change_vs_previous": last["change_vs_previous"], "days": len(series)}
        if ctx.fii_cash:
            fii = sorted((r for r in ctx.fii_cash if r["category"] == "FII"), key=lambda r: r["date"])
            dii = sorted((r for r in ctx.fii_cash if r["category"] == "DII"), key=lambda r: r["date"])
            if fii:
                obs["fii_cash_latest"] = {k: fii[-1][k] for k in ("date", "buy_value", "sell_value", "net_value")}
                latest = max(latest or "", fii[-1]["date"])
            if dii:
                obs["dii_cash_latest"] = {k: dii[-1][k] for k in ("date", "buy_value", "sell_value", "net_value")}
        age = (ctx.now_ist.date() - dt.date.fromisoformat(latest)).days if latest else None
        obs["latest_date"], obs["age_days"] = latest, age
        if age is None or age > self.MAX_AGE_DAYS:
            return insufficient(f"FII/DII data stale (latest {latest})", observations=obs)
        inferences = ["INFERENCE: end-of-day participant positions describe exposure, not intent; profit or loss of participants is not computed."]
        stance = Stance.NEUTRAL
        po = obs.get("participant_oi")
        if po and po["change_vs_previous"] and po["change_vs_previous"].get("index_futures_net") is not None:
            ch = po["change_vs_previous"]["index_futures_net"]
            inferences.append(f"FII index-futures net position changed by {ch:+,.0f} contracts vs the previous file.")
            if abs(ch) >= 20000:
                stance = Stance.CAUTION
                warnings.append("large one-day change in FII index-futures positioning")
        return {"status": "OK", "stance": stance, "confidence": 0.3, "confidence_method": "fixed 0.3: end-of-day data, weak link to intraday risk",
                "observations": obs, "inferences": inferences, "warnings": warnings, "data_sources": ["NSE participant-wise OI", "NSE FII/DII cash"],
                "data_label": "OFFICIAL END-OF-DAY", "assumptions": ["figures as published; revisions replace earlier imports"],
                "invalidation": ["a new official file for the next trading day"]}


# ---------------------------------------------------------------- 6. exposure
class PortfolioExposureAgent(Agent):
    name = "portfolio_exposure"

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        val = ctx.valuation or {}
        rows = val.get("positions") or [p for p in ctx.positions if p.get("net_qty")]
        if not rows:
            return {"status": "OK", "stance": Stance.NEUTRAL, "confidence": 0.9, "confidence_method": "no open positions on this account",
                    "observations": {"open_positions": 0, "net_pnl_today": val.get("net_pnl_today")}}
        legs = {}
        if ctx.chain is not None:
            for r in ctx.chain.rows:
                for leg, ot in ((r.ce, "CE"), (r.pe, "PE")):
                    if leg is not None:
                        legs[leg.instrument_key] = (r.strike, ot, leg)
        spot = getattr(ctx.chain, "spot", None)
        t = days_to_expiry(ctx.chain.expiry, ctx.now_ist) / 365.0 if ctx.chain is not None else 0.0
        greeks = {"delta": 0.0, "gamma": 0.0, "theta_per_day": 0.0, "vega_per_vol_pt": 0.0}
        unknown, short_qty = [], 0
        for p in rows:
            q = p["net_qty"]
            short_qty += -q if q < 0 else 0
            info = legs.get(p["instrument_key"])
            if info is None or spot is None or t <= 0:
                unknown.append(p["instrument_key"])
                continue
            strike, ot, leg = info
            vol = (leg.iv / 100) if leg.iv else implied_vol(p.get("mark") or leg.ltp or 0, spot, strike, t, ot == "CE")
            if not vol:
                unknown.append(p["instrument_key"])
                continue
            g = bs_greeks(spot, strike, t, vol, ot == "CE")
            greeks["delta"] += g["delta"] * q
            greeks["gamma"] += g["gamma"] * q
            greeks["theta_per_day"] += g["theta"] * q
            greeks["vega_per_vol_pt"] += g["vega"] * q
        greeks = {k: round(v, 3) for k, v in greeks.items()}
        obs = {"open_positions": len(rows), "open_lots": val.get("open_lots"), "net_pnl_today": val.get("net_pnl_today"),
               "unrealized": val.get("unrealized"), "realized_today": val.get("realized_today"), "charges_today": val.get("charges_today"),
               "premium_exposure": val.get("premium_exposure"), "gross_notional": val.get("gross_notional"), "marks_missing": val.get("marks_missing", []),
               "portfolio_greeks": greeks if not unknown else None, "greeks_unknown_for": unknown, "short_quantity": short_qty}
        warnings, stance = [], Stance.NEUTRAL
        if val.get("marks_missing"):
            warnings.append("marks missing — P&L unavailable")
            stance = Stance.CAUTION
        if not unknown and short_qty and abs(greeks["delta"]) > 0.3 * short_qty:
            warnings.append(f"net delta {greeks['delta']:+.1f} exceeds 30 % of short quantity — position is directional")
            stance = Stance.CAUTION
        return {"status": "OK" if not unknown and not val.get("marks_missing") else "DEGRADED", "stance": stance,
                "confidence": 0.8 if not unknown else 0.4, "confidence_method": "model Greeks (Black-Scholes, r=6.5 %); lower when IV or marks are missing",
                "observations": obs, "warnings": warnings, "assumptions": ["Greeks are model estimates using source IV or implied vol from last price"],
                "invalidation": ["IV changes", "spot moves of more than one strike step"]}


# ---------------------------------------------------------------- 7. risk advisory
class RiskAdvisoryAgent(Agent):
    name = "risk_advisory"
    critical = True
    uses_market_data = False

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        pol = ctx.policy
        if pol is None:
            return {"status": "OK", "stance": Stance.BLOCK, "confidence": 1.0, "confidence_method": "rule: no approved risk policy",
                    "warnings": ["no approved risk policy for this account"]}
        val = ctx.valuation or {}
        pnl = val.get("net_pnl_today")
        has_pos = bool(val.get("positions"))
        blockers = list(ctx.safety.get("blockers", []))
        sod = (ctx.funds or {}).get("sod_funds")
        used = (ctx.funds or {}).get("margin_used")
        denom = sod if pol.get("margin_risk_denominator") == "SOD_ACCOUNT_FUNDS" else used
        loss = max(0.0, -pnl) if pnl is not None else None
        mr = None if loss is None or not denom else round(loss / denom * 100, 4)
        obs = {"net_pnl_today": pnl, "loss_inr": loss, "loss_level1_inr": pol["loss_level1_inr"], "loss_level2_inr": pol["loss_level2_inr"],
               "margin_risk_pct": mr, "margin_risk_denominator": pol.get("margin_risk_denominator"), "denominator_value": denom,
               "margin_risk_level1_pct": pol["margin_risk_level1_pct"], "margin_risk_level2_pct": pol["margin_risk_level2_pct"],
               "distance_to_level1_inr": None if loss is None else round(pol["loss_level1_inr"] - loss, 2),
               "safety_blockers": blockers, "emergency": ctx.safety.get("emergency"), "policy": ctx.policy_label}
        warnings = []
        stance = Stance.SUPPORTIVE
        if blockers:
            stance = Stance.BLOCK
            warnings.append("new risk blocked: " + ", ".join(blockers))
        if has_pos and pnl is None:
            stance = Stance.BLOCK
            warnings.append("P&L unavailable while positions are open")
        if loss is not None and (loss >= pol["loss_level1_inr"] or (mr is not None and mr >= pol["margin_risk_level1_pct"])):
            stance = Stance.BLOCK
            warnings.append("loss at or beyond level 1")
        elif loss is not None and loss >= 0.75 * pol["loss_level1_inr"] and stance != Stance.BLOCK:
            stance = Stance.CAUTION
            warnings.append("loss within 25 % of level 1")
        if denom is None and has_pos:
            warnings.append("margin-risk denominator unavailable; margin-risk trigger cannot be evaluated")
        return {"status": "OK", "stance": stance, "confidence": 1.0, "confidence_method": "deterministic comparison against the approved policy",
                "observations": obs, "warnings": warnings,
                "assumptions": ["thresholds are triggers, not guaranteed maximum losses; gaps and slippage can exceed them"],
                "invalidation": ["policy change", "P&L or funds update"]}


# ---------------------------------------------------------------- 8. broker health
class BrokerHealthAgent(Agent):
    name = "broker_health"
    uses_market_data = False

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        comps = [c for c in ctx.health if c.get("kind") in ("broker", "datasource")]
        rec = ctx.reconcile or {}
        obs = {"brokers": ctx.brokers, "components": [{k: c.get(k) for k in ("name", "state", "readiness", "reason", "quarantined")} for c in comps],
               "reconciliation": rec}
        warnings = []
        bad = [c["name"] for c in comps if c.get("quarantined") or c.get("state") in ("FAILED", "STALE", "QUARANTINED")]
        if bad:
            warnings.append("unhealthy: " + ", ".join(bad))
        stance = Stance.NEUTRAL
        if ctx.account_kind == "LIVE":
            b = (ctx.brokers or {}).get(ctx.account_id.split("-")[0].lower(), {})
            if not b.get("connected"):
                stance = Stance.BLOCK
                warnings.append("live broker session not connected")
            age = rec.get("age_s")
            if age is None or age > (ctx.policy or {}).get("max_reconcile_age_s", 60):
                stance = Stance.BLOCK
                warnings.append("reconciliation stale or never completed")
        if bad and stance == Stance.NEUTRAL:
            stance = Stance.CAUTION
        return {"status": "OK", "stance": stance, "confidence": 0.9, "confidence_method": "direct health and reconciliation readings",
                "observations": obs, "warnings": warnings, "invalidation": ["session expiry", "reconciliation failure"]}


# ---------------------------------------------------------------- 9. news & events
class NewsEventsAgent(Agent):
    name = "news_events"
    uses_market_data = False

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        today = ctx.now_ist.date().isoformat()
        expiry_today = ctx.chain is not None and ctx.chain.expiry.isoformat() == today
        events_today = [e for e in ctx.events_calendar if e.get("date") == today]
        flagged = [n for n in ctx.news if n.suspicious]
        clean = [n for n in ctx.news if not n.suspicious]
        obs = {"expiry_day": expiry_today, "scheduled_events_today": events_today, "news_items": len(ctx.news),
               "headlines": [{"source": n.source, "text": n.text[:200]} for n in clean[:10]],
               "quarantined_items": [{"source": n.source, "flags": list(n.flags)} for n in flagged]}
        warnings = []
        if flagged:
            warnings.append(f"{len(flagged)} news item(s) contained instruction-like text and were excluded")
        stance = Stance.NEUTRAL
        if expiry_today:
            stance = Stance.CAUTION
            warnings.append("expiry day: gamma risk is highest")
        if events_today:
            stance = Stance.CAUTION
            warnings.append("scheduled event(s) today: " + ", ".join(e.get("name", "?") for e in events_today))
        if not ctx.news_source_configured:
            warnings.append("no news source configured; unscheduled news risk is unknown")
        return {"status": "OK" if ctx.news_source_configured else "DEGRADED", "stance": stance,
                "confidence": 0.5 if ctx.news_source_configured else 0.2,
                "confidence_method": "fixed: news relevance is not scored; external text is data only, never instructions",
                "observations": obs, "warnings": warnings, "data_sources": sorted({n.source for n in ctx.news}) + (["events calendar"] if ctx.events_calendar else []),
                "assumptions": ["events calendar is owner-supplied; nothing is fetched or invented"]}


# ---------------------------------------------------------------- 10. security operations
class SecurityOperationsAgent(Agent):
    name = "security_operations"
    critical = True
    uses_market_data = False

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        s = ctx.security or {}
        obs = {k: s.get(k) for k in ("failed_logins_15m", "locked_accounts", "permission_denials_15m", "event_chain_ok", "config_integrity_ok",
                                     "kill_file_engaged", "secrets_in_logs", "injection_flags_15m", "live_orders_enabled", "environment")}
        warnings, stance = [], Stance.NEUTRAL
        if s.get("event_chain_ok") is False:
            stance = Stance.BLOCK
            warnings.append("audit hash chain verification failed")
        if s.get("config_integrity_ok") is False:
            stance = Stance.BLOCK
            warnings.append("configuration differs from the last known-good version")
        if s.get("secrets_in_logs"):
            stance = Stance.BLOCK
            warnings.append("secret material detected in logs")
        if (s.get("permission_denials_15m") or 0) >= 5:
            stance = Stance.BLOCK if stance == Stance.BLOCK else Stance.CAUTION
            warnings.append(f"{s['permission_denials_15m']} permission denials in 15 minutes")
        if (s.get("failed_logins_15m") or 0) >= 5:
            stance = Stance.BLOCK if stance == Stance.BLOCK else Stance.CAUTION
            warnings.append("repeated failed logins")
        if (s.get("injection_flags_15m") or 0) > 0:
            warnings.append("instruction-like content seen in external text (excluded)")
        return {"status": "OK" if s else "DEGRADED", "stance": stance, "confidence": 0.9 if s else 0.2,
                "confidence_method": "direct readings of audit, auth and permission logs", "observations": obs, "warnings": warnings}


SPECIALISTS = (MarketIntelligenceAgent, OptionChainIntelligenceAgent, PCRPositioningAgent, FIIDIIAgent, PortfolioExposureAgent,
               RiskAdvisoryAgent, BrokerHealthAgent, NewsEventsAgent, SecurityOperationsAgent)

__all__ = ["SPECIALISTS", "AgentOutput", "DataLabel", "PrincipalKind"] + [c.__name__ for c in SPECIALISTS]
