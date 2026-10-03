"""Risk Kernel + Execution Gateway + order lifecycle on the live rig (fake broker behind the real venue)."""
import pytest
from harness import live_app
from pydantic import ValidationError

from amrt.core.enums import Environment, Mode, OrderPurpose, OrderType, Side
from amrt.core.errors import PaperIsolationViolation, PermissionDenied
from amrt.execution.intents import BrokerOrderRequest, SubmissionTicket
from amrt.risk.kernel import RiskKernel
from amrt.security.identity import Principal, PrincipalKind
from amrt.security.signing import Signer

pytestmark = [pytest.mark.unit, pytest.mark.acceptance]


@pytest.fixture
async def rig(monkeypatch, tmp_path):
    r = live_app(monkeypatch, tmp_path)
    await r.ready()
    return r


def failed(res):
    return set(res["decision"]["failed_rules"])


async def test_happy_path_and_rule_rejections(rig):
    ok = await rig.app.pipeline.submit(rig.intent())
    assert ok["order"]["state"] == "FILLED" and rig.session.places == 1
    r = await rig.app.pipeline.submit(rig.intent(strike=25100, lots=5))
    assert "RK-018" in failed(r) and r["order"]["state"] == "REJECTED_PRE_TRADE"
    r = await rig.app.pipeline.submit(rig.intent(strike=25200, order_type=OrderType.MARKET, price=None))
    assert "RK-023" in failed(r)
    r = await rig.app.pipeline.submit(rig.intent(strike=24900, price=1.0))           # far outside the collar
    assert "RK-023" in failed(r)
    assert rig.session.places == 1                                                    # nothing rejected reached the broker


async def test_kernel_rejection_cannot_be_overridden(rig):
    app = rig.app
    it = rig.intent(lots=5)
    d = app.kernel.evaluate(it, app.builder.build(it))
    assert not d.approved
    forged = d.model_copy(update={"approved": True, "failed_rules": []})            # flip the verdict, keep the old signature
    rec = await app.gateway.execute(app.p_mode, it, forged)
    assert rec["state"] == "REJECTED_PRE_TRADE" and rec["last_error"] == "RISK_DECISION_SIGNATURE_INVALID"
    rogue = RiskKernel(Principal.of(PrincipalKind.RISK_KERNEL, "rogue"), Signer("rogue"), app.clock)   # a second kernel with its own key
    it2 = rig.intent(strike=25100)
    rec = await app.gateway.execute(app.p_mode, it2, rogue.evaluate(it2, app.builder.build(it2)))
    assert rec["last_error"] == "RISK_DECISION_SIGNATURE_INVALID"
    it3, it4 = rig.intent(strike=25200), rig.intent(strike=24800)
    good = app.kernel.evaluate(it3, app.builder.build(it3))
    assert (await app.gateway.execute(app.p_mode, it4, good))["last_error"] == "RISK_DECISION_NOT_FOR_THIS_INTENT"
    it5 = rig.intent(strike=24900)
    d5 = app.kernel.evaluate(it5, app.builder.build(it5))
    app.clock.advance(10)
    assert (await app.gateway.execute(app.p_mode, it5, d5))["last_error"] == "RISK_DECISION_EXPIRED"
    assert (await app.gateway.execute(app.p_mode, rig.intent(strike=24900, side=Side.SELL), None))["last_error"] == "NO_RISK_DECISION"
    assert rig.session.places == 0


async def test_master_and_agents_cannot_execute(rig):
    app = rig.app
    it = rig.intent()
    d = app.kernel.evaluate(it, app.builder.build(it))
    for p in (app.master.principal, app.master.strategy.principal, app.master.specialists[0].principal, app.p_reliability, app.p_monitoring):
        with pytest.raises(PermissionDenied):
            await app.gateway.execute(p, it, d)
    with pytest.raises(PermissionDenied):
        RiskKernel(app.master.principal, Signer("x"), app.clock)
    assert rig.session.places == 0


async def test_venue_refuses_anything_without_a_valid_ticket(rig):
    venue = rig.app.gateway.venues[rig.ACCOUNT]
    req = BrokerOrderRequest(account_id=rig.ACCOUNT, instrument_key=rig.key(25000, "CE"), trading_symbol="X", exchange="NSE", segment="NSE_FO",
                             side=Side.BUY, quantity=65, order_type=OrderType.LIMIT, limit_price=60.0, product="NRML", validity="DAY", client_tag="T")
    with pytest.raises(PermissionDenied):
        await venue.submit(req, None)
    fake = SubmissionTicket(ticket_id="T", intent_id="I", intent_hash="h", idempotency_key="k", client_tag="T", account_id=rig.ACCOUNT, broker="fakebroker",
                            mode=Mode.MANUAL, environment=Environment.LIVE_CAPABLE, instrument_key=req.instrument_key, side=Side.BUY, quantity=65,
                            issued_at=rig.app.clock.ts(), expires_at=rig.app.clock.ts() + 10, signature="00" * 32)
    with pytest.raises(PermissionDenied):
        await venue.submit(req, fake)
    paper_ticket = fake.model_copy(update={"mode": Mode.PAPER})
    paper_ticket = paper_ticket.model_copy(update={"signature": rig.app.gateway._signer.sign(paper_ticket.payload())})
    with pytest.raises(PaperIsolationViolation):
        await venue.submit(req, paper_ticket)
    assert rig.session.places == 0


