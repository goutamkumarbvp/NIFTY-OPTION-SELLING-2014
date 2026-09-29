"""Specialist agents. Scores are on a [-1, +1] axis where +1 means "conditions
strongly favour deploying an option-selling structure now"."""
from __future__ import annotations

import datetime as dt
import math
from typing import List

from terminal.agents.base import Agent, Assessment, MarketContext
from terminal.core.clock import now_ist
from terminal.core.models import OptionType


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class MarketAnalystAgent(Agent):
    name = "MarketAnalyst"
    role = "Trend & regime classification (EMA, RSI, VWAP, supertrend, ATR)"
    weight = 1.0

    async def assess(self, ctx: MarketContext) -> Assessment:
        ind = ctx.indicators
        findings: List[str] = []
        if not ind or ind.get("candles", 0) < 3:
            return Assessment(agent=self.name, role=self.role, stance="WARMING_UP", score=0.0, confidence=0.1, findings=["insufficient candles"])
        trend, r, st = ind.get("trend"), ind.get("rsi"), ind.get("supertrend")
        ret5 = ind.get("ret_5m_pct") or 0.0
        atr_pct = (ind.get("atr") or 0) / ctx.spot * 100 if ctx.spot else 0
        regime = "RANGE"
        score = 0.6
        if trend == "UP" and st == "UP":
            regime = "TRENDING_UP"
            score = 0.25
        elif trend == "DOWN" and st == "DOWN":
            regime = "TRENDING_DOWN"
            score = 0.15
        if abs(ret5) > 0.35 or atr_pct > 0.18:
            regime = "VOLATILE"
            score = -0.4
        if r is not None:
            if r > 72 or r < 28:
                findings.append(f"RSI {r:.0f} extreme → mean reversion likely, favours premium selling with skew")
                score += 0.1
            else:
                findings.append(f"RSI {r:.0f} neutral")
        findings.append(f"EMA9/21 trend {trend}, supertrend {st}, 5m return {ret5:+.2f}%, ATR {atr_pct:.2f}% of spot")
        vwap = ind.get("vwap")
        if vwap:
            findings.append(f"Spot {'above' if ctx.spot >= vwap else 'below'} VWAP ({vwap:.0f})")
        conf = 0.55 + min(0.35, ind.get("candles", 0) / 60)
        return Assessment(agent=self.name, role=self.role, stance=regime, score=_clamp(score), confidence=round(conf, 2), findings=findings,
                          data={"regime": regime, "rsi": r, "trend": trend, "supertrend": st, "atr_pct": round(atr_pct, 3)})


class VolatilityAgent(Agent):
    name = "VolatilityAgent"
    role = "IV rank, IV vs realised, expected move, premium richness"
    weight = 1.3

    async def assess(self, ctx: MarketContext) -> Assessment:
        findings: List[str] = []
        if ctx.chain is None:
            return Assessment(agent=self.name, role=self.role, stance="NO_CHAIN", score=0.0, confidence=0.0)
        iv = ctx.chain.iv_atm
        rv = ctx.indicators.get("realized_vol")
        rank = ctx.vix_rank.get("rank")
        score = 0.0
        if rv:
            spread = iv - rv
            findings.append(f"ATM IV {iv:.1f}% vs realised {rv:.1f}% → {'rich' if spread > 0 else 'cheap'} premium ({spread:+.1f} vol pts)")
            score += _clamp(spread / 6.0, -0.6, 0.7)
        else:
            findings.append(f"ATM IV {iv:.1f}%, realised vol not yet available")
        if rank is not None and ctx.exchange != "MCX":
            findings.append(f"VIX {ctx.vix:.2f} rank {rank:.0f}/100 (session range)")
            score += (rank - 40) / 100.0
        em = ctx.chain.expected_move
        findings.append(f"Expected move to expiry ±{em:.0f} pts ({em / ctx.spot * 100:.2f}%), {ctx.days_to_expiry:.2f} days left")
        if ctx.days_to_expiry < 0.2:
            findings.append("Very close to expiry: gamma risk dominates theta")
            score -= 0.35
        stance = "SELL_PREMIUM" if score > 0.15 else ("NEUTRAL" if score > -0.2 else "AVOID_SELLING")
        conf = 0.5 + (0.3 if rv else 0.0) + (0.15 if rank is not None else 0.0)
        return Assessment(agent=self.name, role=self.role, stance=stance, score=_clamp(score), confidence=round(min(conf, 0.95), 2), findings=findings,
                          data={"iv_atm": iv, "realized_vol": rv, "vix_rank": rank, "expected_move": em})


