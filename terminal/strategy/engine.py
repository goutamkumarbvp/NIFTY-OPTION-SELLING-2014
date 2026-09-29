"""Strategy engine: deploys trade plans as strategy runs, monitors MTM, applies
stop-loss / target / trailing / time exits and adjustments."""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from terminal.core.models import Exchange, Leg, OptionType, Order, OrderSource, OrderStatus, Side, StrategyRun, TerminalMode, TradePlan
from terminal.strategy.library import SPECS, build_legs, estimate_margin, net_credit_per_lot, payoff_profile


class StrategyEngine:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.runs: Dict[str, StrategyRun] = {}
        self.plans: Dict[str, TradePlan] = {}
        self.config: Dict[str, dict] = {k: dict(v.params) for k, v in SPECS.items()}
        self.enabled: Dict[str, bool] = {k: True for k in SPECS}
        saved = self.t.db.get_setting("strategy_config")
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in self.config and isinstance(v, dict):
                    self.config[k].update({kk: float(vv) for kk, vv in v.items() if kk != "enabled"})
                    if "enabled" in v:
                        self.enabled[k] = bool(v["enabled"])

    # ------------------------------------------------------------ plan creation
    def make_plan(self, key: str, underlying: str, lots: int, expiry: Optional[str] = None, params: Optional[dict] = None,
                  rationale: Optional[List[str]] = None, confidence: float = 0.0, consensus: float = 0.0, votes: Optional[dict] = None,
                  source: OrderSource = OrderSource.AUTO) -> TradePlan:
        u = self.t.universe.get(underlying)
        chain = self.t.chain_for(underlying, expiry)
        p = {**self.config.get(key, {}), **(params or {})}
        legs = build_legs(key, chain, lots, p)
        credit = net_credit_per_lot(legs, u.lot_size)
        prof = payoff_profile(legs, chain.spot, u.lot_size)
        plan = TradePlan(strategy=key, underlying=underlying, exchange=u.exchange, expiry=chain.expiry, legs=legs, lots=lots, premium_collected=round(credit, 2),
                         max_profit=prof["max_profit"], max_loss=None if prof["undefined_risk"] else prof["max_loss"], margin_estimate=estimate_margin(legs, chain.spot, u.lot_size, lots),
                         stop_loss_pct=float(p.get("stop_loss_pct", self.t.settings.per_trade_stop_loss_pct)), target_pct=float(p.get("target_pct", self.t.settings.per_trade_target_pct)),
                         rationale=rationale or [], confidence=confidence, consensus=consensus, agent_votes=votes or {}, source=source, breakevens=prof["breakevens"])
        self.plans[plan.id] = plan
        self.t.db.save_plan(plan.model_dump(mode="json"))
        return plan

    # ------------------------------------------------------------ deployment
    async def deploy(self, plan: TradePlan, actor: str, source: Optional[OrderSource] = None) -> StrategyRun:
        src = source or plan.source
        check = self.t.risk.check_plan(plan)
        if not check["allowed"]:
            plan.status = "RISK_REJECTED"
            self.t.db.save_plan(plan.model_dump(mode="json"))
            raise ValueError("RISK_REJECTED: " + ", ".join(check["reasons"]))
        u = self.t.universe.get(plan.underlying)
        run = StrategyRun(strategy=plan.strategy, underlying=plan.underlying, exchange=plan.exchange, expiry=plan.expiry, lots=plan.lots, legs=[], premium_collected=0.0,
                          stop_loss_pct=plan.stop_loss_pct, target_pct=plan.target_pct, trailing_lock_pct=self.t.settings.trailing_lock_pct, source=src, plan_id=plan.id)
        # execute buys first (hedges) so margin benefit applies and risk is defined before shorts
        ordered = sorted(plan.legs, key=lambda l: 0 if l.side == Side.BUY else 1)
        filled_legs: List[Leg] = []
        for leg in ordered:
            order = self.t.orders.build(leg.symbol, leg.side, leg.lots, src, run_id=run.id, tag=plan.strategy, reason=f"{SPECS[plan.strategy].name} entry")
            order = await self.t.orders.submit(order, actor, protective=False)
            if order.status == OrderStatus.PENDING_APPROVAL:
                # should not happen: plan level approval already happened; treat as pending
                raise ValueError("ORDER_PENDING_APPROVAL")
            if order.status != OrderStatus.FILLED:
                # roll back the legs that did fill (each is a reducing order on its own symbol)
                for f in filled_legs:
                    back = self.t.orders.build(f.symbol, Side.BUY if f.side == Side.SELL else Side.SELL, f.lots, src, run_id=run.id, tag=plan.strategy, reason="entry rollback")
                    rb = await self.t.orders.submit(back, actor, protective=True)
                    if rb.status != OrderStatus.FILLED:
                        self.t.log("CRITICAL", "strategy", f"entry rollback of {f.symbol} failed: {rb.message}; check positions manually")
                        await self.t.alerts.emit("CRITICAL", "trade", f"Entry rollback failed: {f.symbol}", rb.message)
                plan.status = "FAILED"
                self.t.db.save_plan(plan.model_dump(mode="json"))
                raise ValueError(f"LEG_FAILED:{leg.symbol}:{order.message}")
            filled_legs.append(Leg(option_type=leg.option_type, side=leg.side, strike=leg.strike, lots=leg.lots, symbol=leg.symbol, entry_price=order.filled_price or leg.entry_price, ltp=order.filled_price or leg.entry_price, expiry=leg.expiry))
        run.legs = filled_legs
        run.premium_collected = round(net_credit_per_lot(filled_legs, u.lot_size) * plan.lots, 2)
        run.notes.append(f"entered by {actor} ({src.value}); credit {run.premium_collected}")
        self.runs[run.id] = run
        plan.status = "EXECUTED"
        self.t.db.save_plan(plan.model_dump(mode="json"))
        self.t.db.save_run(run.model_dump(mode="json"))
        self.t.audit.record("STRATEGY_DEPLOYED", run.model_dump(mode="json"), actor)
        self.t.log("INFO", "strategy", f"Deployed {SPECS[run.strategy].name} on {run.underlying} {run.expiry} x{run.lots} credit={run.premium_collected}")
        await self.t.alerts.emit("INFO", "trade", f"New trade: {SPECS[run.strategy].name} {run.underlying}", f"{run.lots} lot(s), credit ₹{run.premium_collected:,.0f}, SL {run.stop_loss_pct}% / target {run.target_pct}%", market=run.exchange.value)
        await self.t.bus.publish("strategy.deployed", run)
        return run

    async def exit_run(self, run_id: str, actor: str, reason: str, source: OrderSource = OrderSource.STRATEGY) -> StrategyRun:
        """Square off a run. Idempotent: a run already EXITING/CLOSED is left to the
        exit guard, so a second stop-loss layer never sends duplicate orders."""
        run = self.runs[run_id]
        if run.status != "ACTIVE":
            return run
        run.status = "EXITING"
        run.exit_reason = reason
        run.notes.append(f"exit requested: {reason} ({source.value})")
        self.t.audit.record("STRATEGY_EXIT_REQUESTED", {"id": run.id, "reason": reason}, actor)
        # buy back shorts before selling longs
        for leg in sorted(run.legs, key=lambda l: 0 if l.side == Side.SELL else 1):
            await self.t.exit_guard.request(leg.symbol, reason, source, run_id=run.id, actor=actor)
        await self.check_exiting()
        return run

    def _legs_flat(self, run: StrategyRun) -> bool:
        for leg in run.legs:
            pos = self.t.positions.positions.get(leg.symbol)
            if pos is not None and pos.net_qty != 0 and (pos.strategy_run_id in (None, run.id)):
                return False
        return True

    async def check_exiting(self) -> None:
        """Finalise runs whose legs are all flat (called after exits and by the guard tick)."""
        for run in list(self.runs.values()):
            if run.status == "EXITING" and self._legs_flat(run):
                reason = run.exit_reason or "EXIT"
                self.mark_closed(run, reason)
                self.t.audit.record("STRATEGY_EXITED", {"id": run.id, "reason": reason, "pnl": run.realized_pnl}, "strategy-engine")
                await self.t.alerts.emit("INFO" if run.realized_pnl >= 0 else "WARNING", "trade", f"Exit {SPECS[run.strategy].name} {run.underlying}: {reason}", f"P&L ₹{run.realized_pnl:,.0f}", market=run.exchange.value)
                await self.t.bus.publish("strategy.closed", run)

    def mark_closed(self, run: StrategyRun, reason: str) -> None:
        if run.status == "CLOSED":
            return
        run.status = "CLOSED"
        run.closed_at = time.time()
        run.exit_reason = reason
        booked = [t for t in self.t.db.trades(limit=2000) if t.get("strategy_run_id") == run.id]
        run.realized_pnl = round(sum(float(t["pnl"]) for t in booked), 2) if booked else round(run.mtm, 2)
        self.t.db.save_run(run.model_dump(mode="json"))
        self.t.log("INFO", "strategy", f"Closed {run.strategy} {run.underlying}: {reason} pnl={run.realized_pnl}")

    # ------------------------------------------------------------ monitoring
    def _run_mtm(self, run: StrategyRun) -> float:
        u = self.t.universe.get(run.underlying)
        mtm = 0.0
        for leg in run.legs:
            q = self.t.quote(leg.symbol)
            if q is None:
                continue
            leg.ltp = q.ltp
            sign = 1 if leg.side == Side.SELL else -1
            mtm += sign * (leg.entry_price - q.ltp) * u.lot_size * leg.lots
        return round(mtm, 2)

    async def monitor(self) -> None:
        """Called every processing cycle. Applies exit rules; adjustments in AUTO."""
        for run in list(self.runs.values()):
            if run.status != "ACTIVE":
                continue
            run.mtm = self._run_mtm(run)
            run.peak_mtm = max(run.peak_mtm, run.mtm)
            credit = abs(run.premium_collected) or 1.0
            sl_amt = credit * run.stop_loss_pct / 100.0
            tgt_amt = credit * run.target_pct / 100.0
            u = self.t.universe.get(run.underlying)
            reason = ""
            if run.mtm <= -sl_amt:
                reason = f"STOP_LOSS ({run.stop_loss_pct}% of credit)"
            elif run.mtm >= tgt_amt:
                reason = f"TARGET ({run.target_pct}% of credit)"
            elif run.peak_mtm >= tgt_amt * 0.5 and run.mtm <= run.peak_mtm * (run.trailing_lock_pct / 100.0):
                reason = f"TRAILING_LOCK (peak {run.peak_mtm:.0f})"
            elif self.t.scheduler.must_square_off(u):
                reason = "SQUARE_OFF_TIME"
            if reason:
                run.notes.append(f"stop-loss layer: strategy-engine → {reason}")
                await self.exit_run(run.id, "strategy-engine", reason)
                continue
            await self._maybe_adjust(run, u)
            if int(time.time()) % 15 == 0:
                self.t.db.save_run(run.model_dump(mode="json"))

    async def _maybe_adjust(self, run: StrategyRun, u) -> None:
        """Delta-based adjustment for short strangles/straddles: when a short leg's
        delta breaches 0.40 the *untested* side is rolled closer to collect more
        credit and re-centre the position. AUTO executes; MANUAL proposes."""
        if run.strategy not in ("short_strangle", "short_straddle") or run.adjustments >= 2:
            return
        shorts = [l for l in run.legs if l.side == Side.SELL]
        if len(shorts) != 2:
            return
        chain = self.t.chains.get(run.underlying)
        if chain is None or chain.expiry != run.expiry:
            return
        tested = None
        for l in shorts:
            q = self.t.quote(l.symbol)
            if q and abs(q.delta) >= 0.40:
                tested = l
        if tested is None:
            return
        untested = next(l for l in shorts if l is not tested)
        target_delta = 0.25
        new_q = chain.by_delta(untested.option_type, target_delta)
        if new_q is None or new_q.strike == untested.strike:
            return
        key = f"{run.id}:{untested.symbol}"
        if key in getattr(self, "_adjust_seen", set()):
            return
        self._adjust_seen = getattr(self, "_adjust_seen", set())
        self._adjust_seen.add(key)
        note = f"Adjust {SPECS[run.strategy].name}: {tested.option_type.value} {tested.strike:.0f} tested (|Δ|≥0.40). Roll {untested.option_type.value} {untested.strike:.0f} → {new_q.strike:.0f} (Δ≈{target_delta})."
        if self.t.mode != TerminalMode.AUTO:
            await self.t.alerts.emit("WARNING", "adjustment", f"Adjustment suggested: {run.underlying}", note, market=run.exchange.value)
            run.notes.append("suggested: " + note)
            return
        actor = "strategy-engine"
        close = self.t.orders.build(untested.symbol, Side.BUY, untested.lots, OrderSource.STRATEGY, run_id=run.id, tag=run.strategy, reason="adjustment: close untested leg")
        o1 = await self.t.orders.submit(close, actor, protective=True)
        if o1.status != OrderStatus.FILLED:
            return
        new_leg = self.t.orders.build(new_q.symbol, Side.SELL, untested.lots, OrderSource.STRATEGY, run_id=run.id, tag=run.strategy, reason="adjustment: roll untested leg")
        o2 = await self.t.orders.submit(new_leg, actor, protective=False)
        realized = (untested.entry_price - (o1.filled_price or untested.entry_price)) * u.lot_size * untested.lots
        run.legs = [l for l in run.legs if l is not untested]
        if o2.status == OrderStatus.FILLED:
            run.legs.append(Leg(option_type=untested.option_type, side=Side.SELL, strike=new_q.strike, lots=untested.lots, symbol=new_q.symbol, entry_price=o2.filled_price or new_q.ltp, ltp=o2.filled_price or new_q.ltp, expiry=run.expiry))
            run.premium_collected = round(run.premium_collected + realized + 0.0, 2)  # keep SL/target relative to original credit + realised leg pnl
        run.adjustments += 1
        run.notes.append("executed: " + note)
        self.t.db.save_run(run.model_dump(mode="json"))
        self.t.audit.record("STRATEGY_ADJUSTED", {"run": run.id, "note": note}, actor)
        await self.t.alerts.emit("INFO", "adjustment", f"Adjusted {run.underlying} {SPECS[run.strategy].name}", note, market=run.exchange.value)

    # ------------------------------------------------------------ config
    def update_config(self, key: str, patch: dict, actor: str) -> dict:
        if key not in self.config:
            raise ValueError("UNKNOWN_STRATEGY")
        for k, v in patch.items():
            if k == "enabled":
                self.enabled[key] = bool(v)
            else:
                self.config[key][k] = float(v)
        self.t.db.set_setting("strategy_config", {k: {**self.config[k], "enabled": self.enabled[k]} for k in self.config})
        self.t.audit.record("STRATEGY_CONFIG_UPDATED", {"strategy": key, "patch": patch}, actor)
        return self.config[key]

    def describe(self) -> dict:
        return {
            "strategies": [{"key": k, "name": s.name, "description": s.description, "regime_fit": s.regime_fit, "defined_risk": s.defined_risk, "params": self.config[k], "enabled": self.enabled[k]} for k, s in SPECS.items()],
            "runs": [r.model_dump(mode="json") for r in sorted(self.runs.values(), key=lambda r: r.entered_at, reverse=True)],
            "plans": [p.model_dump(mode="json") for p in sorted(self.plans.values(), key=lambda p: p.created_at, reverse=True)[:50]],
        }

    def active_runs(self, underlying: Optional[str] = None) -> List[StrategyRun]:
        return [r for r in self.runs.values() if r.status == "ACTIVE" and (underlying is None or r.underlying == underlying)]
