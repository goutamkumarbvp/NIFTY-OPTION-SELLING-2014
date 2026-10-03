"""Quantitative Strategy Agent (STRATEGY_AGENT principal): the only agent that may propose an action.

It turns an enabled strategy recipe into concrete legs from the current chain.
A proposal is a request for evaluation: the Master can reject it, the owner or
an automation policy must authorize it, and the Risk Kernel can still refuse it.
"""
from __future__ import annotations

import datetime as dt

from amrt.agents.base import Agent, AgentContext
from amrt.core.enums import OrderPurpose, Stance
from amrt.quant.costs import option_charges
from amrt.quant.strategies import build_entry, build_exit, hhmm_minutes, ist_minutes, structure_metrics
from amrt.security.identity import PrincipalKind


class QuantitativeStrategyAgent(Agent):
    name = "quantitative_strategy"
    kind = PrincipalKind.STRATEGY_AGENT

    def analyze(self, ctx: AgentContext, peers=None) -> dict:
        specs = [s for s in ctx.strategies if ctx.exchange in s.exchanges]
        if not specs:
            return {"status": "OK", "stance": Stance.NEUTRAL, "confidence": 0.0, "confidence_method": "no strategy enabled for this exchange",
                    "warnings": [f"no enabled strategy for {ctx.exchange}"]}
        spec = specs[0]
        if ctx.chain is None or ctx.chain.spot is None:
            return {"status": "INSUFFICIENT_DATA", "stance": Stance.INSUFFICIENT_DATA, "confidence": 0.0, "confidence_method": "no chain",
                    "warnings": ["option chain unavailable; no proposal"]}
        now_m = ist_minutes(ctx.now_ist)
        held = [p for p in ctx.positions if p.get("net_qty") and f":{ctx.underlying}:" in p["instrument_key"]]
        bt = ctx.backtests.get(spec.strategy_id)
        conf, method = (0.3, "fixed 0.3: no validated backtest on record") if not bt else (
            round(min(0.7, 0.3 + 0.4 * bt.get("oos_hit_rate", 0.0)), 3), "0.3 + 0.4 × out-of-sample hit rate, capped at 0.7 (validated backtest)")
        obs = {"strategy": spec.model_dump(), "now_ist": ctx.now_ist.strftime("%H:%M"), "held_legs": len(held), "backtest": bt}
        base = {"status": "OK", "confidence": conf, "confidence_method": method, "observations": obs,
                "assumptions": ["limit prices at mid (or last price when no two-sided quote)", "charges per the dated schedule in quant/costs.py"]}

        # exit window: propose flattening the underlying's legs
        if now_m >= hhmm_minutes(spec.exit_time):
            if not held:
                return {**base, "stance": Stance.NEUTRAL, "warnings": ["after exit time; no entries proposed"]}
            legs = build_exit(held, ctx.chain, OrderPurpose.EXIT)
            return {**base, "stance": Stance.NEUTRAL, "proposed_action": self._action("EXIT", spec, ctx, legs, {}, [f"scheduled exit at {spec.exit_time} IST"])}

        # roll when ATM has drifted from the short strike
        if held and ctx.analytics is not None and ctx.analytics.atm_strike is not None and ctx.analytics.strike_step:
            shorts = [p for p in held if p["net_qty"] < 0]
            strikes = {float(p["instrument_key"].split(":")[3]) for p in shorts}
            if len(strikes) == 1:
                drift = abs(ctx.analytics.atm_strike - next(iter(strikes))) / ctx.analytics.strike_step
                obs["atm_drift_strikes"] = drift
                if drift >= spec.roll_threshold_strikes and now_m < hhmm_minutes(spec.exit_time) - 15:
                    exit_legs = build_exit(held, ctx.chain, OrderPurpose.ADJUSTMENT)
                    entry_legs, why = build_entry(spec, ctx.chain)
                    if entry_legs is None:
                        return {**base, "stance": Stance.CAUTION, "warnings": [f"roll needed but new legs unavailable: {why}"],
                                "proposed_action": self._action("EXIT", spec, ctx, exit_legs, {}, ["ATM drifted; closing because new legs cannot be priced"])}
                    for leg in entry_legs:
                        leg["purpose"] = "ADJUSTMENT" if leg["purpose"] == "ENTRY" else leg["purpose"]
                    m = structure_metrics(spec, entry_legs, ctx.lot_size)
                    return {**base, "stance": Stance.NEUTRAL, "proposed_action": self._action(
                        "ROLL", spec, ctx, exit_legs + entry_legs, m, [f"ATM moved {drift:.0f} strikes from the short strike; roll to the new ATM"])}
            return {**base, "stance": Stance.NEUTRAL, "warnings": ["position already open; no new entry proposed"]}
        if held:
            return {**base, "stance": Stance.NEUTRAL, "warnings": ["position already open; no new entry proposed"]}

        start = hhmm_minutes(spec.entry_time)
        if not (start <= now_m < start + spec.entry_window_minutes):
            return {**base, "stance": Stance.NEUTRAL, "warnings": [f"outside entry window {spec.entry_time} + {spec.entry_window_minutes} min"]}
        legs, why = build_entry(spec, ctx.chain)
        if legs is None:
            return {**base, "status": "DEGRADED", "stance": Stance.CAUTION, "warnings": [f"cannot build entry: {why}"]}
        m = structure_metrics(spec, legs, ctx.lot_size)
        rationale = [spec.description, f"net credit {m['net_credit_per_unit']:.2f} per unit, ₹{m['net_credit_inr']:,.0f} for {m['quantity']} units"]
        if m["max_loss_inr"] is not None:
            rationale.append(f"structural max loss at expiry ₹{m['max_loss_inr']:,.0f} (not a guarantee: gaps and slippage can exceed it)")
        else:
            rationale.append("undefined risk: requires a policy that allows naked short options")
        return {**base, "stance": Stance.SUPPORTIVE, "proposed_action": self._action("ENTRY", spec, ctx, legs, m, rationale),
                "invalidation": ["spot moving away from the short strike by the roll threshold", "loss level 1 reached", "data turning stale"]}

    def _action(self, kind: str, spec, ctx: AgentContext, legs: list[dict], metrics: dict, rationale: list[str]) -> dict:
        day = ctx.now_ist.date() if isinstance(ctx.now_ist, dt.datetime) else dt.date.today()
        charges = 0.0
        for leg in legs:
            if leg.get("limit_price"):
                charges += option_charges(ctx.exchange, leg["side"], leg["limit_price"] * leg["lots"] * ctx.lot_size, day)["total"]
        return {"kind": kind, "strategy_id": spec.strategy_id, "strategy_version": spec.version, "underlying": ctx.underlying, "exchange": ctx.exchange,
                "expiry": ctx.chain.expiry.isoformat(), "legs": legs, "metrics": metrics, "est_charges_inr": round(charges, 2), "rationale": rationale}