class OptionsFlowAgent(Agent):
    name = "OptionsFlow"
    role = "PCR, OI build-up, max pain, support/resistance walls"
    weight = 1.0

    async def assess(self, ctx: MarketContext) -> Assessment:
        ch = ctx.chain
        if ch is None:
            return Assessment(agent=self.name, role=self.role, stance="NO_CHAIN", score=0.0, confidence=0.0)
        findings: List[str] = []
        pcr = ch.pcr
        bias = "NEUTRAL"
        if pcr > 1.25:
            bias = "BULLISH (put writers confident)"
        elif pcr < 0.75:
            bias = "BEARISH (call writers dominant)"
        findings.append(f"PCR(OI) {pcr:.2f}, PCR(vol) {ch.pcr_volume:.2f} → {bias}")
        top_ce = max(ch.rows, key=lambda r: r.ce.oi)
        top_pe = max(ch.rows, key=lambda r: r.pe.oi)
        findings.append(f"Resistance wall {top_ce.strike:.0f} (CE OI {top_ce.ce.oi/1e5:.1f}L), support wall {top_pe.strike:.0f} (PE OI {top_pe.pe.oi/1e5:.1f}L)")
        findings.append(f"Max pain {ch.max_pain:.0f} ({(ch.max_pain / ch.spot - 1) * 100:+.2f}% from spot)")
        width = (top_ce.strike - top_pe.strike) / ch.spot * 100
        inside = top_pe.strike < ch.spot < top_ce.strike
        score = 0.3 if inside else -0.3
        score += _clamp((width - 1.5) / 4.0, -0.3, 0.3)
        if len(ctx.pcr_history) >= 3:
            d = ch.pcr - ctx.pcr_history[-3]["pcr"]
            findings.append(f"PCR drift {d:+.3f} over last readings")
            if abs(d) > 0.15:
                score -= 0.15
        stance = "RANGE_SUPPORTED" if inside and width > 1.5 else ("NARROW_RANGE" if inside else "OUTSIDE_WALLS")
        return Assessment(agent=self.name, role=self.role, stance=stance, score=_clamp(score), confidence=0.7, findings=findings,
                          data={"pcr": pcr, "support": top_pe.strike, "resistance": top_ce.strike, "max_pain": ch.max_pain, "bias": bias})


class EventRiskAgent(Agent):
    name = "EventRisk"
    role = "Scheduled events, expiry-day dynamics, session timing"
    weight = 1.2

    # illustrative macro calendar (IST dates); extend via runtime/events.json
    CALENDAR = [
        {"date": "2026-10-01", "name": "RBI MPC decision", "impact": "HIGH"},
        {"date": "2026-10-28", "name": "US FOMC decision", "impact": "HIGH"},
        {"date": "2026-11-12", "name": "India CPI", "impact": "MEDIUM"},
        {"date": "2026-12-04", "name": "RBI MPC decision", "impact": "HIGH"},
        {"date": "2026-12-16", "name": "US FOMC decision", "impact": "HIGH"},
    ]

    async def assess(self, ctx: MarketContext) -> Assessment:
        findings: List[str] = []
        score = 0.2
        today = now_ist().date()
        veto = False
        events = ctx.events or self.CALENDAR
        for ev in events:
            try:
                d = dt.date.fromisoformat(ev["date"])
            except Exception:
                continue
            delta = (d - today).days
            if 0 <= delta <= 1:
                findings.append(f"{ev['name']} ({ev.get('impact','MEDIUM')}) {'today' if delta == 0 else 'tomorrow'}")
                if ev.get("impact") == "HIGH":
                    score -= 0.5
                    if delta == 0:
                        veto = True
                else:
                    score -= 0.2
        if ctx.is_expiry_day:
            findings.append("Expiry day: fast theta decay but pin/gamma risk. Prefer defined-risk or reduced size.")
            score -= 0.1
        if not ctx.can_enter:
            findings.append(f"Entry window closed: {ctx.can_enter_reason}")
            score = -1.0
        if not findings:
            findings.append("No high-impact scheduled events in the next 24h")
        stance = "VETO" if veto else ("CAUTION" if score < 0 else "CLEAR")
        return Assessment(agent=self.name, role=self.role, stance=stance, score=_clamp(score), confidence=0.85, findings=findings, veto=veto)


