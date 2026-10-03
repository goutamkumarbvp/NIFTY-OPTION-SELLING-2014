"""Composition root. Every component gets its own principal; nothing shares credentials or capabilities.

Four independently permissioned components:
  Master AI Agent (advisory)            — MASTER_AGENT / SPECIALIST_AGENT / STRATEGY_AGENT principals
  Deterministic Risk Kernel             — RISK_KERNEL (signs decisions) + Path A / Path B monitors
  Execution Gateway                     — EXECUTION_GATEWAY (sole holder of broker credentials, signs tickets)
  Reliability Control Plane             — RELIABILITY_PLANE (allowlist only) + WATCHDOG (switch B)
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from amrt import __version__
from amrt.agents import build_master
from amrt.agents.narrative import NarrativeWriter
from amrt.brokers.registry import BrokerManager
from amrt.config import Settings
from amrt.core.clock import Clock
from amrt.core.enums import HealthState, Mode, OrderState
from amrt.core.errors import NotReady
from amrt.events.store import EventStore
from amrt.execution.accounts import Account, AccountRegistry
from amrt.execution.gateway import ExecutionGateway
from amrt.execution.lifecycle import OrderBook
from amrt.execution.paper import PaperVenue
from amrt.execution.pipeline import OrderPipeline, RiskContextBuilder
from amrt.execution.reconciliation import Reconciler
from amrt.marketdata.feed import BrokerChainSource, MarketFeed, ReplayChainSource, SimulatedSource
from amrt.marketdata.hub import MarketDataHub
from amrt.marketdata.instruments import InstrumentMaster
from amrt.marketdata.simulator import SimulatedMarket
from amrt.modes.approvals import ApprovalGate
from amrt.modes.automatic import AutomaticController
from amrt.modes.controller import ModeController
from amrt.orchestration import DecisionService
from amrt.portfolio.ledger import Ledger
from amrt.quant.costs import option_charges
from amrt.reliability.alerts import AlertService
from amrt.reliability.health import HealthRegistry
from amrt.reliability.incidents import IncidentManager
from amrt.reliability.supervisor import ReliabilitySupervisor
from amrt.reliability.watchdog import Watchdog, write_heartbeat
from amrt.risk.emergency import SafetyState
from amrt.risk.kernel import RiskKernel
from amrt.risk.monitors import PortfolioRiskMonitor, SafetyMonitor
from amrt.risk.policy import PolicyStore
from amrt.risk.protective import ProtectiveWorkflow
from amrt.security.auth import AuthService
from amrt.security.identity import Capability, Principal, PrincipalKind, require, set_denial_audit_hook, validate_matrix
from amrt.security.redact import REDACTOR
from amrt.security.signing import Signer
from amrt.services import BackupService, ConfigIntegrity, FiiDiiStore, load_events_calendar
from amrt.storage.cache import make_cache
from amrt.storage.db import Database

log = logging.getLogger("amrt.app")
CRITICAL = ("gateway", "event_store", "database", "portfolio_risk_monitor", "safety_monitor", "reconciler")


class AmrtApp:
    def __init__(self, settings: Settings | None = None, clock: Clock | None = None, sdk_factories: dict | None = None) -> None:
        s = self.settings = settings or Settings()
        if s.simulated_market and s.live_capable:
            raise NotReady("AMRT_SIMULATED_MARKET cannot be enabled in a LIVE_CAPABLE deployment")
        problems = validate_matrix()
        if problems:
            raise NotReady("permission matrix invalid", problems=problems)
        self.clock = clock or Clock()
        self.started_at = self.clock.ts()
        REDACTOR.register([v for v in s.secret_values() if v])
        Path(s.runtime_dir).mkdir(parents=True, exist_ok=True)

        # ---- storage & audit
        self.db = Database(s.db_url)
        self.db.migrate()
        self.events = EventStore(self.db, self.clock.ts)
        set_denial_audit_hook(lambda t, d, p: self.events.append(t, d, p))
        self.cache = make_cache(s.redis_url)
        self.health = HealthRegistry(self.clock)
        for name in CRITICAL:
            self.health.register(name, critical=True, period_s=2.0 if name != "reconciler" else max(2.0, s.reconcile_seconds), kind="service",
                                 restartable=name in ("reconciler",))
        for name, period in (("master_agent", s.agent_cycle_seconds), ("supervisor", s.supervisor_seconds), ("alerts", 30.0), ("cache", 30.0)):
            self.health.register(name, critical=False, period_s=period, kind={"cache": "cache", "alerts": "notifier"}.get(name, "worker"),
                                 restartable=name in ("master_agent",))

        # ---- principals (one per component) and signing keys (process-local, never exported)
        P = Principal.of
        self.p_kernel, self.p_gateway = P(PrincipalKind.RISK_KERNEL, "risk-kernel"), P(PrincipalKind.EXECUTION_GATEWAY, "execution-gateway")
        self.p_mode, self.p_protective = P(PrincipalKind.MODE_CONTROLLER, "mode-controller"), P(PrincipalKind.PROTECTIVE_WORKFLOW, "protective-workflow")
        self.p_path_a, self.p_path_b = P(PrincipalKind.PORTFOLIO_RISK_MONITOR, "path-a"), P(PrincipalKind.SAFETY_MONITOR, "path-b")
        self.p_reconciler, self.p_reliability = P(PrincipalKind.RECONCILER, "reconciler"), P(PrincipalKind.RELIABILITY_PLANE, "reliability-plane")
        self.p_monitoring, self.p_system = P(PrincipalKind.MONITORING, "monitoring"), P(PrincipalKind.MONITORING, "system")
        kernel_signer, gateway_signer = Signer("risk-kernel"), Signer("execution-gateway")

        # ---- state
        self.safety = SafetyState(self.db, self.events, Path(s.runtime_dir) / "KILL_SWITCH", self.clock.ts)
        self.policies = PolicyStore(self.db, s.hard_limits(), self.events, self.clock.ts)
        self.instruments = InstrumentMaster()
        self.hub = MarketDataHub(self.clock, self.db, max_drift_ms=s.hard_limits().max_clock_drift_ms)
        self.ledger = Ledger(self.db, self.clock)
        self.book = OrderBook(self.db, self.clock.ts)
        self.accounts = AccountRegistry()
        self.ws_listeners: list = []
        self.alerts = AlertService(self.db, self.events, self.clock, self.p_monitoring, s, publish=self._publish)
        self.incidents = IncidentManager(self.db, self.events, self.clock)
        self.mode_ctl = ModeController(self.db, self.events, self.clock, s, self.p_mode)

        # ---- risk kernel & execution
        self.kernel = RiskKernel(self.p_kernel, kernel_signer, self.clock)
        self.gateway = ExecutionGateway(self.p_gateway, gateway_signer, kernel_signer.verifier(), self.safety, self.book, self.events, self.clock, s,
                                        self.accounts, self.instruments, self.ledger, on_fill=self._on_fill, alert=self.alerts.emit)
        self.reconciler = Reconciler(self.p_reconciler, self.gateway, self.book, self.ledger, self.accounts, self.instruments, self.safety, self.events,
                                     self.clock, s, self.db)
        self.builder = RiskContextBuilder(clock=self.clock, settings=s, mode_ctl=self.mode_ctl, accounts=self.accounts, policies=self.policies,
                                          safety=self.safety, ledger=self.ledger, book=self.book, reconciler=self.reconciler, health=self.health,
                                          hub=self.hub, instruments=self.instruments)
        self.pipeline = OrderPipeline(self.p_mode, self.kernel, self.gateway, self.builder, self.events, self.instruments, s, self.clock)
        self.protective_pipeline = OrderPipeline(self.p_protective, self.kernel, self.gateway, self.builder, self.events, self.instruments, s, self.clock)
        self.protective = ProtectiveWorkflow(self.p_protective, self.protective_pipeline, self.ledger, self.book, self.accounts, self.mode_ctl,
                                             self.events, self.clock)
        self.path_a = PortfolioRiskMonitor(self.p_path_a, self.builder, self.safety, self.protective, self.alerts, self.incidents, self.health,
                                           self.mode_ctl, self.accounts, self.ledger, self.db, self.clock, self.events)
        self.path_b = SafetyMonitor(self.p_path_b, self.safety, self.health, self.hub, self.book, self.ledger, self.accounts, self.builder,
                                    self.incidents, self.alerts, self.db, self.clock, s)
        self.approvals = ApprovalGate(self.db, self.events, self.clock, self.pipeline, self.accounts, self.mode_ctl, self.alerts, self.instruments)
        self.automatic = AutomaticController(self.pipeline, self.policies, self.safety, self.health, self.mode_ctl, self.approvals, self.accounts,
                                             self.events, self.clock)

        # ---- accounts & venues: one isolated PAPER account always; LIVE accounts only in a LIVE_CAPABLE deployment
        paper = self.accounts.add(Account("PAPER-1", "paper", "PAPER", "Paper account (SIMULATED fills)",
                                          funds={"available": s.paper_capital_inr, "total": s.paper_capital_inr, "margin_used": 0.0, "sod_funds": s.paper_capital_inr,
                                                 "ts": self.clock.ts()}))
        self.paper_venue = PaperVenue(paper.account_id, self.hub, self.gateway.verifier(), self.clock, s.paper_capital_inr, s.paper_slippage_bps,
                                      max_quote_age_ms=5000, allow_simulated=s.simulated_market and not s.live_capable)
        self.gateway.register_venue(paper.account_id, self.paper_venue)
        self.brokers = BrokerManager(self.p_gateway, s, self.clock, sdk_factories=sdk_factories)
        for st in self.brokers.status():
            if not st.get("configured"):
                continue
            self.health.register(f"broker:{st['broker']}", critical=False, period_s=30.0, kind="broker", restartable=False)
            if s.live_capable:
                acct_id = self.brokers.account_id(st["broker"])
                self.accounts.add(Account(acct_id, st["broker"], "LIVE", f"{st['broker'].title()} live account"))
                self.gateway.register_venue(acct_id, self.brokers.live_venue(st["broker"], self.gateway.verifier()))
                self.health.register(f"broker:{acct_id}", critical=False, period_s=30.0, kind="broker")

        # ---- market data source (labelled by what it is)
        self.feed: MarketFeed | None = None
        source = None
        if s.data_broker and self.brokers.reader(s.data_broker) is not None:
            source = BrokerChainSource(self.brokers.reader(s.data_broker), self.instruments, self.clock)
        elif s.replay_file:
            source = ReplayChainSource(s.replay_file, self.instruments, self.clock)
        elif s.simulated_market:
            source = SimulatedSource(SimulatedMarket(self.clock, self.instruments))
        if source is not None:
            self.feed = MarketFeed(self.hub, source, s.underlying_list, self.health, self.clock, s.chain_strikes_each_side)
        self.hub.on_quote(self._paper_on_quote)

        # ---- advisory layer
        self.master = build_master(health=self.health, precheck=self.pipeline.precheck_structure)
        self.narrative = NarrativeWriter(s)
        self.fiidii = FiiDiiStore(self.db, self.events, self.clock)
        self.events_calendar = load_events_calendar(s.events_calendar_file)
        self.decisions = DecisionService(self)

        # ---- reliability
        self.backups = BackupService(self.db, s, self.events, self.clock)
        self.config = ConfigIntegrity(self.db, s, self.events, self.clock)
        self.config.record("startup")
        self.auth = AuthService(self.db, self.clock.ts, s.session_ttl_minutes, s.step_up_ttl_seconds, s.login_lockout_attempts, s.login_lockout_minutes)
        self.supervisor = ReliabilitySupervisor(self.p_reliability, self.health, self.incidents, self.alerts, self.safety, self.events, self.db,
                                                self.clock, verifier=self.verify_recovery)
        self.loop_beats: dict[str, float] = {}
        self.watchdog = Watchdog(self.safety, s.watchdog_stall_seconds, self._exposed, self.loop_beats)
        self.tasks: dict[str, asyncio.Task] = {}
        self._chain_check: dict = {"ok": None, "at": None}
        self._register_recovery()
        self.mode_ctl.verifier = self.verify_readiness
        self.mode_ctl.exposure = self.live_exposure
        self.events.append("SYSTEM_STARTED", {"version": __version__, "environment": s.environment.value, "live_capable": s.live_capable,
                                              "mode": self.mode_ctl.mode.value, "data_source": getattr(source, "name", None),
                                              "accounts": [a["account_id"] for a in self.accounts.describe()]}, self.p_system)

    # ================================================================ callbacks
    def _publish(self, topic: str, payload: dict) -> None:
        for fn in list(self.ws_listeners):
            try:
                fn(topic, payload)
            except Exception:  # noqa: BLE001
                log.debug("ws listener failed", exc_info=True)

    def _on_fill(self, rec: dict, intent, qty: int, price: float, source: str) -> None:
        acct = self.accounts.get(intent.account_id)
        inst = self.instruments.get(intent.instrument_key)
        ch = option_charges(inst.exchange, intent.side, price * qty, self.clock.ist().date())["total"]
        self.ledger.apply_fill(intent.account_id, intent.instrument_key, intent.side, qty, price, ch, inst.lot_size, simulated=acct.kind == "PAPER")
        self._publish("fill", {"intent_id": intent.intent_id, "instrument_key": intent.instrument_key, "side": intent.side.value, "qty": qty, "price": price,
                               "simulated": acct.kind == "PAPER"})

    def _paper_on_quote(self, q) -> None:
        for o in self.paper_venue.on_quote(q):
            rec = self.book.by_client_tag(o["client_tag"])
            if rec is not None and rec["state"] not in (OrderState.FILLED.value,):
                self.gateway.apply_fill(rec["intent_id"], o["filled_qty"], o["avg_price"], "FILLED", source="paper-quote")

    def _exposed(self) -> bool:
        return any(p.net_qty for p in self.ledger.open_positions())

    def live_exposure(self) -> dict:
        live = [a.account_id for a in self.accounts.live()]
        return {"live_positions": sum(1 for p in self.ledger.open_positions() if p.account_id in live),
                "live_working": sum(len(self.book.working(a)) for a in live)}

    # ================================================================ verification
    def verify_recovery(self, scope: str = "all") -> dict:
        checks: list[dict] = []

        def chk(name: str, ok: bool | None, detail: str = "") -> None:
            checks.append({"check": name, "passed": bool(ok), "detail": detail})

        chk("database_reachable", self.db.ping())
        if self._chain_check.get("at") is None or self.clock.ts() - self._chain_check["at"] > 600:
            self._chain_check = {**self.events.verify(), "at": self.clock.ts()}
        chk("audit_chain_intact", self._chain_check.get("ok"), f"checked {self._chain_check.get('checked')}")
        for name in CRITICAL:
            st = self.health.state(name)
            chk(f"component_{name}", st in (HealthState.HEALTHY, HealthState.DEGRADED) and self.health.readiness(name), st.value)
        unknown = self.book.unknown(None)
        chk("no_order_state_unknown", not unknown, f"{len(unknown)} order(s) in ORDER STATE UNKNOWN")
        for a in self.accounts.live():
            st = self.reconciler.account_status(a.account_id)
            fresh = st.get("last_ok_ts") is not None and self.clock.ts() - st["last_ok_ts"] <= 60
            chk(f"reconciled_{a.account_id}", fresh and not st.get("divergence"), "fresh and matching" if fresh else "stale or diverged")
        stale = []
        for p in self.ledger.open_positions():
            if not self.hub.freshness(p.instrument_key, 5000).fresh:
                stale.append(p.instrument_key)
        chk("held_instruments_fresh", not stale, ", ".join(stale[:5]))
        failed = [c["check"] for c in checks if not c["passed"]]
        res = {"passed": not failed, "failed": failed, "checks": checks, "at": self.clock.ts(), "scope": scope}
        self.events.append("RECOVERY_VERIFICATION", {"passed": res["passed"], "failed": failed}, self.p_reliability)
        return res

    def verify_readiness(self, target: Mode) -> dict:
        base = self.verify_recovery(scope=f"mode:{target.value}")
        checks = list(base["checks"])

        def chk(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"check": name, "passed": bool(ok), "detail": detail})

        live = [a for a in self.accounts.live() if a.enabled]
        chk("live_account_registered", bool(live), "no live account" if not live else live[0].account_id)
        for a in live:
            chk(f"risk_policy_active_{a.account_id}", self.policies.active_risk(a.account_id) is not None)
            chk(f"broker_session_{a.account_id}", self.health.state(f"broker:{a.account_id}") == HealthState.HEALTHY)
            if target == Mode.AUTOMATIC:
                chk(f"automation_policy_active_{a.account_id}", self.policies.active_automation(a.account_id) is not None)
        chk("kill_switch_released", not self.safety.state["kill_switch"].get("engaged") and not self.safety.kill_file_engaged())
        cfg = self.config.status()
        chk("configuration_known_good", cfg.get("ok") is True, ", ".join(cfg.get("differences", [])) or cfg.get("note", ""))
        if target == Mode.AUTOMATIC:
            chk("master_agent_healthy", self.health.state("master_agent") == HealthState.HEALTHY)
            chk("ai_not_suspended", not self.safety.state["ai_suspended"].get("active"))
        failed = [c["check"] for c in checks if not c["passed"]]
        return {"passed": not failed, "failed": failed, "checks": checks, "at": self.clock.ts(), "target": target.value}

    # ================================================================ snapshots for agents / API
    def security_snapshot(self) -> dict:
        now = self.clock.ts()
        denials = [e for e in self.events.tail(500, type_prefix="PERMISSION_DENIED") if now - e.ts <= 900]
        return {"failed_logins_15m": self.auth.recent_failures(), "locked_accounts": self.auth.recent_lockouts(), "permission_denials_15m": len(denials),
                "event_chain_ok": self._chain_check.get("ok"), "config_integrity_ok": self.config.status().get("ok"),
                "kill_file_engaged": self.safety.kill_file_engaged(), "secrets_in_logs": False, "injection_flags_15m": 0,
                "live_orders_enabled": self.settings.live_capable, "environment": self.settings.environment.value}

    def validated_backtests(self) -> dict:
        return self.db.kv_get("validated_backtests", {}) or {}

    # ================================================================ recovery handlers (allowlist)
    def _register_recovery(self) -> None:
        sup = self.supervisor
        if self.feed is not None:
            sup.register(self.feed.component, "RECONNECT_STREAM", lambda c: self.feed.reconnect())
            sup.register(self.feed.component, "RESTART_WORKER", lambda c: self._restart("feed"))
        sup.register("reconciler", "RESTART_WORKER", lambda c: self._restart("reconciler"))
        sup.register("master_agent", "RESTART_WORKER", lambda c: self._restart("decisions"))
        sup.register("cache", "REBUILD_CACHE", lambda c: (self.cache.flush(), True)[1])
        for a in self.master.all_agents():
            sup.register(f"agent:{a.name}", "RESTART_WORKER", lambda c: True)   # agents are stateless; the next cycle re-runs them
        for st in self.brokers.status():
            name = f"broker:{st['broker']}"
            if name in self.health.components and self.brokers.reader(st["broker"]) is not None:
                sup.register(name, "RENEW_SESSION", lambda c, b=st["broker"]: self._renew(b))

    async def _renew(self, broker: str) -> bool:
        reader = self.brokers.reader(broker)
        await reader.login()
        self.health.beat(f"broker:{broker}", HealthState.HEALTHY, reason="session renewed")
        return True

    def _restart(self, name: str) -> bool:
        t = self.tasks.get(name)
        if t is not None:
            t.cancel()
        factory = self._loop_factories().get(name)
        if factory is None:
            return False
        self.tasks[name] = asyncio.get_running_loop().create_task(factory(), name=f"amrt-{name}")
        return True

    # ================================================================ owner actions (called by the API with the session principal)
    def engage_kill(self, principal: Principal, reason: str) -> dict:
        info = self.safety.engage_kill(principal, reason)
        self.alerts.emit("CRITICAL", "safety", "KILL SWITCH ENGAGED", f"by {principal.id}: {reason}")
        return info

    def release_kill(self, principal: Principal) -> dict:
        ver = self.verify_recovery("release_kill")
        self.safety.release_kill(principal, ver)
        return ver

    def release_freeze(self, principal: Principal, codes: list[str] | None) -> dict:
        ver = self.verify_recovery("release_freeze")
        self.safety.release_freeze(principal, codes, ver)
        return ver

    def release_recovery_lock(self, principal: Principal) -> dict:
        ver = self.verify_recovery("release_recovery_lock")
        self.safety.release_recovery_lock(principal, ver)
        return ver

    def unquarantine(self, principal: Principal, component: str) -> None:
        require(principal, Capability.UNQUARANTINE, "unquarantine")
        self.health.unquarantine(component)
        self.events.append("COMPONENT_UNQUARANTINED", {"component": component}, principal)

    async def quick_exit(self, principal: Principal, account_id: str) -> dict:
        require(principal, Capability.MANUAL_QUICK_EXIT, "manual quick exit")
        self.events.append("MANUAL_QUICK_EXIT_REQUESTED", {"account_id": account_id}, principal)
        self.alerts.emit("CRITICAL", "safety", "MANUAL QUICK EXIT", f"{principal.id} requested flatten of {account_id}")
        return await self.protective.flatten(account_id, "MANUAL QUICK EXIT", requester=principal)

    # ================================================================ loops
    def _loop_factories(self) -> dict:
        s = self.settings
        f = {
            "heartbeat": lambda: self._every(1.0, self._heartbeat, "event_loop"),
            "path_a": lambda: self._every(s.risk_monitor_seconds, self._path_a, "portfolio_risk_monitor"),
            "path_b": lambda: self._every(s.safety_monitor_seconds, self._path_b, "safety_monitor"),
            "reconciler": lambda: self._every(s.reconcile_seconds, self._reconcile, "reconciler"),
            "decisions": lambda: self._every(s.agent_cycle_seconds, self.decisions.cycle, "master_agent", initial_delay=3.0),
            "supervisor": lambda: self._every(s.supervisor_seconds, self._supervise, "supervisor", initial_delay=5.0),
            "flush": lambda: self._every(5.0, self._flush, "flush"),
            "integrity": lambda: self._every(60.0, self._integrity, "integrity", initial_delay=2.0),
            "backup": lambda: self._every(s.backup_interval_minutes * 60, self._backup, "backup", initial_delay=60.0),
            "brokers": lambda: self._every(30.0, self._broker_status, "brokers"),
        }
        if self.feed is not None:
            f["feed"] = lambda: self._every(s.chain_poll_seconds, self.feed.poll_once, self.feed.component)
        return f

    async def _every(self, period: float, fn, beat_name: str, initial_delay: float = 0.0) -> None:
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while True:
            t0 = time.monotonic()
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a loop failure is surfaced through health, never kills the loop
                log.exception("loop %s failed", beat_name)
            self.loop_beats[beat_name] = time.monotonic()
            await asyncio.sleep(max(0.05, period - (time.monotonic() - t0)))

    async def _heartbeat(self) -> None:
        self.safety.sync_from_file()
        ok = self.db.ping()
        if ok:
            self.health.beat("database", HealthState.HEALTHY)
            self.health.beat("event_store", HealthState.HEALTHY)
        else:
            self.health.fail("database", "ping failed")
            self.health.fail("event_store", "database unreachable")
        self.health.beat("gateway", HealthState.HEALTHY, reason=f"{len(self.gateway.venues)} venue(s)", kill_switch_b=self.safety.kill_file_engaged())
        try:
            self.health.beat("cache", HealthState.HEALTHY if self.cache.ping() else HealthState.DEGRADED)
        except Exception as e:  # noqa: BLE001
            self.health.fail("cache", str(e))
        dh = self.alerts.delivery_health()
        self.health.beat("alerts", HealthState.DEGRADED if dh.get("degraded") else HealthState.HEALTHY, reason=str(dh)[:200])
        write_heartbeat(Path(self.settings.runtime_dir), {"mode": self.mode_ctl.mode.value})
        self._publish("heartbeat", {"ts": self.clock.ts(), "mode": self.mode_ctl.mode.value, "blockers": self.safety.new_risk_blockers()})

    async def _path_a(self) -> None:
        await self.path_a.tick()
        self.health.beat("portfolio_risk_monitor", HealthState.HEALTHY)

    async def _path_b(self) -> None:
        await self.path_b.tick()
        self.health.beat("safety_monitor", HealthState.HEALTHY)

    async def _reconcile(self) -> None:
        res = await self.reconciler.reconcile_all()
        bad = [k for k, v in res.items() if v.get("last_error")]
        self.health.beat("reconciler", HealthState.DEGRADED if bad else HealthState.HEALTHY, reason=", ".join(bad))

    async def _supervise(self) -> None:
        await self.supervisor.tick()
        self.health.beat("supervisor", HealthState.HEALTHY)

    def _flush(self) -> None:
        self.hub.flush()

    def _integrity(self) -> None:
        if self._chain_check.get("at") is None or self.clock.ts() - self._chain_check["at"] > 600:
            self._chain_check = {**self.events.verify(), "at": self.clock.ts()}
            if self._chain_check.get("ok") is False:
                self.safety.freeze(self.p_path_b, "AUDIT_CHAIN_BROKEN", f"first bad seq {self._chain_check.get('first_bad_seq')}")
                self.alerts.emit("CRITICAL", "security", "Audit hash chain verification failed", str(self._chain_check))
        cfg = self.config.status()
        if cfg.get("ok") is False:
            self.alerts.emit("WARNING", "security", "Configuration differs from known-good", ", ".join(cfg.get("differences", [])), dedupe_s=3600)

    def _backup(self) -> None:
        self.backups.run(Principal.of(PrincipalKind.OPERATOR, "scheduler"), "scheduled")

    async def _broker_status(self) -> None:
        for st in self.brokers.status():
            name = f"broker:{st['broker']}"
            if name not in self.health.components:
                continue
            if st.get("authenticated"):
                self.health.beat(name, HealthState.HEALTHY, reason="session authenticated")
            elif st.get("last_error"):
                self.health.fail(name, st["last_error"])
            for a in self.accounts.live():
                if a.broker == st["broker"]:
                    if st.get("authenticated"):
                        self.health.beat(f"broker:{a.account_id}", HealthState.HEALTHY)
                    else:
                        self.health.fail(f"broker:{a.account_id}", st.get("last_error") or "not authenticated")

    async def start(self) -> None:
        """Log in to configured brokers (read-only data first), start every loop and the watchdog."""
        if self.feed is not None and isinstance(self.feed.source, BrokerChainSource):
            try:
                await self.feed.source.reader.login()
                self.feed.state.authenticated = True
            except Exception as e:  # noqa: BLE001
                self.health.fail(self.feed.component, f"login failed: {type(e).__name__}")
                self.alerts.emit("CRITICAL", "data", "Broker data login failed", type(e).__name__)
        for name, factory in self._loop_factories().items():
            self.tasks[name] = asyncio.get_running_loop().create_task(factory(), name=f"amrt-{name}")
        self.watchdog.start()

    async def stop(self) -> None:
        self.watchdog.stop()
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.hub.flush()
        self.master.close()
        self.events.append("SYSTEM_STOPPED", {"uptime_s": round(self.clock.ts() - self.started_at, 1)}, self.p_system)

    # ================================================================ views
    def status(self) -> dict:
        s = self.settings
        return {"product": "AI Market Risk Terminal", "version": __version__, "environment": s.environment.value, "live_capable": s.live_capable,
                "mode": self.mode_ctl.describe(), "safety": self.safety.snapshot(), "blockers": self.safety.new_risk_blockers(),
                "data_source": getattr(getattr(self.feed, "source", None), "name", None), "simulated_market": s.simulated_market,
                "accounts": self.accounts.describe(), "health": self.health.describe(), "gateway": self.gateway.describe(),
                "hub": self.hub.describe(), "config": self.config.status(), "audit_chain": self._chain_check,
                "readiness": self.readiness_summary()}

    def readiness_summary(self) -> dict:
        s = self.settings
        if not s.live_capable:
            return {"status": "NOT READY", "reason": "PAPER_ONLY deployment: live order flow disabled by configuration"}
        ver = self.verify_readiness(Mode.MANUAL)
        return {"status": "READY" if ver["passed"] else "NOT READY", "failed": ver["failed"]}