async def test_ambiguous_timeout_is_unknown_and_never_duplicated(rig):
    app = rig.app
    rig.session.behaviour = "timeout_accepted"          # broker took it, the answer was lost
    it = rig.intent(idem="D1")
    r = await app.pipeline.submit(it)
    assert r["order"]["state"] == "ORDER STATE UNKNOWN" and rig.session.places == 1
    rig.session.behaviour = "fill"
    again = await app.pipeline.submit(rig.intent(idem="D1"))                         # same decision retried → same idempotency key
    assert again["order"].get("duplicate") is True and rig.session.places == 1
    other = await app.pipeline.submit(rig.intent(strike=25100))                      # any new risk while UNKNOWN exists is refused
    assert "RK-013" in failed(other)
    same = await app.pipeline.submit(rig.intent(idem="D2"))
    assert {"RK-013", "RK-014"} & failed(same) and rig.session.places == 1
    assert any(a["title"].startswith("ORDER STATE UNKNOWN") for a in app.alerts.recent(20))
    await app.reconciler.reconcile_all()                                               # broker truth resolves it
    rec = app.book.get(it.intent_id)
    assert rec["state"] == "FILLED" and rec["filled_qty"] == 65
    assert app.ledger.position(rig.ACCOUNT, it.instrument_key).net_qty == 65
    assert app.book.unknown(None) == []


async def test_unknown_not_at_broker_needs_repeated_reads_and_grace(rig):
    app = rig.app
    rig.session.behaviour = "timeout"                   # never reached the broker
    it = rig.intent()
    await app.pipeline.submit(it)
    await app.reconciler.reconcile_all()
    assert app.book.get(it.intent_id)["state"] == "ORDER STATE UNKNOWN"              # one read is not proof
    app.clock.advance(10)
    await app.reconciler.reconcile_all()
    assert app.book.get(it.intent_id)["state"] == "ORDER STATE UNKNOWN"              # grace (30 s) not elapsed
    app.clock.advance(25)
    await app.reconciler.reconcile_all()
    assert app.book.get(it.intent_id)["state"] == "NOT_FOUND_AT_BROKER"


async def test_definitive_rejection_and_connection_errors(rig):
    rig.session.behaviour = "reject"
    r = await rig.app.pipeline.submit(rig.intent())
    assert r["order"]["state"] == "REJECTED"
    rig.session.behaviour = "error"
    r = await rig.app.pipeline.submit(rig.intent(strike=25100))
    assert r["order"]["state"] == "ORDER STATE UNKNOWN"


async def test_kill_switches_block_new_risk_but_allow_exits(rig):
    app = rig.app
    await app.pipeline.submit(rig.intent(lots=2))
    app.safety.engage_kill(app.p_path_b, "test")
    r = await app.pipeline.submit(rig.intent(strike=25100))
    assert "RK-001" in failed(r)
    ex = await app.pipeline.submit(rig.intent(side=Side.SELL, lots=1, purpose=OrderPurpose.EXIT, reduce_only=True))
    assert ex["order"]["state"] == "FILLED"
    # switch B alone (file), with the database switch still showing clear: the gateway checks it directly
    app.safety.state["kill_switch"] = {"engaged": False}
    app.safety.state["freeze"] = {"active": False, "reasons": []}
    it = rig.intent(strike=25200)
    d = app.kernel.evaluate(it, app.builder.build(it))
    assert not d.approved and "RK-001" in d.failed_rules                              # kernel also reads the file
    approved = app.kernel.evaluate(it, __import__("dataclasses").replace(app.builder.build(it), kill_file_engaged=False))
    rec = await app.gateway.execute(app.p_mode, it, approved)
    assert approved.approved and rec["last_error"] == "KILL_SWITCH_B_ENGAGED"


async def test_freeze_and_recovery_lock_block_new_risk(rig):
    app = rig.app
    app.safety.freeze(app.p_path_b, "TEST", "x")
    assert "RK-002" in failed(await app.pipeline.submit(rig.intent()))
    app.safety.state["freeze"] = {"active": False, "reasons": []}
    app.safety.set_recovery_lock(app.p_reliability, "INC-1", "x")
    assert "RK-003" in failed(await app.pipeline.submit(rig.intent(strike=25100)))


async def test_stale_data_and_unhealthy_services_block_new_risk(rig):
    app = rig.app
    app.clock.advance(5)                                                              # quotes now older than 3 s
    assert "RK-010" in failed(await app.pipeline.submit(rig.intent()))
    rig.fresh_market()
    app.health.fail("safety_monitor", "crashed")
    assert "RK-025" in failed(await app.pipeline.submit(rig.intent(strike=25100)))


async def test_naked_short_needs_policy(monkeypatch, tmp_path):
    rig = live_app(monkeypatch, tmp_path, naked=False)
    await rig.ready()
    r = await rig.app.pipeline.submit(rig.intent(side=Side.SELL))
    assert "RK-024" in failed(r)
    await rig.app.pipeline.submit(rig.intent(side=Side.BUY, strike=25100))
    hedged = await rig.app.pipeline.submit(rig.intent(side=Side.SELL, strike=25000))
    assert hedged["decision"]["approved"]


async def test_paper_mode_cannot_reach_live(monkeypatch, tmp_path):
    rig = live_app(monkeypatch, tmp_path)
    await rig.ready()
    app = rig.app
    app.mode_ctl.mode = Mode.PAPER
    with pytest.raises(ValidationError):                 # a PAPER intent cannot name a live broker
        rig.intent(mode=Mode.PAPER)
    r = await app.pipeline.submit(rig.intent(mode=Mode.MANUAL))                       # live intent while in PAPER mode
    assert "RK-004" in failed(r) and rig.session.places == 0
    paper_key = rig.key(25000, "CE")
    it = rig.intent(mode=Mode.PAPER, account="PAPER-1")
    assert it.broker == "paper" and it.instrument_key == paper_key
    await app.pipeline.submit(it)
    assert rig.session.places == 0                                                    # paper never touches the live session