class SentinelAgent(Agent):
    name = "Sentinel"
    role = "Black-swan / anomaly detection, feed & broker health, protective actions"
    weight = 2.0

    async def assess(self, ctx: MarketContext) -> Assessment:
        s = self.t.settings
        findings: List[str] = []
        veto = False
        score = 0.3
        protect = []
        if not ctx.feed_fresh:
            findings.append("Market feed STALE — no new entries")
            veto = True
        sm = ctx.sigma_move_5m
        if sm is not None:
            findings.append(f"{s.black_swan_window_minutes}m move = {sm:+.2f}σ of expected")
            if abs(sm) >= s.black_swan_sigma:
                findings.append(f"BLACK SWAN candidate: |{sm:.1f}σ| ≥ {s.black_swan_sigma}σ")
                veto = True
                protect.append("FLATTEN_SHORT_GAMMA")
            elif abs(sm) >= s.black_swan_sigma * 0.6:
                score -= 0.5
        vix_chg = None
        vt = self.t.processor.last_tick("INDIAVIX")
        if vt and vt.prev_close:
            vix_chg = (vt.ltp / vt.prev_close - 1) * 100
            if vix_chg >= s.vix_spike_pct:
                findings.append(f"VIX spike {vix_chg:+.1f}% today")
                veto = True
                protect.append("HALT_NEW_ENTRIES")
        if ctx.risk.level.value in ("RED", "HALTED"):
            findings.append(f"Risk level {ctx.risk.level.value}: {', '.join(ctx.risk.breaches) or 'halted'}")
            veto = True
        if not self.t.broker.connected:
            findings.append("Broker not connected")
            veto = True
        if not findings:
            findings.append("No anomalies. Feed fresh, broker connected, risk within limits.")
        stance = "PROTECT" if protect else ("VETO" if veto else "CLEAR")
        return Assessment(agent=self.name, role=self.role, stance=stance, score=_clamp(-1.0 if veto else score), confidence=0.95, findings=findings, veto=veto,
                          data={"protect": protect, "sigma_move": sm, "vix_change_pct": vix_chg})


class RiskAgent(Agent):
    name = "RiskGuardian"
    role = "Independent capital-at-risk audit: loss budget, margin, greeks, concentration"
    weight = 1.6

    async def assess(self, ctx: MarketContext) -> Assessment:
        r = ctx.risk
        findings: List[str] = []
        score = 0.4
        veto = False
        if not r.safety_gate_open:
            findings.append("Safety gate CLOSED — entries blocked until opened by operator")
            veto = True
        if r.kill_switch:
            findings.append("Kill switch engaged")
            veto = True
        findings.append(f"Daily P&L ₹{r.daily_pnl:,.0f}; loss budget used {r.loss_budget_used_pct:.0f}%")
        if r.loss_budget_used_pct >= 70:
            score -= 0.6
            findings.append("Loss budget >70% used: no new risk")
            veto = True
        elif r.loss_budget_used_pct >= 40:
            score -= 0.3
        findings.append(f"Margin utilisation {r.margin_utilisation_pct:.0f}% ({r.open_lots} lots, {r.open_positions} positions)")
        if r.margin_utilisation_pct > self.t.risk.limits["max_margin_utilisation_pct"] * 0.9:
            score -= 0.5
            findings.append("Margin near cap")
        if abs(r.portfolio_delta) > self.t.risk.limits["portfolio_delta_limit"] * 0.8:
            findings.append(f"Portfolio delta {r.portfolio_delta:+.0f} near limit — prefer delta-opposing structure")
            score -= 0.2
        active = [x for x in ctx.active_runs if x.underlying == ctx.underlying]
        if active:
            findings.append(f"{len(active)} active run(s) already on {ctx.underlying}")
            score -= 0.5
        stance = "VETO" if veto else ("CAUTION" if score < 0 else "WITHIN_LIMITS")
        return Assessment(agent=self.name, role=self.role, stance=stance, score=_clamp(score), confidence=0.9, findings=findings, veto=veto)


