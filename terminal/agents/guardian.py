"""Guardian: the self-monitoring / self-healing agent.

It watches the terminal's own health — every internal loop's heartbeat, the market
feed, the broker session and REST error rate, book reconciliation, stuck exits,
event-loop lag, error bursts and host resources — turns a symptom into an
*incident* with a diagnosis and a concrete remedy, and then **asks the operator
for final permission** before it applies anything (GUARDIAN_AUTO_APPLY=none, the
default). Incidents that clear on their own are closed as self-recovered; a remedy
that does not clear the symptom escalates to the next one in the chain.

The Guardian never places entry orders. Its only market-affecting remedies are
"pause entries" and "flatten all", and flatten always requires explicit approval
regardless of the auto-apply policy.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from terminal.core.models import OrderSource, new_id

log = logging.getLogger("terminal.guardian")

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
OPEN_STATES = {"AWAITING_APPROVAL", "APPROVED", "APPLYING", "APPLIED", "ADVISORY"}
ESCALATION = {"reconnect_feed": "relogin_session", "relogin_session": "pause_entries", "restart_loop": "pause_entries", "adopt_broker_book": "pause_entries", "pause_entries": "flatten_all"}

DIAGNOSIS_PROMPT = (
    "You are the reliability engineer for an options-trading terminal (Python asyncio, FastAPI, a broker WebSocket feed and REST session). "
    "Given one incident with evidence, give a root-cause diagnosis in at most three short bullet points and say whether the proposed remedy is appropriate, "
    "or name a better one from this list only: reconnect_feed, relogin_session, restart_loop, adopt_broker_book, pause_entries, prune_history, flatten_all, none. "
    "Never invent facts that are not in the evidence. Be terse."
)


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: new_id("INC"))
    ts: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)
    component: str
    symptom: str
    severity: str = "WARNING"  # WARNING | CRITICAL
    summary: str = ""
    evidence: Dict[str, Any] = Field(default_factory=dict)
    diagnosis: List[str] = Field(default_factory=list)
    ai_diagnosis: str = ""
    remedy: str | None = None
    remedy_label: str = ""
    remedy_args: Dict[str, Any] = Field(default_factory=dict)
    risk: str = "low"  # low | medium | high
    status: str = "AWAITING_APPROVAL"
    approved_by: str = ""
    decided_at: float | None = None
    applied_at: float | None = None
    expires_at: float | None = None
    verify_by: float | None = None
    result: str = ""
    occurrences: int = 1
    escalated_from: str | None = None

    @property
    def key(self) -> str:
        return f"{self.component}:{self.symptom}"


class Finding(BaseModel):
    component: str
    symptom: str
    severity: str = "WARNING"
    summary: str
    evidence: Dict[str, Any] = Field(default_factory=dict)
    diagnosis: List[str] = Field(default_factory=list)
    remedy: str | None = None
    remedy_args: Dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.component}:{self.symptom}"


class GuardianAgent:
    name = "Guardian"
    role = "Self-monitoring & self-healing (asks before acting)"

    def __init__(self, terminal) -> None:
        self.t = terminal
        s = terminal.settings
        self.enabled = bool(s.guardian_enabled)
        self.interval = float(s.guardian_interval_seconds)
        self.auto_apply = str(s.guardian_auto_apply).lower()  # none | low | medium | all
        self.approval_ttl = float(s.guardian_approval_ttl_seconds)
        self.incidents: List[Incident] = []
        self.scans = 0
        self.last_scan_ts = 0.0
        self.applied = 0
        self.healed = 0
        self.failed = 0
        self.last_error = ""
        self._lag_samples: List[float] = []
        self._live_errors_hist: List[tuple] = []
        self._feed_reconnects_hist: List[tuple] = []
        self._feed_down_since: float | None = None
        self._muted: Dict[str, float] = {}  # key -> until (after a rejection / expiry, don't nag immediately)
        self._started = time.time()
        self.remedies: Dict[str, tuple] = {
            # name: (label, risk, coroutine(incident) -> result text)
            "reconnect_feed": ("Reconnect market feed (re-subscribe every instrument)", "low", self._r_reconnect_feed),
            "relogin_session": ("Re-login broker session (TOTP/MPIN) and reconnect feed", "medium", self._r_relogin),
            "restart_loop": ("Restart the stalled internal loop", "low", self._r_restart_loop),
            "adopt_broker_book": ("Adopt broker positions as the book of record", "medium", self._r_adopt_book),
            "pause_entries": ("Pause new entries (exits and stop-losses keep running)", "low", self._r_pause),
            "prune_history": ("Prune old logs from the local database", "low", self._r_prune),
            "flatten_all": ("Flatten every open position at market", "high", self._r_flatten),
        }

    # ------------------------------------------------------------------ loop
    async def run(self) -> None:
        await asyncio.sleep(min(5.0, self.interval))
        while not self.t._stop.is_set():
            try:
                await self.scan()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.exception("guardian scan failed")
            try:
                await asyncio.wait_for(self.t._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    async def scan(self) -> List[Incident]:
        """One monitoring pass: detect, dedupe, verify remedies, auto-resolve, expire approvals."""
        self.scans += 1
        self.last_scan_ts = time.time()
        self.t.beat("guardian-loop")
        findings = self.detect()
        present = {f.key for f in findings}
        now = time.time()
        new: List[Incident] = []
        for f in findings:
            inc = self._open(f.key)
            if inc is not None:
                inc.occurrences += 1
                inc.last_seen = now
                inc.evidence, inc.summary, inc.severity = f.evidence, f.summary, f.severity
                if inc.status == "ADVISORY" and f.remedy:
                    await self._upgrade(inc, f)  # the symptom persisted long enough to warrant a remedy
                elif inc.status == "APPLIED" and inc.verify_by and now > inc.verify_by:
                    await self._escalate(inc)
                continue
            if self._muted.get(f.key, 0) > now:
                continue
            new.append(await self._raise(f))
        for inc in list(self.incidents):
            if inc.status not in OPEN_STATES:
                continue
            if inc.key not in present:
                if inc.status == "APPLIED":
                    self.healed += 1
                    self._close(inc, "RESOLVED", f"healed: symptom cleared {round(now - (inc.applied_at or now))}s after remedy")
                elif inc.status in ("AWAITING_APPROVAL", "ADVISORY"):
                    self._close(inc, "RESOLVED", "self-recovered before any action was needed")
            elif inc.status == "AWAITING_APPROVAL" and inc.expires_at and now > inc.expires_at:
                self._close(inc, "EXPIRED", "no decision within the approval window; will be raised again if the symptom persists")
                self._muted[inc.key] = now + 60.0
        self.incidents = self.incidents[-300:]
        return new

    # ------------------------------------------------------------- detectors
    def detect(self) -> List[Finding]:
        t = self.t
        s = t.settings
        out: List[Finding] = []
        now = time.time()
        # 1. internal loops: crashed task or stale heartbeat
        for name, (_, period) in t.loop_specs().items():
            task = t.loop_task(name)
            hb = t.heartbeats.get(name, self._started)
            limit = max(45.0, period * 6)  # network loops legitimately block for a connect timeout
            if task is not None and task.done() and not t._stop.is_set():
                err = ""
                try:
                    exc = task.exception() if not task.cancelled() else None
                    err = f"{type(exc).__name__}: {exc}" if exc else "cancelled"
                except Exception:
                    err = "unknown"
                out.append(Finding(component="loops", symptom=f"LOOP_CRASHED:{name}", severity="CRITICAL", summary=f"internal loop '{name}' is no longer running ({err})",
                                   evidence={"loop": name, "error": err, "heartbeat_age_s": round(now - hb, 1)}, diagnosis=[f"The '{name}' task exited; nothing it owns (marks, risk checks, streaming…) is being done."],
                                   remedy="restart_loop", remedy_args={"loop": name}))
            elif now - hb > limit:
                out.append(Finding(component="loops", symptom=f"LOOP_STALLED:{name}", severity="WARNING", summary=f"internal loop '{name}' has not reported for {round(now - hb)}s (expected every {period:g}s)",
                                   evidence={"loop": name, "heartbeat_age_s": round(now - hb, 1), "expected_period_s": period}, diagnosis=["A blocking call or an await that never returns is holding the loop."],
                                   remedy="restart_loop", remedy_args={"loop": name}))
        # 2. market feed
        feed = t.feed
        session_open = any(t.scheduler.session(u) == "OPEN" for u in t.universe.all()) or getattr(t.scheduler, "assume_open", False)
        if t.engine_running and not feed.connected:
            self._feed_down_since = self._feed_down_since or now
            down_for = now - self._feed_down_since
            exposure = len(t.positions.open_positions())
            out.append(Finding(component="feed", symptom="FEED_DISCONNECTED", severity="CRITICAL" if exposure else "WARNING", summary=f"market feed '{feed.name}' is disconnected for {round(down_for)}s",
                               evidence={"feed": feed.status(), "down_for_s": round(down_for), "open_positions": exposure},
                               diagnosis=["WebSocket closed and the feed's own back-off has not restored it." if down_for > 30 else "WebSocket closed; the feed's own reconnect back-off is running."],
                               remedy="reconnect_feed" if down_for > 20 else None))
            if exposure and down_for > 90:
                out.append(Finding(component="feed", symptom="FEED_DOWN_WITH_EXPOSURE", severity="CRITICAL", summary=f"{exposure} open position(s) are unmarked: no live prices for {round(down_for)}s",
                                   evidence={"down_for_s": round(down_for), "open_positions": exposure, "reconnects": getattr(feed, "reconnects", None)},
                                   diagnosis=["Stop-losses cannot be evaluated without prices.", "Pausing entries prevents adding exposure while blind; flattening is the last resort."], remedy="pause_entries"))
        else:
            self._feed_down_since = None
            if feed.connected and feed.last_tick_ts and session_open and (now - feed.last_tick_ts) > s.feed_stale_seconds * 3:
                out.append(Finding(component="feed", symptom="FEED_STALE", severity="WARNING", summary=f"feed connected but silent for {round(now - feed.last_tick_ts)}s during an open session",
                                   evidence={"feed": feed.status(), "stale_s": round(now - feed.last_tick_ts)}, diagnosis=["Half-open socket: connected flag is true but no data arrives (typical after a network blip)."],
                                   remedy="reconnect_feed"))
        rc = getattr(feed, "reconnects", None)
        if isinstance(rc, int):
            self._feed_reconnects_hist.append((now, rc))
            self._feed_reconnects_hist = [(a, b) for a, b in self._feed_reconnects_hist if now - a <= 600]
            if len(self._feed_reconnects_hist) > 1 and rc - self._feed_reconnects_hist[0][1] >= 3:
                out.append(Finding(component="feed", symptom="FEED_FLAPPING", severity="WARNING", summary=f"feed reconnected {rc - self._feed_reconnects_hist[0][1]} times in 10 minutes",
                                   evidence={"reconnects_10m": rc - self._feed_reconnects_hist[0][1], "session": t.live.status() if t.live else None},
                                   diagnosis=["Repeated drops usually mean an expired session token or duplicate login on the broker side."], remedy="relogin_session"))
        # 3. broker session / REST errors
        live = t.live
        if live is not None:
            if not live.authenticated and (live.last_error or live.calls) and session_open:
                out.append(Finding(component="broker", symptom="SESSION_LOST", severity="CRITICAL" if t.positions.open_positions() else "WARNING", summary=f"{live.provider} session not authenticated: {live.last_error or 'no session'}",
                                   evidence={"session": live.status()}, diagnosis=["The broker rejected or expired the session; chain polling and order routing fail until re-login."], remedy="relogin_session"))
            self._live_errors_hist.append((now, live.errors))
            self._live_errors_hist = [(a, b) for a, b in self._live_errors_hist if now - a <= 60]
            burst = live.errors - self._live_errors_hist[0][1] if self._live_errors_hist else 0
            if burst >= 5:
                out.append(Finding(component="broker", symptom="BROKER_API_ERRORS", severity="WARNING", summary=f"{burst} broker API errors in the last minute ({live.last_error})",
                                   evidence={"errors_1m": burst, "last_error": live.last_error}, diagnosis=["Rate limit, expired token or broker outage. A fresh login resolves token problems; outages need patience."],
                                   remedy="relogin_session" if live._auth_error(live.last_error or "") else "pause_entries"))
        # 4. reconciliation
        pr = t.position_reconciler.describe()
        if pr.get("ok") is False:
            out.append(Finding(component="book", symptom="BOOK_MISMATCH", severity="CRITICAL", summary=f"terminal book differs from broker: {len(pr.get('diffs', []))} diffs, {len(pr.get('broker_only', []))} broker-only, {len(pr.get('terminal_only', []))} terminal-only",
                               evidence={"reconcile": pr}, diagnosis=["A fill was missed or an order was placed outside the terminal; risk numbers are wrong until the book is adopted from the broker."], remedy="adopt_broker_book"))
        # 5. stuck exits
        stuck = [p for p in t.exit_guard.describe().get("pending", []) if isinstance(p, dict) and p.get("attempts", 0) >= 6]
        if stuck:
            out.append(Finding(component="exits", symptom="EXIT_STUCK", severity="CRITICAL", summary=f"{len(stuck)} exit(s) still not filled after {stuck[0].get('attempts')} attempts",
                               evidence={"exits": stuck[:5]}, diagnosis=["Exit orders keep failing or not filling: illiquid strike, broker rejects or a dead session."],
                               remedy="relogin_session" if live is not None and not live.authenticated else None))
        # 6. council
        c = t.council
        if c.last_cycle_ts and not c.busy and not t.paused and t.scheduler.in_operating_window() and now - c.last_cycle_ts > s.agent_cycle_seconds * 6 + 30:
            out.append(Finding(component="council", symptom="COUNCIL_STALLED", severity="WARNING", summary=f"agent council has not completed a cycle for {round(now - c.last_cycle_ts)}s",
                               evidence={"last_cycle_age_s": round(now - c.last_cycle_ts), "cycle": c.cycle}, diagnosis=["The council loop is alive but cycles are not completing (LLM timeout or an agent exception)."],
                               remedy="restart_loop", remedy_args={"loop": "council-loop"}))
        # 7. event-loop lag
        self._lag_samples = (self._lag_samples + [t.loop_lag_ms])[-3:]
        if len(self._lag_samples) == 3 and min(self._lag_samples) > 2000:
            out.append(Finding(component="runtime", symptom="EVENT_LOOP_LAG", severity="WARNING", summary=f"chain loop taking {round(min(self._lag_samples))}+ ms per pass; ticks are being processed late",
                               evidence={"lag_ms": self._lag_samples}, diagnosis=["CPU starvation: too many strikes, too many ticks or a blocking call on the event loop."], remedy="pause_entries"))
        # 8. error burst
        recent = [r for r in t.db.logs(200, "CRITICAL") if now - float(r.get("ts", 0)) <= 60]
        if len(recent) >= 8:
            out.append(Finding(component="runtime", symptom="ERROR_BURST", severity="CRITICAL", summary=f"{len(recent)} CRITICAL log lines in the last minute",
                               evidence={"messages": [r.get("message", "")[:160] for r in recent[:6]]}, diagnosis=["Repeated failures in one component; see messages. Pausing entries limits damage while the cause is fixed."], remedy="pause_entries"))
        # 9. market-data quality
        dq = t.dq
        rate = dq.reject_rate(60)
        bad = dq.bad_symbols(60)
        if rate >= 5.0 or bad:
            severe = rate >= 20 or any(b["reject_rate_pct"] >= 50 for b in bad)
            summary = (f"{rate}% of all market-data updates rejected in the last minute" if rate >= 5.0 else f"{len(bad)} instrument(s) with unusable prices: " + ", ".join(f"{b['symbol']} {b['reject_rate_pct']}%" for b in bad[:3]))
            out.append(Finding(component="data", symptom="DATA_QUALITY", severity="CRITICAL" if severe else "WARNING", summary=summary,
                               evidence={"reject_rate_pct": rate, "by_reason": dq.by_reason, "bad_symbols": bad[:5]},
                               diagnosis=["Bad prints, crossed quotes or a feed replaying stale data; marks and stop-losses on these contracts cannot be trusted while this persists."], remedy="pause_entries" if severe else None))
        if dq.record_errors and dq.recording:
            out.append(Finding(component="data", symptom="TICK_RECORDER_ERRORS", severity="WARNING", summary=f"{dq.record_errors} tick-journal write errors (disk?)", evidence={"errors": dq.record_errors}, diagnosis=["The tick journal could not be written; check disk space / permissions."], remedy="prune_history"))
        # 10. latency SLOs
        breached = t.latency.breached()
        if breached:
            worst = max(breached.items(), key=lambda kv: (kv[1]["p95"] or 0) / (kv[1]["slo_ms"] or 1))
            fill_breach = "order_submit_to_fill" in breached
            out.append(Finding(component="runtime", symptom="LATENCY_SLO", severity="CRITICAL" if fill_breach else "WARNING", summary=f"p95 {worst[0]} = {worst[1]['p95']} ms > SLO {worst[1]['slo_ms']} ms",
                               evidence={"breached": breached}, diagnosis=["Ticks or orders are being processed later than the desk's SLO allows; entries add risk that cannot be managed in time."], remedy="pause_entries" if fill_breach else None))
        # 11. backups
        if t.backups.last_error:
            out.append(Finding(component="host", symptom="BACKUP_FAILED", severity="WARNING", summary=f"last backup failed: {t.backups.last_error}", evidence={"error": t.backups.last_error, "dir": str(t.backups.dir)}, diagnosis=["Without a backup a disk failure loses the book and the audit trail."], remedy="prune_history"))
        # 12. host resources
        mem = t.health.memory().get("used_pct")
        disk = t.health.disk().get("used_pct")
        if (mem is not None and mem > 92) or (disk is not None and disk > 95):
            out.append(Finding(component="host", symptom="RESOURCE_PRESSURE", severity="WARNING", summary=f"memory {mem}% / disk {disk}% used",
                               evidence={"memory_pct": mem, "disk_pct": disk}, diagnosis=["The runtime database and logs grow all day; pruning frees space without touching trades or audit."], remedy="prune_history"))
        return out

    # ------------------------------------------------------------- lifecycle
    def _open(self, key: str) -> Incident | None:
        for inc in reversed(self.incidents):
            if inc.key == key and inc.status in OPEN_STATES:
                return inc
        return None

    async def _raise(self, f: Finding, escalated_from: str | None = None, remedy: str | None = None) -> Incident:
        remedy = remedy if remedy is not None else f.remedy
        label, risk = (self.remedies[remedy][0], self.remedies[remedy][1]) if remedy else ("", "low")
        inc = Incident(component=f.component, symptom=f.symptom, severity=f.severity, summary=f.summary, evidence=f.evidence, diagnosis=list(f.diagnosis),
                       remedy=remedy, remedy_label=label, remedy_args=f.remedy_args, risk=risk, escalated_from=escalated_from,
                       status="AWAITING_APPROVAL" if remedy else "ADVISORY", expires_at=(time.time() + self.approval_ttl) if remedy else None)
        self.incidents.append(inc)
        self.t.audit.record("GUARDIAN_INCIDENT", {"id": inc.id, "symptom": inc.symptom, "remedy": remedy, "risk": risk, "summary": inc.summary}, "guardian")
        self.t.log("WARNING" if inc.severity == "WARNING" else "CRITICAL", "guardian", f"{inc.symptom}: {inc.summary}" + (f" → proposed remedy: {label}" if remedy else " (advisory)"))
        asyncio.create_task(self._diagnose_ai(inc))
        if remedy and self._auto_allowed(inc):
            await self.apply(inc, "guardian(auto)")
        else:
            body = (f"{inc.summary}\nRemedy: {label} [{risk} risk]\nApprove in Approvals → Guardian, or Telegram: /heal approve {inc.id}" if remedy else inc.summary)
            await self.t.alerts.emit(inc.severity, "guardian", f"Guardian: {inc.symptom}" + (" — permission needed" if remedy else ""), body, dedupe_seconds=120)
        return inc

    async def _upgrade(self, inc: Incident, f: Finding) -> None:
        label, risk = self.remedies[f.remedy][0], self.remedies[f.remedy][1]
        inc.remedy, inc.remedy_label, inc.remedy_args, inc.risk = f.remedy, label, f.remedy_args, risk
        inc.diagnosis = list(f.diagnosis)
        inc.status, inc.expires_at = "AWAITING_APPROVAL", time.time() + self.approval_ttl
        self.t.audit.record("GUARDIAN_INCIDENT", {"id": inc.id, "symptom": inc.symptom, "remedy": f.remedy, "risk": risk, "summary": inc.summary, "upgraded": True}, "guardian")
        if self._auto_allowed(inc):
            await self.apply(inc, "guardian(auto)")
        else:
            await self.t.alerts.emit(inc.severity, "guardian", f"Guardian: {inc.symptom} — permission needed",
                                     f"{inc.summary}\nRemedy: {label} [{risk} risk]\nApprove in Approvals → Guardian, or Telegram: /heal approve {inc.id}", dedupe_seconds=120)

    def _auto_allowed(self, inc: Incident) -> bool:
        if inc.remedy == "flatten_all":
            return False  # always a human decision
        policy = self.auto_apply
        if policy in ("none", "", "false", "0"):
            return False
        if policy == "all":
            return True
        return RISK_ORDER.get(inc.risk, 9) <= RISK_ORDER.get(policy, -1)

    async def approve(self, incident_id: str, actor: str) -> Incident:
        inc = self.get(incident_id)
        if inc.status != "AWAITING_APPROVAL":
            raise ValueError(f"INCIDENT_NOT_PENDING:{inc.status}")
        return await self.apply(inc, actor)

    def reject(self, incident_id: str, actor: str, reason: str = "") -> Incident:
        inc = self.get(incident_id)
        if inc.status != "AWAITING_APPROVAL":
            raise ValueError(f"INCIDENT_NOT_PENDING:{inc.status}")
        inc.approved_by = actor
        self._close(inc, "REJECTED", reason or "rejected by operator")
        self._muted[inc.key] = time.time() + self.approval_ttl
        self.t.audit.record("GUARDIAN_REJECTED", {"id": inc.id, "symptom": inc.symptom, "reason": reason}, actor)
        return inc

    async def apply(self, inc: Incident, actor: str) -> Incident:
        fn = self.remedies[inc.remedy][2] if inc.remedy else None
        if fn is None:
            raise ValueError("NO_REMEDY")
        inc.status, inc.approved_by, inc.decided_at = "APPLYING", actor, time.time()
        self.t.audit.record("GUARDIAN_APPROVED", {"id": inc.id, "symptom": inc.symptom, "remedy": inc.remedy}, actor)
        try:
            result = await asyncio.wait_for(fn(inc), timeout=60)
            inc.status, inc.applied_at, inc.result = "APPLIED", time.time(), str(result or "done")
            inc.verify_by = inc.applied_at + max(20.0, self.interval * 4)
            self.applied += 1
            self.t.audit.record("GUARDIAN_APPLIED", {"id": inc.id, "remedy": inc.remedy, "result": inc.result}, actor)
            self.t.log("INFO", "guardian", f"{inc.symptom}: applied '{inc.remedy}' ({inc.result}); verifying")
        except Exception as exc:
            self.failed += 1
            self._close(inc, "FAILED", f"{type(exc).__name__}: {exc}"[:200])
            self.t.log("CRITICAL", "guardian", f"{inc.symptom}: remedy '{inc.remedy}' failed: {inc.result}")
            await self._escalate(inc, failed=True)
        return inc

    async def _escalate(self, inc: Incident, failed: bool = False) -> None:
        nxt = ESCALATION.get(inc.remedy or "")
        if inc.status == "APPLIED":
            self.failed += 1
            self._close(inc, "FAILED", "remedy applied but the symptom persists")
        if not nxt:
            return
        f = Finding(component=inc.component, symptom=inc.symptom, severity="CRITICAL", summary=inc.summary + f" — '{inc.remedy}' did not clear it", evidence=inc.evidence,
                    diagnosis=inc.diagnosis + [f"Escalating from {inc.remedy} to {nxt}."], remedy=nxt, remedy_args=inc.remedy_args)
        await self._raise(f, escalated_from=inc.id, remedy=nxt)

    def _close(self, inc: Incident, status: str, result: str) -> None:
        inc.status, inc.result, inc.decided_at = status, result, inc.decided_at or time.time()
        if status == "RESOLVED":
            self.t.audit.record("GUARDIAN_RESOLVED", {"id": inc.id, "symptom": inc.symptom, "result": result}, "guardian")
            self.t.log("INFO", "guardian", f"{inc.symptom}: {result}")

    async def _diagnose_ai(self, inc: Incident) -> None:
        llm = self.t.council.llm
        if not (self.t.settings.guardian_llm_diagnosis and llm.enabled):
            return
        try:
            text = await llm.ask(DIAGNOSIS_PROMPT, {"symptom": inc.symptom, "summary": inc.summary, "evidence": inc.evidence, "proposed_remedy": inc.remedy, "rule_diagnosis": inc.diagnosis})
            if text:
                inc.ai_diagnosis = text[:1200]
        except Exception as exc:  # diagnosis is advisory; never blocks the incident
            inc.ai_diagnosis = f"(AI diagnosis unavailable: {exc})"[:200]

    # -------------------------------------------------------------- remedies
    async def _r_reconnect_feed(self, inc: Incident) -> str:
        await self.t.feed.reconnect()
        return "feed reconnect issued"

    async def _r_relogin(self, inc: Incident) -> str:
        live = self.t.live
        if live is None:
            await self.t.feed.reconnect()
            return "no live session configured; feed reconnected"
        live.authenticated = False
        await live.connect()
        await self.t.feed.reconnect()
        return f"{live.provider} re-login ok; feed reconnected"

    async def _r_restart_loop(self, inc: Incident) -> str:
        name = inc.remedy_args.get("loop") or inc.evidence.get("loop")
        await self.t.restart_loop(name)
        return f"loop '{name}' restarted"

    async def _r_adopt_book(self, inc: Incident) -> str:
        res = await self.t.position_reconciler.tick(adopt=True)
        return f"broker book adopted ({len(res.get('diffs', []))} diffs before)"

    async def _r_pause(self, inc: Incident) -> str:
        self.t.paused = True
        self.t.audit.record("ENGINE_PAUSE", {"by": "guardian", "incident": inc.id}, inc.approved_by or "guardian")
        return "entries paused; exits and stop-losses continue"

    async def _r_prune(self, inc: Incident) -> str:
        n = self.t.db.prune_logs(keep=20000)
        return f"pruned {n} log rows"

    async def _r_flatten(self, inc: Incident) -> str:
        orders = await self.t.orders.flatten_all(OrderSource.SENTINEL, inc.approved_by or "guardian", f"GUARDIAN_FLATTEN:{inc.symptom}")
        return f"{len(orders)} exit order(s) sent"

    # ----------------------------------------------------------------- views
    def get(self, incident_id: str) -> Incident:
        for inc in self.incidents:
            if inc.id == incident_id:
                return inc
        raise KeyError(f"UNKNOWN_INCIDENT:{incident_id}")

    def pending(self) -> List[Incident]:
        return [i for i in self.incidents if i.status == "AWAITING_APPROVAL"]

    def describe(self, limit: int = 30) -> dict:
        open_ = [i for i in self.incidents if i.status in OPEN_STATES]
        return {"enabled": self.enabled, "interval_seconds": self.interval, "auto_apply": self.auto_apply, "approval_ttl_seconds": self.approval_ttl, "scans": self.scans,
                "last_scan_age_s": round(time.time() - self.last_scan_ts, 1) if self.last_scan_ts else None, "pending": len(self.pending()), "open": len(open_),
                "applied": self.applied, "healed": self.healed, "failed": self.failed, "last_error": self.last_error,
                "watch": {"loops": list(self.t.loop_specs().keys()), "feed": self.t.feed.name, "broker_session": bool(self.t.live), "remedies": {k: {"label": v[0], "risk": v[1]} for k, v in self.remedies.items()}},
                "incidents": [i.model_dump(mode="json") for i in self.incidents[-limit:][::-1]]}
