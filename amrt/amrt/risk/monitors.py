"""The two independent protective paths.

Path A — PortfolioRiskMonitor (loss & exposure):
  * loss ≥ level1 (₹) or margin-risk ≥ level1 (%)  → EMERGENCY LEVEL1: freeze new risk, alert.
  * loss ≥ level2 or margin-risk ≥ level2          → EMERGENCY LEVEL2: freeze, alert, and run the
    pre-authorized protective action from the owner-approved policy (FLATTEN_ALL), then verify residual exposure.
  * per-leg stops and a profit ratchet (stop only ever tightens) trigger reduce-only exits.
  * P&L that cannot be computed (missing marks) freezes new risk — missing evidence is never treated as safe.
  These are triggers, not guarantees: slippage, gaps, halts and API failures can produce larger losses.

Path B — SafetyMonitor (integrity of the inputs):
  * stale market data on held instruments, broker unavailable, heartbeat loss of
    critical components, clock drift beyond policy, orders stuck in ORDER STATE
    UNKNOWN, event-store / database failures → freeze new risk (latched) + incident.
  * Master Agent failure → AI-driven actions suspended (never re-assigned).
  * Database unreachable → kill switch B (file) is engaged directly.
Neither path can release what it latched; only the owner can, after verification.
"""
from __future__ import annotations

import logging

from amrt.core.enums import Mode
from amrt.core.errors import PermissionDenied
from amrt.security.identity import Principal

log = logging.getLogger("amrt.monitors")