class StrategySelectorAgent(Agent):
    name = "StrategySelector"
    role = "Chooses structure, strikes and size from regime + vol + flow + memory"
    weight = 1.0

    async def assess(self, ctx: MarketContext) -> Assessment:
        ch = ctx.chain
        if ch is None:
            return Assessment(agent=self.name, role=self.role, stance="NO_CHAIN", score=0.0, confidence=0.0)
        regime = ctx.memory.get("regime", "RANGE")
        vol_ok = ctx.memory.get("vol_score", 0.0)
        flow = ctx.memory.get("flow", {})
        enabled = self.t.strategies.enabled
        stats = ctx.memory.get("strategy_stats", {})
        candidates: List[tuple] = []
        naked_ok = bool(self.t.risk.limits.get("naked_short_allowed", 1))
        defined_bias = 0.15 if (ctx.is_expiry_day or ctx.risk.loss_budget_used_pct > 40 or not naked_ok) else 0.0

        def add(key: str, base: float, why: str) -> None:
            if not enabled.get(key, True):
                return
            spec = self.t.strategies.config
            from terminal.strategy.library import SPECS
            sp = SPECS[key]
            if not naked_ok and not sp.defined_risk:
                return
            hist = stats.get(f"{key}:{regime}") or stats.get(key) or {}
            wr = hist.get("win_rate")
            n = hist.get("n", 0)
            learn = ((wr - 0.5) * 0.6) if wr is not None and n >= 3 else 0.0
            score = base + (defined_bias if sp.defined_risk else 0.0) + learn
            candidates.append((score, key, why + (f"; learned win-rate {wr:.0%} over {n} trades" if wr is not None and n >= 3 else "")))

        if regime == "RANGE":
            add("short_strangle", 0.75 + 0.2 * vol_ok, "range regime: harvest theta both sides at ~15Δ")
            add("iron_condor", 0.7 + 0.1 * vol_ok, "range regime with defined risk")
            add("short_straddle", 0.55 + 0.3 * vol_ok, "range + rich IV: maximum theta at ATM")
            add("iron_fly", 0.5 + 0.2 * vol_ok, "range, defined-risk straddle")
        elif regime == "TRENDING_UP":
            add("bull_put_spread", 0.75, "uptrend: sell puts under support with protection")
            add("jade_lizard", 0.6, "uptrend: no upside risk structure")
            add("short_strangle", 0.35, "trend may fade; wide strangle")
        elif regime == "TRENDING_DOWN":
            add("bear_call_spread", 0.75, "downtrend: sell calls above resistance with protection")
            add("iron_condor", 0.45, "defined risk if trend stalls")
        else:  # VOLATILE
            add("iron_condor", 0.4, "volatile: defined risk only, wider wings")
            add("short_strangle", 0.2 + 0.3 * vol_ok, "volatile but rich IV: far strikes only")
        if flow.get("bias", "").startswith("BULLISH"):
            candidates = [(s + (0.1 if k in ("bull_put_spread", "jade_lizard") else 0), k, w) for s, k, w in candidates]
        if flow.get("bias", "").startswith("BEARISH"):
            candidates = [(s + (0.1 if k == "bear_call_spread" else 0), k, w) for s, k, w in candidates]
        if not candidates:
            return Assessment(agent=self.name, role=self.role, stance="NO_STRATEGY", score=-0.5, confidence=0.5, findings=["no enabled strategy fits current regime"])
        candidates.sort(reverse=True)
        best_score, key, why = candidates[0]
        lots = self._size(ctx)
        findings = [f"Selected {key} ({why})", f"Size {lots} lot(s) from loss budget & margin headroom"]
        alt = ", ".join(f"{k}:{s:.2f}" for s, k, _ in candidates[1:3])
        if alt:
            findings.append(f"Alternatives: {alt}")
        return Assessment(agent=self.name, role=self.role, stance=key.upper(), score=_clamp(best_score), confidence=0.7, findings=findings,
                          data={"strategy": key, "lots": lots, "candidates": [[k, round(s, 3)] for s, k, _ in candidates]})

    def _size(self, ctx: MarketContext) -> int:
        s = self.t.settings
        lim = self.t.risk.limits
        budget_left = max(0.0, lim["max_daily_loss"] * (1 - ctx.risk.loss_budget_used_pct / 100.0))
        u = self.t.universe.get(ctx.underlying)
        # assume worst realistic loss per lot = SL% of credit ≈ 1.2% of notional
        per_lot_risk = ctx.spot * u.lot_size * 0.012
        by_budget = int(budget_left / per_lot_risk) if per_lot_risk else 1
        margin_left = max(0.0, s.capital * lim["max_margin_utilisation_pct"] / 100.0 - ctx.risk.margin_used)
        by_margin = int(margin_left / max(ctx.spot * u.lot_size * 0.07, 1))
        lots = max(1, min(by_budget, by_margin, int(lim["max_lots_per_order"]), s.default_lots * 2))
        return lots


