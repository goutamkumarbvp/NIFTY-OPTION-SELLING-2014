"""Risk manager: pre-trade gates, live portfolio limits, safety gate, kill switch.

Hard limits apply in BOTH modes. AUTO and MANUAL only differ in who originates
entries; nothing can bypass this layer (agents, API, UI all go through it).
"""
from __future__ import annotations

import time
from typing import Dict, List

from terminal.core.models import Order, OrderSource, OrderStatus, RiskLevel, RiskSnapshot, Side, TerminalMode, TradePlan
from terminal.risk.portfolio import DEFAULT_LIMITS as PORTFOLIO_LIMITS
from terminal.risk.pretrade import DEFAULT_LIMITS as PRETRADE_LIMITS
from terminal.strategy.library import estimate_margin


class RiskManager:
    def __init__(self, terminal) -> None:
        self.t = terminal
        s = terminal.settings
        self.limits: Dict[str, float] = {
            "max_daily_loss": min(s.max_daily_loss, s.capital * s.max_daily_loss_pct / 100.0) if s.max_daily_loss else s.capital * s.max_daily_loss_pct / 100.0,
            "daily_profit_target": s.daily_profit_target,
            "max_lots_per_order": s.max_lots_per_order,
            "max_open_lots": s.max_open_lots,
            "max_open_lots_per_market": s.max_open_lots_per_market,
            "max_open_positions": s.max_open_positions,
            "max_margin_utilisation_pct": s.max_margin_utilisation_pct,
            "portfolio_delta_limit": s.portfolio_delta_limit,
            "portfolio_vega_limit": s.portfolio_vega_limit,
            "auto_max_trades_per_day": s.auto_max_trades_per_day,
            "naked_short_allowed": 1.0 if s.naked_short_allowed else 0.0,
            **PRETRADE_LIMITS,
            **PORTFOLIO_LIMITS,
        }
        self.limits["max_stress_loss"] = 2.0 * self.limits["max_daily_loss"]
        saved = terminal.db.get_setting("risk_limits")
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in self.limits:
                    try:
                        self.limits[k] = float(v)
                    except (TypeError, ValueError):
                        pass
        self.kill_switch = False
        self.safety_gate_open = bool(s.safety_gate_open_on_start)
        self.halted_reason = ""
        self.breaches: List[str] = []
        self.warnings: List[str] = []
        self.snapshot = RiskSnapshot(max_daily_loss=self.limits["max_daily_loss"])
        self.market_enabled: Dict[str, bool] = {m: True for m in s.market_list}
        self._halt_in_progress = False
        self.day = time.strftime("%Y-%m-%d")

    # ---------------------------------------------------------------- limits
    def update_limits(self, patch: Dict[str, float], actor: str) -> Dict[str, float]:
        changed = {}
        for k, v in patch.items():
            if k in self.limits and float(v) != self.limits[k]:
                self.t.db.config_change(f"risk_limits.{k}", self.limits[k], float(v), actor)
                changed[k] = {"old": self.limits[k], "new": float(v)}
                self.limits[k] = float(v)
        self.t.db.set_setting("risk_limits", self.limits)
        self.t.audit.record("RISK_LIMITS_UPDATED", changed or patch, actor)
        return self.limits

    # ---------------------------------------------------------------- margin
    def margin_used(self) -> float:
        """Broker-reported used margin when fresh (live), otherwise the SPAN-like estimate."""
        rec = getattr(self.t, "position_reconciler", None)
        if rec is not None and rec.margin.get("used") is not None and time.time() - rec.margin.get("ts", 0) < 180:
            return round(float(rec.margin["used"]), 0)
        return self.estimated_margin()

    def estimated_margin(self) -> float:
        total = 0.0
        by_run: Dict[str, float] = {}
        for run in self.t.strategies.runs.values():
            if run.status != "ACTIVE":
                continue
            spot = self.t.processor.last_price(run.underlying) or 0.0
            u = self.t.universe.get(run.underlying)
            by_run[run.id] = estimate_margin(run.legs, spot, u.lot_size, run.lots)
            total += by_run[run.id]
        # standalone (manual) positions not tied to a run
        for p in self.t.positions.open_positions():
            if p.strategy_run_id and p.strategy_run_id in by_run:
                continue
            spot = self.t.processor.last_price(p.underlying) or 0.0
            if p.net_qty < 0:
                hedged = any(o.net_qty > 0 and o.underlying == p.underlying and o.option_type == p.option_type and o.expiry == p.expiry for o in self.t.positions.open_positions())
                total += spot * abs(p.net_qty) * 0.07 * (0.4 if hedged else 1.0)
            else:
                total += p.avg_price * p.net_qty
        return round(total, 0)

    # ---------------------------------------------------------------- pre-trade
    def pre_trade(self, order: Order, reducing: bool = False) -> Dict[str, object]:
        reasons: List[str] = []
        s = self.t.settings
        if self.kill_switch and not reducing:
            reasons.append("KILL_SWITCH_ACTIVE")
        if not reducing:
            if not self.safety_gate_open:
                reasons.append("SAFETY_GATE_CLOSED")
            if self.halted_reason:
                reasons.append(f"HALTED:{self.halted_reason}")
            if not self.market_enabled.get(order.exchange.value, True):
                reasons.append(f"MARKET_DISABLED:{order.exchange.value}")
            if order.lots > self.limits["max_lots_per_order"]:
                reasons.append("MAX_LOTS_PER_ORDER")
            if self.t.positions.open_lots() + order.lots > self.limits["max_open_lots"]:
                reasons.append("MAX_OPEN_LOTS")
            mk = self.t.positions.lots_by_market().get(order.exchange.value, 0)
            if mk + order.lots > self.limits["max_open_lots_per_market"]:
                reasons.append(f"MAX_OPEN_LOTS_{order.exchange.value}")
            if order.symbol not in self.t.positions.positions and len(self.t.positions.open_positions()) >= self.limits["max_open_positions"]:
                reasons.append("MAX_OPEN_POSITIONS")
            if order.side == Side.SELL and not self.limits["naked_short_allowed"] and order.tag in ("", "manual"):
                reasons.append("NAKED_SHORT_DISABLED")
            u = self.t.universe.get(order.underlying)
            ok, why = self.t.scheduler.can_enter(u)
            if not ok:
                reasons.append(why)
            if order.source in (OrderSource.AUTO, OrderSource.STRATEGY) and self.t.orders.trades_today >= self.limits["auto_max_trades_per_day"] * 4:
                reasons.append("AUTO_TRADES_PER_DAY")
            spot = self.t.processor.last_price(order.underlying) or 0.0
            if order.side == Side.SELL:
                add_margin = spot * order.quantity * 0.07
                # a long option of the same type / expiry already held makes this a spread (SPAN offset)
                hedged = any(p.net_qty > 0 and p.underlying == order.underlying and p.option_type == order.option_type and p.expiry == order.expiry for p in self.t.positions.open_positions())
                if hedged:
                    add_margin *= 0.4
                cap = self._capital()
                if cap > 0 and (self.margin_used() + add_margin) / cap * 100 > self.limits["max_margin_utilisation_pct"]:
                    reasons.append("MARGIN_UTILISATION")
            dp = self.snapshot.daily_pnl
            if dp <= -self.limits["max_daily_loss"]:
                reasons.append("DAILY_LOSS_LIMIT")
        if order.lots <= 0:
            reasons.append("INVALID_LOTS")
        if self.t.env.value == "LIVE" and not s.live_allowed:
            reasons.append("LIVE_INTERLOCK")
        if not self.t.feed.is_fresh(s.feed_stale_seconds) and not reducing:
            reasons.append("FEED_STALE")
        if not reducing and any(b.startswith(("STRESS_LOSS", "CLUSTER_DELTA")) for b in self.breaches):
            reasons.append("PORTFOLIO_STRESS_BREACH")
        reasons += self.t.pretrade.check(order, reducing)
        return {"allowed": not reasons, "reasons": reasons}

    def check_plan(self, plan: TradePlan) -> Dict[str, object]:
        """Plan-level pre-check used by agents before proposing."""
        reasons: List[str] = []
        if self.kill_switch:
            reasons.append("KILL_SWITCH_ACTIVE")
        if not self.safety_gate_open:
            reasons.append("SAFETY_GATE_CLOSED")
        if self.halted_reason:
            reasons.append(f"HALTED:{self.halted_reason}")
        if not self.market_enabled.get(plan.exchange.value, True):
            reasons.append(f"MARKET_DISABLED:{plan.exchange.value}")
        ok, why = self.t.scheduler.can_enter(self.t.universe.get(plan.underlying))
        if not ok:
            reasons.append(why)  # e.g. OUTSIDE_OPERATING_HOURS / SESSION_CLOSED, before any leg is sent
        if not self.t.feed.is_fresh(self.t.settings.feed_stale_seconds):
            reasons.append("FEED_STALE")
        if any(b.startswith(("STRESS_LOSS", "CLUSTER_DELTA")) for b in self.breaches):
            reasons.append("PORTFOLIO_STRESS_BREACH")
        total_lots = sum(l.lots for l in plan.legs)
        if self.t.positions.open_lots() + total_lots > self.limits["max_open_lots"]:
            reasons.append("MAX_OPEN_LOTS")
        # positions are netted per symbol: a new structure must not touch a strike another run holds
        held = {p.symbol for p in self.t.positions.open_positions()}
        new_symbols = {l.symbol for l in plan.legs} - held
        if len(held) + len(new_symbols) > self.limits["max_open_positions"]:
            reasons.append("MAX_OPEN_POSITIONS")
        clash = [l.symbol for l in plan.legs if l.symbol in held]
        if clash:
            reasons.append("SYMBOL_ALREADY_IN_POSITION:" + ",".join(clash))
        if plan.lots > self.limits["max_lots_per_order"]:
            reasons.append("MAX_LOTS_PER_ORDER")
        cap = self._capital()
        if cap > 0 and (self.margin_used() + plan.margin_estimate) / cap * 100 > self.limits["max_margin_utilisation_pct"]:
            reasons.append("MARGIN_UTILISATION")
        if self.snapshot.daily_pnl <= -self.limits["max_daily_loss"]:
            reasons.append("DAILY_LOSS_LIMIT")
        if any(l.side == Side.SELL for l in plan.legs) and not self.limits["naked_short_allowed"]:
            spec_defined = all(any(x.side == Side.BUY and x.option_type == l.option_type for x in plan.legs) for l in plan.legs if l.side == Side.SELL)
            if not spec_defined:
                reasons.append("NAKED_SHORT_DISABLED")
        return {"allowed": not reasons, "reasons": reasons}

    def _capital(self) -> float:
        return float(self.t.settings.capital)

    # ---------------------------------------------------------------- monitor
    async def evaluate(self) -> RiskSnapshot:
        day = time.strftime("%Y-%m-%d")
        if day != self.day:
            self.day = day
            self.halted_reason = ""
            self.breaches = []
        pm = self.t.positions
        daily = pm.daily_pnl()
        max_loss = self.limits["max_daily_loss"]
        used_pct = (-daily / max_loss * 100) if daily < 0 and max_loss else 0.0
        margin = self.margin_used()
        cap = self._capital()
        util = margin / cap * 100 if cap else 0.0
        g = pm.greeks()
        breaches: List[str] = []
        warnings: List[str] = []
        if daily <= -max_loss:
            breaches.append("DAILY_LOSS_LIMIT")
        elif used_pct >= 70:
            warnings.append(f"LOSS_BUDGET_{int(used_pct)}%")
        if util > self.limits["max_margin_utilisation_pct"]:
            breaches.append("MARGIN_UTILISATION")
        elif util > self.limits["max_margin_utilisation_pct"] * 0.85:
            warnings.append("MARGIN_NEAR_LIMIT")
        if abs(g["delta"]) > self.limits["portfolio_delta_limit"]:
            warnings.append("PORTFOLIO_DELTA")
        if abs(g["vega"]) > self.limits["portfolio_vega_limit"]:
            warnings.append("PORTFOLIO_VEGA")
        if pm.open_lots() > self.limits["max_open_lots"]:
            breaches.append("MAX_OPEN_LOTS")
        if not self.t.feed.is_fresh(self.t.settings.feed_stale_seconds):
            warnings.append("FEED_STALE")
        try:
            pr = self.t.portfolio_risk.evaluate(g)
            breaches += pr["breaches"]
            warnings += pr["warnings"]
        except Exception:
            warnings.append("PORTFOLIO_RISK_UNAVAILABLE")
        target = self.limits.get("daily_profit_target", 0)
        if target and daily >= target and not self.halted_reason:
            warnings.append("PROFIT_TARGET_REACHED")
        level = RiskLevel.GREEN
        if self.kill_switch or self.halted_reason:
            level = RiskLevel.HALTED
        elif breaches:
            level = RiskLevel.RED
        elif warnings:
            level = RiskLevel.AMBER
        self.breaches, self.warnings = breaches, warnings
        self.snapshot = RiskSnapshot(level=level, daily_pnl=daily, realized_pnl=round(pm.realized_today, 2), unrealized_pnl=pm.unrealized(), max_daily_loss=max_loss,
                                     loss_budget_used_pct=round(used_pct, 1), margin_used=margin, margin_available=round(cap - margin, 0), margin_utilisation_pct=round(util, 1),
                                     open_lots=pm.open_lots(), open_positions=len(pm.open_positions()), portfolio_delta=g["delta"], portfolio_gamma=g["gamma"],
                                     portfolio_theta=g["theta"], portfolio_vega=g["vega"], kill_switch=self.kill_switch, safety_gate_open=self.safety_gate_open,
                                     breaches=breaches, warnings=warnings, per_market_lots=pm.lots_by_market(), trades_today=self.t.orders.trades_today,
                                     stress_worst_loss=self.t.portfolio_risk.last.get("worst_loss", 0.0), stress_worst_scenario=self.t.portfolio_risk.last.get("worst_scenario", ""),
                                     cluster_exposure={k: v["delta_notional"] for k, v in self.t.portfolio_risk.last.get("clusters", {}).items()})
        if "DAILY_LOSS_LIMIT" in breaches and not self.halted_reason and not self._halt_in_progress:
            await self.halt("DAILY_LOSS_LIMIT", actor="risk-manager")
        if target and daily >= target and not self.halted_reason and self.t.mode == TerminalMode.AUTO and pm.open_positions():
            await self.halt("PROFIT_TARGET_REACHED", actor="risk-manager")
        return self.snapshot

    async def halt(self, reason: str, actor: str = "system") -> None:
        """Hard stop: flatten everything, close the gate, pause the auto engine."""
        if self._halt_in_progress:
            return
        self._halt_in_progress = True
        try:
            self.halted_reason = reason
            self.safety_gate_open = False
            self._sync_flags()
            self.t.audit.record("RISK_HALT", {"reason": reason}, actor)
            self.t.log("CRITICAL", "risk", f"HALT: {reason} — flattening all positions and closing safety gate")
            await self.t.alerts.emit("CRITICAL", "risk", f"Trading halted: {reason}", "All positions flattened, safety gate closed, engine paused.")
            await self.t.orders.flatten_all(OrderSource.SENTINEL, actor, f"HALT:{reason}")
            self.t.paused = True
            await self.t.bus.publish("risk.halt", {"reason": reason})
        finally:
            self._halt_in_progress = False

    def _sync_flags(self) -> None:
        """Reflect gate / kill state into the last snapshot immediately (agents and API read it)."""
        self.snapshot.kill_switch = self.kill_switch
        self.snapshot.safety_gate_open = self.safety_gate_open
        if self.kill_switch or self.halted_reason:
            self.snapshot.level = RiskLevel.HALTED

    async def kill(self, actor: str) -> None:
        self.kill_switch = True
        self.safety_gate_open = False
        self._sync_flags()
        self.t.audit.record("KILL_SWITCH", {}, actor)
        self.t.log("CRITICAL", "risk", f"KILL SWITCH by {actor}: cancelling orders, flattening positions, switching to MANUAL")
        for o in list(self.t.orders.orders.values()):
            if o.status in (OrderStatus.PENDING_APPROVAL, OrderStatus.OPEN, OrderStatus.PENDING):
                try:
                    await self.t.orders.cancel(o.id, actor)
                except Exception:
                    pass
        await self.t.orders.flatten_all(OrderSource.KILL_SWITCH, actor, "KILL_SWITCH")
        await self.t.set_mode(TerminalMode.MANUAL, actor, reason="kill switch")
        self.t.paused = True
        await self.t.alerts.emit("CRITICAL", "risk", "KILL SWITCH ENGAGED", f"By {actor}. All positions flattened. Terminal in MANUAL, gate closed.")
        await self.t.bus.publish("risk.kill", {"actor": actor})

    def reset_kill(self, actor: str) -> None:
        self.kill_switch = False
        self.halted_reason = ""
        self._sync_flags()
        self.snapshot.level = RiskLevel.GREEN
        self.t.audit.record("KILL_SWITCH_RESET", {}, actor)

    def set_gate(self, open_: bool, actor: str) -> None:
        if open_ and self.kill_switch:
            raise ValueError("RESET_KILL_SWITCH_FIRST")
        self.safety_gate_open = open_
        self._sync_flags()
        self.t.audit.record("SAFETY_GATE_" + ("OPENED" if open_ else "CLOSED"), {}, actor)
        self.t.log("WARNING" if open_ else "INFO", "risk", f"Safety gate {'OPENED' if open_ else 'CLOSED'} by {actor}")

    def set_market(self, market: str, enabled: bool, actor: str) -> None:
        self.market_enabled[market.upper()] = enabled
        self.t.audit.record("MARKET_TOGGLE", {"market": market, "enabled": enabled}, actor)

    def describe(self) -> dict:
        return {"snapshot": self.snapshot.model_dump(mode="json"), "limits": self.limits, "halted_reason": self.halted_reason, "markets": self.market_enabled,
                "pretrade": self.t.pretrade.describe(), "portfolio": self.t.portfolio_risk.describe()}