class PortfolioRiskMonitor:
    name = "portfolio_risk_monitor"

    def __init__(self, principal: Principal, builder, safety, protective, alerts, incidents, health, mode_ctl, accounts, ledger, db, clock, events) -> None:
        self.principal = principal
        self.builder, self.safety, self.protective, self.alerts, self.incidents = builder, safety, protective, alerts, incidents
        self.health, self.mode_ctl, self.accounts, self.ledger, self.db, self.clock, self.events = health, mode_ctl, accounts, ledger, db, clock, events
        self.last: dict[str, dict] = {}
        self._flatten_attempts: dict[str, list[float]] = {}
        self._leg_exit_ts: dict[tuple[str, str], float] = {}

    def _ratchet(self, account_id: str, pnl: float, policy) -> dict:
        day = self.clock.ist().date().isoformat()
        key = f"ratchet:{account_id}:{day}"
        st = self.db.kv_get(key) or {"peak": 0.0, "stop": None, "triggered": False}
        changed = False
        if pnl > st["peak"]:
            st["peak"], changed = pnl, True
        if policy.ratchet_activation_inr and policy.ratchet_trail_inr and st["peak"] >= policy.ratchet_activation_inr:
            new_stop = st["peak"] - policy.ratchet_trail_inr
            if st["stop"] is None or new_stop > st["stop"]:     # never loosens
                st["stop"], changed = new_stop, True
        if changed:
            self.db.kv_set(key, st, self.principal.id)
        return st

    async def _protect(self, account_id: str, reason: str, correlation: str) -> dict | None:
        acct = self.accounts.get(account_id)
        if acct.kind == "LIVE" and self.mode_ctl.mode == Mode.PAPER:
            self.alerts.emit("CRITICAL", "risk", "LIVE EXPOSURE WHILE IN PAPER MODE", f"{account_id}: {reason}. Switch to MANUAL and use Quick Exit.")
            return None
        tries = [t for t in self._flatten_attempts.get(account_id, []) if self.clock.ts() - t < 300]
        if len(tries) >= 5:
            self.alerts.emit("CRITICAL", "risk", f"Protective exits not completing on {account_id}", "Escalated to owner; emergency state retained.", dedupe_s=120)
            return None
        if tries and self.clock.ts() - tries[-1] < 10:
            return None
        tries.append(self.clock.ts())
        self._flatten_attempts[account_id] = tries
        try:
            return await self.protective.flatten(account_id, reason, self.principal)
        except PermissionDenied as exc:
            self.alerts.emit("CRITICAL", "risk", f"Protective action refused on {account_id}", str(exc))
            return None

    async def tick(self) -> dict:
        out = {}
        for acct in self.accounts.accounts.values():
            open_pos = self.ledger.open_positions(acct.account_id)
            pol, _ = self.builder.policy_for(acct.account_id)
            if pol is None:
                if open_pos:
                    self.safety.freeze(self.principal, f"NO_POLICY:{acct.account_id}", "open positions without an approved risk policy")
                continue
            val = self.ledger.valuation(acct.account_id, lambda k, p=pol: self.builder.mark(k, p.max_data_age_ms), lambda k, p=pol: self.builder.spot_for(k, p.max_data_age_ms))
            pnl = val["net_pnl_today"]
            st = {"account_id": acct.account_id, "pnl": pnl, "open_lots": val["open_lots"], "marks_missing": val["marks_missing"], "level": "NONE"}
            if open_pos and pnl is None:
                self.safety.freeze(self.principal, f"PNL_UNAVAILABLE:{acct.account_id}", f"marks missing for {val['marks_missing']}")
                self.alerts.emit("CRITICAL", "risk", f"P&L unavailable on {acct.account_id}", f"DATA UNAVAILABLE for {', '.join(val['marks_missing'])}")
                st["level"] = "UNKNOWN"
                out[acct.account_id] = st
                continue
            if pnl is None:
                out[acct.account_id] = st
                continue
            loss = max(0.0, -pnl)
            f = acct.funds
            den = f.get("sod_funds") if pol.margin_risk_denominator == "SOD_ACCOUNT_FUNDS" else f.get("margin_used")
            mr = 100.0 * loss / den if den else None
            st.update({"loss": loss, "margin_risk_pct": None if mr is None else round(mr, 3), "denominator": pol.margin_risk_denominator, "denominator_value": den})
            level2 = loss >= pol.loss_level2_inr or (mr is not None and mr >= pol.margin_risk_level2_pct)
            level1 = loss >= pol.loss_level1_inr or (mr is not None and mr >= pol.margin_risk_level1_pct)
            if level2:
                st["level"] = "LEVEL2"
                reason = f"loss ₹{loss:.0f} (level2 ₹{pol.loss_level2_inr:.0f}) / margin-risk {mr if mr is None else round(mr, 2)}% (level2 {pol.margin_risk_level2_pct}%)"
                if self.safety.raise_emergency(self.principal, "LEVEL2", f"{acct.account_id}: {reason}"):
                    self.alerts.emit("CRITICAL", "risk", f"EMERGENCY LEVEL 2 on {acct.account_id}", reason)
                    self.incidents.open("portfolio_risk", "SEV1", f"Loss level 2 on {acct.account_id}", st, self.principal)
                if pol.protective_action_level2 == "FLATTEN_ALL" and open_pos:
                    st["protective"] = await self._protect(acct.account_id, f"LEVEL2: {reason}", "level2")
            elif level1:
                st["level"] = "LEVEL1"
                reason = f"loss ₹{loss:.0f} (level1 ₹{pol.loss_level1_inr:.0f}) / margin-risk {mr if mr is None else round(mr, 2)}%"
                if self.safety.raise_emergency(self.principal, "LEVEL1", f"{acct.account_id}: {reason}"):
                    self.alerts.emit("WARNING", "risk", f"Loss level 1 on {acct.account_id}: new risk frozen", reason)
            # per-leg stops
            if pol.per_leg_stop_pct:
                for p in open_pos:
                    mark = self.builder.mark(p.instrument_key, pol.max_data_age_ms)
                    if mark is None or p.avg_price <= 0:
                        continue
                    hit = (p.net_qty < 0 and mark >= p.avg_price * (1 + pol.per_leg_stop_pct / 100)) or \
                          (p.net_qty > 0 and mark <= p.avg_price * (1 - pol.per_leg_stop_pct / 100))
                    k = (acct.account_id, p.instrument_key)
                    if hit and self.clock.ts() - self._leg_exit_ts.get(k, 0) > 10:
                        self._leg_exit_ts[k] = self.clock.ts()
                        if acct.kind == "LIVE" and self.mode_ctl.mode == Mode.PAPER:
                            self.alerts.emit("CRITICAL", "risk", "Leg stop hit on LIVE position while in PAPER MODE", p.instrument_key)
                            continue
                        res = await self.protective.exit_position(acct.account_id, p.instrument_key, f"LEG_STOP {pol.per_leg_stop_pct}% (entry {p.avg_price}, mark {mark})", self.principal)
                        self.alerts.emit("WARNING", "risk", f"Leg stop: exiting {p.instrument_key}", str(res))
            # ratchet
            rt = self._ratchet(acct.account_id, pnl, pol)
            st["ratchet"] = rt
            if rt.get("stop") is not None and pnl <= rt["stop"] and open_pos and not rt.get("triggered"):
                rt["triggered"] = True
                self.db.kv_set(f"ratchet:{acct.account_id}:{self.clock.ist().date().isoformat()}", rt, self.principal.id)
                self.safety.freeze(self.principal, f"RATCHET:{acct.account_id}", f"P&L {pnl:.0f} ≤ ratchet stop {rt['stop']:.0f}")
                st["protective"] = await self._protect(acct.account_id, f"RATCHET stop {rt['stop']:.0f} hit at {pnl:.0f}", "ratchet")
            out[acct.account_id] = st
        self.last = out
        self.health.beat(self.name, meta={"accounts": len(out)})
        return out