class ExecutionAgent(Agent):
    name = "ExecutionTactician"
    role = "Liquidity / spread check and execution tactic"
    weight = 0.8

    async def assess(self, ctx: MarketContext) -> Assessment:
        ch = ctx.chain
        if ch is None:
            return Assessment(agent=self.name, role=self.role, stance="NO_CHAIN", score=0.0, confidence=0.0)
        atm = next((r for r in ch.rows if r.strike == ch.atm_strike), None)
        findings: List[str] = []
        score = 0.3
        if atm:
            spread_pct = (atm.ce.ask - atm.ce.bid) / max(atm.ce.ltp, 0.05) * 100
            findings.append(f"ATM CE spread {spread_pct:.1f}% of premium, volume {atm.ce.volume/1e3:.0f}K")
            if spread_pct > 4:
                score -= 0.4
                findings.append("Wide spreads: use limit orders at mid, slice lots")
        t = now_ist().time()
        if ctx.exchange != "MCX" and (t < dt.time(9, 30)):
            findings.append("Opening volatility window: wait for 9:30 price discovery")
            score -= 0.3
        tactic = "LIMIT_AT_MID" if score < 0.2 else "MARKET_SMALL_SIZE"
        findings.append(f"Tactic: {tactic}")
        return Assessment(agent=self.name, role=self.role, stance=tactic, score=_clamp(score), confidence=0.6, findings=findings, data={"tactic": tactic})


class ReviewAgent(Agent):
    name = "PostTradeReviewer"
    role = "Learns from closed trades; maintains strategy × regime statistics"
    weight = 0.5

    async def assess(self, ctx: MarketContext) -> Assessment:
        db = self.t.db
        stats = db.memory_get("strategy_stats", {}) or {}
        seen = set(db.memory_get("reviewed_runs", []) or [])
        new = 0
        for run in list(self.t.strategies.runs.values()):
            if run.status != "CLOSED" or run.id in seen:
                continue
            regime = (run.notes[0].split("regime=")[1].split()[0] if run.notes and "regime=" in run.notes[0] else ctx.memory.get("regime", "RANGE"))
            for key in (run.strategy, f"{run.strategy}:{regime}"):
                st = stats.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0})
                st["n"] += 1
                st["wins"] += 1 if run.realized_pnl > 0 else 0
                st["pnl"] = round(st["pnl"] + run.realized_pnl, 2)
                st["win_rate"] = round(st["wins"] / st["n"], 3)
            seen.add(run.id)
            new += 1
        if new:
            db.memory_set("strategy_stats", stats)
            db.memory_set("reviewed_runs", sorted(seen)[-500:])
        findings = [f"{new} newly reviewed trade(s)" if new else "no new closed trades"]
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["n"])[:4]:
            if ":" not in k:
                findings.append(f"{k}: {v['n']} trades, win-rate {v.get('win_rate', 0):.0%}, P&L ₹{v['pnl']:,.0f}")
        return Assessment(agent=self.name, role=self.role, stance="LEARNING", score=0.0, confidence=0.5, findings=findings, data={"stats": stats})
