"""Copilot: a Claude tool-use agent over the terminal's own state.

Read tools expose market, chain, indicators, risk, positions, runs, council
decisions, journal and backtests. The single write tool can only *propose* a
plan into the Approvals queue; it cannot place orders, change mode or touch
risk limits. When the LLM is disabled the copilot answers deterministically
from the same tools so the panel is always useful.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List

from terminal.core.models import OrderSource

log = logging.getLogger("terminal.copilot")

SYSTEM_PROMPT = (
    "You are the copilot of an Indian options-selling terminal (NSE/BSE/MCX) with MANUAL and AUTO modes and an agent council. "
    "Answer the operator's questions using the tools; never invent numbers. Be concise (under 200 words unless asked for detail), "
    "use ₹ and lots, and state risks plainly. You may propose a trade with propose_plan only when the operator asks for a structure; "
    "a proposal goes to the Approvals queue and a human must approve it. You cannot place orders, change mode or edit limits. "
    "This is decision support for a professional operator, not personal investment advice."
)


def _schema(props: Dict[str, Any], required: List[str] | None = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


class Copilot:
    def __init__(self, terminal) -> None:
        self.t = terminal
        self.calls = 0
        self.tool_calls = 0
        self.errors = 0
        self.last_error = ""
        self._client = None

    @property
    def llm_enabled(self) -> bool:
        s = self.t.settings
        return bool(s.llm_enabled and s.llm_api_key and s.llm_provider.lower() == "anthropic")

    # ------------------------------------------------------------------ tools
    def tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {"name": "get_overview", "description": "Terminal mode, environment, risk level, daily P&L, feed/broker health and every underlying's spot, PCR, IV, DTE and expected move.", "input_schema": _schema({}), "strict": True},
            {"name": "get_chain", "description": "Option chain rows around ATM for an underlying: strike, CE/PE LTP, IV, delta, OI, OI change.", "input_schema": _schema({"underlying": {"type": "string"}, "strikes_each_side": {"type": "integer"}}, ["underlying", "strikes_each_side"]), "strict": True},
            {"name": "get_indicators", "description": "Trend, RSI, EMA, VWAP, ATR, realised vol, supertrend and VIX rank for an underlying.", "input_schema": _schema({"underlying": {"type": "string"}}, ["underlying"]), "strict": True},
            {"name": "get_risk", "description": "Risk snapshot: limits, margin, greeks, breaches, gate, kill switch, operating window and pending exits.", "input_schema": _schema({}), "strict": True},
            {"name": "get_positions_and_runs", "description": "Open positions with P&L and greeks, active strategy runs with MTM, stop-loss and target levels, and pending approvals.", "input_schema": _schema({}), "strict": True},
            {"name": "get_council", "description": "Latest council decisions with every agent's stance, score, confidence and findings.", "input_schema": _schema({"limit": {"type": "integer"}}, ["limit"]), "strict": True},
            {"name": "get_journal", "description": "Recent daily journal entries and the council evaluation scorecard (agent hit-rates).", "input_schema": _schema({"days": {"type": "integer"}}, ["days"]), "strict": True},
            {"name": "preview_strategy", "description": "Build a strategy on the live chain without trading: legs, credit, margin, breakevens, max loss and the risk pre-check. Strategies: short_straddle, short_strangle, iron_condor, iron_fly, bull_put_spread, bear_call_spread, jade_lizard.", "input_schema": _schema({"strategy": {"type": "string"}, "underlying": {"type": "string"}, "lots": {"type": "integer"}, "delta": {"type": "number"}, "stop_loss_pct": {"type": "number"}, "target_pct": {"type": "number"}}, ["strategy", "underlying", "lots", "delta", "stop_loss_pct", "target_pct"]), "strict": True},
            {"name": "propose_plan", "description": "Place a strategy proposal into the Approvals queue for the human operator (never executes by itself). Use only when the operator asked for a trade.", "input_schema": _schema({"strategy": {"type": "string"}, "underlying": {"type": "string"}, "lots": {"type": "integer"}, "rationale": {"type": "string"}}, ["strategy", "underlying", "lots", "rationale"]), "strict": True},
            {"name": "run_backtest", "description": "Synthetic backtest of a strategy (research tool): net P&L, win rate, profit factor, drawdown, exit mix.", "input_schema": _schema({"strategy": {"type": "string"}, "underlying": {"type": "string"}, "days": {"type": "integer"}, "lots": {"type": "integer"}}, ["strategy", "underlying", "days", "lots"]), "strict": True},
        ]

    async def run_tool(self, name: str, args: Dict[str, Any], actor: str) -> Any:
        t = self.t
        self.tool_calls += 1
        if name == "get_overview":
            s = t.snapshot()
            return {"mode": s["mode"], "env": s["env"], "paused": s["paused"], "risk_level": s["risk"]["snapshot"]["level"], "pnl": s["pnl"], "feed": s["feed"], "broker": s["broker"]["name"], "vix": s["vix"],
                    "markets": [{k: m[k] for k in ("symbol", "exchange", "ltp", "change_pct", "pcr", "iv_atm", "dte", "expiry", "expected_move", "session")} for m in s["market"]],
                    "operating_window": s["scheduler"]["operating_window"], "in_window": s["scheduler"]["in_operating_window"]}
        if name == "get_chain":
            u = args["underlying"].upper()
            if not t.universe.has(u):
                return {"error": f"unknown underlying {u}"}
            ch = t.chain_for(u)
            n = max(1, min(int(args.get("strikes_each_side", 6)), 15))
            rows = [r for r in ch.rows if abs(r.strike - ch.atm_strike) <= n * t.universe.get(u).strike_step]
            return {"underlying": u, "expiry": ch.expiry, "spot": ch.spot, "atm": ch.atm_strike, "pcr": ch.pcr, "max_pain": ch.max_pain, "iv_atm": ch.iv_atm, "expected_move": ch.expected_move, "dte": ch.days_to_expiry,
                    "rows": [{"strike": r.strike, "ce_ltp": r.ce.ltp, "ce_iv": r.ce.iv, "ce_delta": r.ce.delta, "ce_oi": r.ce.oi, "ce_oi_chg": r.ce.oi_change, "pe_ltp": r.pe.ltp, "pe_iv": r.pe.iv, "pe_delta": r.pe.delta, "pe_oi": r.pe.oi, "pe_oi_chg": r.pe.oi_change} for r in rows]}
        if name == "get_indicators":
            u = args["underlying"].upper()
            return {"underlying": u, "indicators": t.processor.indicators(u), "vix_rank": t.processor.vix_rank()}
        if name == "get_risk":
            return {**t.risk.describe(), "scheduler": t.scheduler.describe(), "exits": t.exit_guard.describe()["pending"]}
        if name == "get_positions_and_runs":
            return {"positions": t.positions.snapshot(), "greeks": t.positions.greeks(), "runs": [r.model_dump(mode="json") for r in t.strategies.runs.values() if r.status != "CLOSED"],
                    "pending_plans": [p.model_dump(mode="json") for p in t.council.pending_plans()]}
        if name == "get_council":
            lim = max(1, min(int(args.get("limit", 4)), 20))
            return [{"underlying": d.underlying, "decision": d.decision, "consensus": d.consensus, "confidence": d.confidence, "summary": d.summary, "ts": d.ts,
                     "agents": [{"agent": a.agent, "stance": a.stance, "score": a.score, "confidence": a.confidence, "veto": a.veto, "findings": a.findings[:3]} for a in d.assessments]} for d in t.council.decisions[-lim:][::-1]]
        if name == "get_journal":
            days = max(1, min(int(args.get("days", 5)), 30))
            return {"journal": t.db.journal(days), "evaluation": t.evaluator.scorecard() if getattr(t, "evaluator", None) else {}}
        if name == "preview_strategy":
            params = {k: float(args[k]) for k in ("delta", "stop_loss_pct", "target_pct") if args.get(k)}
            plan = t.strategies.make_plan(args["strategy"], args["underlying"].upper(), int(args["lots"]), None, params, rationale=["copilot preview"], source=OrderSource.MANUAL)
            return {"plan": {k: plan.model_dump(mode="json")[k] for k in ("strategy", "underlying", "expiry", "lots", "premium_collected", "max_profit", "max_loss", "margin_estimate", "breakevens", "stop_loss_pct", "target_pct")},
                    "legs": [{"side": l.side.value, "type": l.option_type.value, "strike": l.strike, "price": l.entry_price} for l in plan.legs], "risk_check": t.risk.check_plan(plan)}
        if name == "propose_plan":
            plan = t.strategies.make_plan(args["strategy"], args["underlying"].upper(), int(args["lots"]), rationale=[f"copilot ({actor}): {args.get('rationale', '')}"], source=OrderSource.AUTO)
            plan.status = "PROPOSED"
            t.db.save_plan(plan.model_dump(mode="json"))
            t.audit.record("COPILOT_PROPOSAL", {"plan": plan.id, "strategy": plan.strategy, "underlying": plan.underlying, "lots": plan.lots}, actor)
            await t.alerts.emit("INFO", "copilot", f"Copilot proposal: {plan.strategy} on {plan.underlying}", f"{plan.lots} lot(s), credit ₹{plan.premium_collected * plan.lots:,.0f}. Approve in the Approvals panel.")
            await t.bus.publish("plan.proposed", plan)
            return {"proposed": True, "plan_id": plan.id, "credit": plan.premium_collected, "margin_estimate": plan.margin_estimate, "risk_check": t.risk.check_plan(plan)}
        if name == "run_backtest":
            from terminal.analytics.backtest import run_backtest
            u = t.universe.get(args["underlying"].upper())
            loop = asyncio.get_running_loop()
            r = await loop.run_in_executor(None, lambda: run_backtest(u, args["strategy"], max(5, min(int(args.get("days", 90)), 400)), int(args.get("lots", 1))))
            return {k: r[k] for k in ("strategy", "underlying", "days", "net_pnl", "win_rate", "profit_factor", "max_drawdown", "exits")}
        return {"error": f"unknown tool {name}"}

    # ------------------------------------------------------------------ chat
    async def chat(self, message: str, user: str) -> Dict[str, Any]:
        self.calls += 1
        self.t.db.add_copilot(user, "user", message)
        try:
            if self.llm_enabled:
                answer, trace = await asyncio.wait_for(self._chat_llm(message, user), timeout=self.t.settings.llm_timeout_seconds * 4)
                mode = "claude"
            else:
                answer, trace = await self._chat_deterministic(message, user)
                mode = "deterministic"
        except Exception as exc:
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            answer, trace, mode = f"Copilot error: {self.last_error}", [], "error"
        self.t.db.add_copilot(user, "assistant", answer)
        return {"answer": answer, "tools": trace, "mode": mode, "ts": time.time()}

    async def _chat_llm(self, message: str, user: str):
        import anthropic

        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.t.settings.llm_api_key)
        history = self.t.db.copilot_history(user, limit=10)
        messages: List[Dict[str, Any]] = [{"role": h["role"], "content": h["content"]} for h in history if h["role"] in ("user", "assistant")]
        if not messages or messages[-1]["role"] != "user" or messages[-1]["content"] != message:
            messages.append({"role": "user", "content": message})
        trace: List[Dict[str, Any]] = []
        for _ in range(self.t.settings.copilot_max_turns):
            try:
                resp = await self._client.messages.create(model=self.t.settings.llm_model, max_tokens=4000, system=SYSTEM_PROMPT, tools=self.tool_definitions(),
                                                          output_config={"effort": "medium"}, messages=messages)
            except anthropic.RateLimitError as exc:
                raise RuntimeError("rate limited") from exc
            except anthropic.APIStatusError as exc:
                raise RuntimeError(f"api error {exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise RuntimeError("connection error") from exc
            if resp.stop_reason == "refusal":
                return "The model declined this request.", trace
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                return text or "(no answer)", trace
            messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
            results = []
            for tu in tool_uses:
                args = tu.input if isinstance(tu.input, dict) else json.loads(json.dumps(tu.input))
                try:
                    out = await self.run_tool(tu.name, args, user)
                    payload = json.dumps(out, default=str)[:12000]
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": payload})
                    trace.append({"tool": tu.name, "args": args, "ok": True})
                except Exception as exc:
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"error: {exc}", "is_error": True})
                    trace.append({"tool": tu.name, "args": args, "ok": False, "error": str(exc)[:200]})
            messages.append({"role": "user", "content": results})
        return "I ran out of tool-call rounds; here is what I gathered so far. Ask a narrower question.", trace

    async def _chat_deterministic(self, message: str, user: str):
        """No LLM configured: answer the common questions straight from the tools."""
        m = message.lower()
        trace: List[Dict[str, Any]] = []
        parts: List[str] = []

        async def use(name: str, **args):
            out = await self.run_tool(name, args, user)
            trace.append({"tool": name, "args": args, "ok": True})
            return out

        und = next((u.symbol for u in self.t.universe.all() if u.symbol.lower() in m), None)
        if any(k in m for k in ("propose", "suggest a trade", "build", "deploy")) and und:
            key = next((k for k in ("iron_condor", "short_strangle", "short_straddle", "iron_fly", "bull_put_spread", "bear_call_spread", "jade_lizard") if k.replace("_", " ") in m or k in m), "short_strangle")
            lots = 1
            r = await use("propose_plan", strategy=key, underlying=und, lots=lots, rationale="operator request via copilot")
            parts.append(f"Proposed {key} on {und} x{lots}: credit ₹{r['credit']:,.0f}/lot, margin ≈ ₹{r['margin_estimate']:,.0f}, risk check {'allowed' if r['risk_check']['allowed'] else 'BLOCKED: ' + ', '.join(r['risk_check']['reasons'])}. It is waiting in Approvals.")
        elif "chain" in m and und:
            r = await use("get_chain", underlying=und, strikes_each_side=4)
            parts.append(f"{und} {r['expiry']}: spot {r['spot']:.0f}, ATM {r['atm']:.0f}, PCR {r['pcr']}, ATM IV {r['iv_atm']}%, max pain {r['max_pain']:.0f}, expected move ±{r['expected_move']:.0f}.")
            parts.append("Strike | CE LTP/Δ/OI | PE LTP/Δ/OI: " + "; ".join(f"{x['strike']:.0f} | {x['ce_ltp']}/{x['ce_delta']}/{x['ce_oi'] // 1000}K | {x['pe_ltp']}/{x['pe_delta']}/{x['pe_oi'] // 1000}K" for x in r["rows"]))
        elif any(k in m for k in ("risk", "margin", "limit", "gate", "kill")):
            r = await use("get_risk")
            snap = r["snapshot"]
            parts.append(f"Risk {snap['level']}: daily P&L ₹{snap['daily_pnl']:,.0f} ({snap['loss_budget_used_pct']}% of ₹{snap['max_daily_loss']:,.0f} budget), margin ₹{snap['margin_used']:,.0f} ({snap['margin_utilisation_pct']}%), {snap['open_lots']} lots / {snap['open_positions']} positions, Δ {snap['portfolio_delta']}, vega {snap['portfolio_vega']}. Gate {'open' if snap['safety_gate_open'] else 'closed'}, kill switch {'ON' if snap['kill_switch'] else 'off'}, window {r['scheduler']['operating_window'][0]}–{r['scheduler']['operating_window'][1]} ({'open' if r['scheduler']['in_operating_window'] else 'closed'}).")
            if snap["breaches"] or snap["warnings"]:
                parts.append("Breaches: " + ", ".join(snap["breaches"]) + " | Warnings: " + ", ".join(snap["warnings"]))
        elif any(k in m for k in ("position", "run", "p&l", "pnl", "trade")):
            r = await use("get_positions_and_runs")
            parts.append(f"{len(r['positions'])} open positions, {len(r['runs'])} runs, {len(r['pending_plans'])} pending proposals. Greeks Δ {r['greeks']['delta']} Θ {r['greeks']['theta']} vega {r['greeks']['vega']}.")
            for run in r["runs"][:6]:
                parts.append(f"• {run['strategy']} {run['underlying']} {run['lots']}L MTM ₹{run['mtm']:,.0f} (credit ₹{run['premium_collected']:,.0f}, SL {run['stop_loss_pct']}%, target {run['target_pct']}%) {run['status']}")
        elif any(k in m for k in ("council", "why", "agent", "decision")):
            r = await use("get_council", limit=4)
            for d in r:
                parts.append(f"{d['underlying']}: {d['decision']} (consensus {d['consensus']:+.2f}, conf {d['confidence']:.2f}). " + "; ".join(f"{a['agent']} {a['stance']} {a['score']:+.2f}{' VETO' if a['veto'] else ''}" for a in d["agents"]))
        elif "backtest" in m and und:
            key = next((k for k in ("iron_condor", "short_strangle", "short_straddle", "iron_fly", "bull_put_spread", "bear_call_spread", "jade_lizard") if k.replace("_", " ") in m or k in m), "short_strangle")
            r = await use("run_backtest", strategy=key, underlying=und, days=120, lots=1)
            parts.append(f"Backtest {key} {und} {r['days']} sessions: net ₹{r['net_pnl']:,.0f}, win rate {r['win_rate']}, profit factor {r['profit_factor']}, max DD ₹{r['max_drawdown']:,.0f}, exits {r['exits']}.")
        elif "journal" in m or "yesterday" in m or "evaluation" in m or "score" in m:
            r = await use("get_journal", days=5)
            ev = r.get("evaluation") or {}
            parts.append(f"Journal entries: {len(r['journal'])}. Evaluation: {ev.get('scored', 0)} scored decisions; agent hit-rates: " + ", ".join(f"{k} {v['hit_rate']:.0%} ({v['n']})" for k, v in (ev.get("agents") or {}).items()) if ev else "No evaluation data yet.")
        else:
            r = await use("get_overview")
            parts.append(f"{r['mode']} / {r['env']} · risk {r['risk_level']} · daily P&L ₹{r['pnl']['daily']:,.0f} · window {'open' if r['in_window'] else 'closed'} · feed {'ok' if r['feed']['connected'] else 'DOWN'}.")
            parts.append("; ".join(f"{x['symbol']} {x['ltp']} ({x['change_pct']:+.2f}%, PCR {x['pcr']}, IV {x['iv_atm']})" for x in r["markets"][:6]))
            parts.append("Ask me: 'risk', 'positions', 'why did the council hold', 'chain NIFTY', 'backtest iron condor BANKNIFTY', 'propose a strangle on NIFTY'. Set LLM_ENABLED=true with an Anthropic key for full reasoning.")
        return "\n".join(parts), trace

    def status(self) -> dict:
        return {"llm": self.llm_enabled, "model": self.t.settings.llm_model if self.llm_enabled else "deterministic", "calls": self.calls, "tool_calls": self.tool_calls, "errors": self.errors, "last_error": self.last_error}