class SafetyMonitor:
    name = "safety_monitor"
    CRITICAL = ("gateway", "event_store", "portfolio_risk_monitor", "reconciler", "database")

    def __init__(self, principal: Principal, safety, health, hub, book, ledger, accounts, builder, incidents, alerts, db, clock, settings) -> None:
        self.principal = principal
        self.safety, self.health, self.hub, self.book, self.ledger, self.accounts = safety, health, hub, book, ledger, accounts
        self.builder, self.incidents, self.alerts, self.db, self.clock, self.settings = builder, incidents, alerts, db, clock, settings
        self.findings: list[dict] = []

    def _trip(self, code: str, detail: str, severity: str = "SEV2") -> None:
        added = self.safety.freeze(self.principal, code, detail)
        self.findings.append({"ts": self.clock.ts(), "code": code, "detail": detail})
        self.findings = self.findings[-200:]
        if added:
            self.alerts.emit("CRITICAL" if severity in ("SEV1", "SEV2") else "WARNING", "safety", f"New risk frozen: {code}", detail)
            try:
                self.incidents.open("safety_monitor", severity, code, {"detail": detail}, self.principal)
            except Exception:  # noqa: BLE001
                pass

    async def tick(self) -> list[dict]:
        found = []
        # database / event store reachability — if the database is gone, engage switch B directly
        if not self.db.ping():
            try:
                self.safety.engage_kill(self.principal, "database unreachable", variant="FREEZE")
            except Exception:  # noqa: BLE001
                pass
            found.append({"code": "DATABASE_UNREACHABLE"})
            self.health.beat(self.name, meta={"findings": len(found)})
            return found
        # stale data on held instruments
        for acct in self.accounts.accounts.values():
            pos = self.ledger.open_positions(acct.account_id)
            if not pos:
                continue
            pol, _ = self.builder.policy_for(acct.account_id)
            limit = (pol.max_data_age_ms if pol else 3000) * 3
            stale = [p.instrument_key for p in pos if not self.hub.freshness(p.instrument_key, limit).fresh]
            if stale:
                found.append({"code": f"STALE_DATA:{acct.account_id}", "instruments": stale})
                self._trip(f"STALE_DATA:{acct.account_id}", f"no fresh data for {', '.join(stale)} (> {limit} ms)")
            if acct.kind == "LIVE":
                bs = self.health.state(f"broker:{acct.account_id}")
                if bs.value in ("FAILED", "UNAVAILABLE", "QUARANTINED"):
                    found.append({"code": f"BROKER_UNAVAILABLE:{acct.account_id}"})
                    self._trip(f"BROKER_UNAVAILABLE:{acct.account_id}", f"broker health {bs.value} with open positions", "SEV1")
        # heartbeats of critical components
        for comp in self.CRITICAL:
            if comp in self.health.components and self.health.state(comp).value in ("STALE", "FAILED", "UNAVAILABLE"):
                found.append({"code": f"HEARTBEAT_LOSS:{comp}"})
                self._trip(f"HEARTBEAT_LOSS:{comp}", f"{comp} is {self.health.state(comp).value}", "SEV1")
        if "master_agent" in self.health.components and self.health.state("master_agent").value in ("STALE", "FAILED", "QUARANTINED"):
            if self.safety.suspend_ai(self.principal, f"Master Agent {self.health.state('master_agent').value}"):
                self.alerts.emit("WARNING", "agents", "AI-driven actions suspended", "Master Agent unhealthy; no other agent is promoted.")
            found.append({"code": "MASTER_AGENT_UNHEALTHY"})
        # clock drift
        for name, src in self.hub.sources.items():
            d = src.drift_ms
            if src.live and d is not None and abs(d) > self.settings.hard_limits().max_clock_drift_ms:
                found.append({"code": f"CLOCK_DRIFT:{name}", "drift_ms": d})
                self._trip(f"CLOCK_DRIFT:{name}", f"median drift {d:.0f} ms vs exchange timestamps")
        # unknown orders
        for r in self.book.unknown():
            age = self.clock.ts() - (r["unknown_since"] or self.clock.ts())
            if age > 30:
                found.append({"code": "ORDER_STATE_UNKNOWN", "intent_id": r["intent_id"], "age_s": round(age)})
                self._trip("ORDER_STATE_UNKNOWN", f"{r['intent_id']} unresolved for {age:.0f}s — awaiting reconciliation", "SEV1")
        self.health.beat(self.name, meta={"findings": len(found)})
        return found
