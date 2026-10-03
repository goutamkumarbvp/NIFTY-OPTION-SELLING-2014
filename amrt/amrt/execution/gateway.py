"""Execution Gateway — the only component that can submit or cancel orders at a venue.

Path: Intent → Risk Kernel decision → Gateway checks → persist → submit → ack /
fill / reject / UNKNOWN → reconciliation. The gateway:
* accepts requests only from MODE_CONTROLLER / PROTECTIVE_WORKFLOW principals;
* persists the intent first (UNIQUE idempotency key ⇒ no duplicate orders);
* verifies the Risk Kernel signature, intent hash, approval and expiry;
* checks kill switch B (local file) itself, independently of the kernel;
* enforces paper isolation (PAPER intents → paper venue only; live venues only
  when the deployment is LIVE_CAPABLE);
* refuses a second working order on an instrument with a working/UNKNOWN order;
* signs a single-use SubmissionTicket the venue must verify;
* treats timeouts and ambiguous errors as ORDER STATE UNKNOWN — never retries.
"""
from __future__ import annotations

import asyncio
import logging

from amrt.core.enums import Mode, OrderState
from amrt.core.errors import BrokerRejected, PaperIsolationViolation, PermissionDenied
from amrt.core.ids import new_id
from amrt.execution.intents import BrokerOrderRequest, OrderIntent, SubmissionTicket
from amrt.security.identity import Capability, Principal, require

log = logging.getLogger("amrt.gateway")
TICKET_TTL = 10.0


class ExecutionGateway:
    def __init__(self, principal: Principal, signer, kernel_verifier, safety, order_book, events, clock, settings, accounts, instruments, ledger,
                 on_fill=None, alert=None) -> None:
        require(principal, Capability.SUBMIT_LIVE_ORDER, "construct gateway")
        self.principal = principal
        self._signer = signer
        self._kernel_verifier = kernel_verifier
        self.safety = safety
        self.book = order_book
        self.events = events
        self.clock = clock
        self.settings = settings
        self.accounts = accounts
        self.instruments = instruments
        self.ledger = ledger
        self.venues: dict[str, object] = {}
        self.on_fill = on_fill
        self.alert = alert
        self.submitted = 0
        self.rejected = 0
        self.unknown = 0
        self.last_ok_ts = 0.0

    def verifier(self):
        return self._signer.verifier()

    def register_venue(self, account_id: str, venue) -> None:
        if venue.kind == "LIVE" and not self.settings.live_capable:
            raise PaperIsolationViolation("live venue registration refused: deployment is not LIVE_CAPABLE")
        self.venues[account_id] = venue

    # ---------------------------------------------------------------- helpers
    def _event(self, type_: str, payload: dict, intent: OrderIntent) -> None:
        self.events.append(type_, payload, self.principal, correlation_id=intent.decision_id or intent.correlation_id, causation_id=intent.intent_id,
                           idempotency_key=f"{type_}:{intent.intent_id}" if type_ in ("ORDER_INTENT_PERSISTED",) else None)

    def _reject(self, intent: OrderIntent, reason: str, extra: dict | None = None) -> dict:
        self.rejected += 1
        rec = self.book.transition(intent.intent_id, OrderState.REJECTED_PRE_TRADE, note=reason, last_error=reason)
        self._event("ORDER_REJECTED_PRE_TRADE", {"intent_id": intent.intent_id, "reason": reason, **(extra or {})}, intent)
        return rec

    def _request(self, intent: OrderIntent, client_tag: str) -> BrokerOrderRequest:
        inst = self.instruments.get(intent.instrument_key)
        acct = self.accounts.get(intent.account_id)
        return BrokerOrderRequest(account_id=intent.account_id, instrument_key=intent.instrument_key, trading_symbol=inst.trading_symbol,
                                  broker_ref=inst.broker_refs.get(acct.broker, {}), exchange=inst.exchange.value, segment=inst.segment.value,
                                  side=intent.side, quantity=intent.quantity, order_type=intent.order_type, limit_price=intent.limit_price,
                                  product=intent.product, validity=intent.validity, client_tag=client_tag)

    def _ticket(self, intent: OrderIntent, client_tag: str) -> SubmissionTicket:
        now = self.clock.ts()
        t = SubmissionTicket(ticket_id=new_id("TK"), intent_id=intent.intent_id, intent_hash=intent.intent_hash(), idempotency_key=intent.idempotency_key,
                             client_tag=client_tag, account_id=intent.account_id, broker=intent.broker, mode=intent.mode, environment=intent.environment,
                             instrument_key=intent.instrument_key, side=intent.side, quantity=intent.quantity, issued_at=now, expires_at=now + TICKET_TTL)
        return t.model_copy(update={"signature": self._signer.sign(t.payload())})

    # ------------------------------------------------------------------- api
    async def execute(self, caller: Principal, intent: OrderIntent, decision) -> dict:
        require(caller, Capability.REQUEST_EXECUTION, "request execution")
        acct = self.accounts.get(intent.account_id)
        rec, created = self.book.create(intent, decision.model_dump(mode="json") if decision is not None else None, simulated=acct.kind == "PAPER",
                                        tag_len=getattr(self.venues.get(intent.account_id), "tag_len", 20))
        if not created:
            self._event("DUPLICATE_INTENT_SUPPRESSED", {"intent_id": intent.intent_id, "existing": rec["intent_id"], "state": rec["state"]}, intent)
            return {**rec, "duplicate": True}
        self._event("ORDER_INTENT_PERSISTED", {"intent": intent.model_dump(mode="json")}, intent)

        # 1. risk decision must be genuine, approved, for this exact intent, unexpired
        if decision is None:
            return self._reject(intent, "NO_RISK_DECISION")
        if not self._kernel_verifier.verify(decision.signed_payload(), decision.signature):
            return self._reject(intent, "RISK_DECISION_SIGNATURE_INVALID")
        if decision.intent_id != intent.intent_id or decision.intent_hash != intent.intent_hash():
            return self._reject(intent, "RISK_DECISION_NOT_FOR_THIS_INTENT")
        if not decision.approved:
            return self._reject(intent, "RISK_KERNEL_REJECTED", {"failed_rules": decision.failed_rules})
        if self.clock.ts() > decision.expires_at:
            return self._reject(intent, "RISK_DECISION_EXPIRED")
        # 2. kill switch B — independent of the kernel
        if self.safety.kill_file_engaged() and not intent.reduce_only:
            return self._reject(intent, "KILL_SWITCH_B_ENGAGED")
        # 3. paper isolation and venue routing
        venue = self.venues.get(intent.account_id)
        if intent.mode == Mode.PAPER and (acct.kind != "PAPER" or venue is None or venue.kind != "PAPER"):
            self.events.append("PAPER_ISOLATION_VIOLATION", {"intent_id": intent.intent_id, "account": intent.account_id}, self.principal)
            return self._reject(intent, "PAPER_ISOLATION_VIOLATION")
        if acct.kind == "LIVE":
            if intent.mode == Mode.PAPER or not self.settings.live_capable or venue is None or venue.kind != "LIVE":
                return self._reject(intent, "LIVE_ROUTE_UNAVAILABLE")
        if venue is None:
            return self._reject(intent, "NO_VENUE")
        # 4. one working order per instrument (duplicate-risk guard, defence in depth)
        clash = [w for w in self.book.working(intent.account_id) if w["instrument_key"] == intent.instrument_key and w["intent_id"] != intent.intent_id]
        if clash:
            return self._reject(intent, "WORKING_ORDER_EXISTS", {"working": [c["intent_id"] for c in clash]})
        # 5. persist SUBMITTING, then submit exactly once
        req = self._request(intent, rec["client_tag"])
        ticket = self._ticket(intent, rec["client_tag"])
        self.book.transition(intent.intent_id, OrderState.SUBMITTING, note=f"ticket {ticket.ticket_id}")
        self._event("ORDER_SUBMITTING", {"intent_id": intent.intent_id, "client_tag": rec["client_tag"], "venue": venue.name}, intent)
        self.submitted += 1
        try:
            ack = await asyncio.wait_for(venue.submit(req, ticket), timeout=self.settings.order_ack_timeout_seconds)
        except BrokerRejected as exc:
            rec = self.book.transition(intent.intent_id, OrderState.REJECTED, note=str(exc), last_error=str(exc))
            self._event("ORDER_REJECTED", {"intent_id": intent.intent_id, "message": str(exc)}, intent)
            return rec
        except PermissionDenied as exc:  # the venue refused the ticket: definitively not sent
            rec = self.book.transition(intent.intent_id, OrderState.REJECTED, note=f"venue refused ticket: {exc}", last_error=str(exc))
            self.events.append("VENUE_TICKET_REFUSED", {"intent_id": intent.intent_id, "error": str(exc), "code": exc.code}, self.principal)
            return rec
        except (TimeoutError, Exception) as exc:  # noqa: B014 — every ambiguity is UNKNOWN, never retried
            self.unknown += 1
            msg = f"{type(exc).__name__}: {exc}"
            rec = self.book.transition(intent.intent_id, OrderState.UNKNOWN, note="no definitive broker answer — awaiting reconciliation", last_error=msg)
            self._event("ORDER_STATE_UNKNOWN", {"intent_id": intent.intent_id, "error": msg}, intent)
            if self.alert:
                self.alert("CRITICAL", "execution", f"ORDER STATE UNKNOWN: {intent.trading_symbol}", f"{intent.side} {intent.quantity} — {msg}. New risk blocked until reconciled.")
            return rec
        self.last_ok_ts = self.clock.ts()
        return self.apply_ack(intent, ack)

    def apply_ack(self, intent: OrderIntent, ack) -> dict:
        if not ack.accepted or ack.status == "REJECTED":
            rec = self.book.transition(intent.intent_id, OrderState.REJECTED, note=ack.message, broker_order_id=ack.broker_order_id, last_error=ack.message)
            self._event("ORDER_REJECTED", {"intent_id": intent.intent_id, "message": ack.message, "broker_order_id": ack.broker_order_id}, intent)
            return rec
        if ack.status == "UNKNOWN":
            rec = self.book.transition(intent.intent_id, OrderState.UNKNOWN, note=ack.message, broker_order_id=ack.broker_order_id)
            self._event("ORDER_STATE_UNKNOWN", {"intent_id": intent.intent_id, "message": ack.message}, intent)
            return rec
        rec = self.book.transition(intent.intent_id, OrderState.ACKNOWLEDGED, note=ack.message, broker_order_id=ack.broker_order_id)
        self._event("ORDER_ACKNOWLEDGED", {"intent_id": intent.intent_id, "broker_order_id": ack.broker_order_id}, intent)
        if ack.filled_qty:
            rec = self.apply_fill(intent.intent_id, ack.filled_qty, ack.avg_price, "FILLED" if ack.status == "FILLED" else "PARTIAL", source="ack")
        return rec

    def apply_fill(self, intent_id: str, cum_filled: int, avg_price: float, status: str, source: str) -> dict:
        """Apply a cumulative fill report idempotently (only the increment is booked)."""
        rec = self.book.get(intent_id)
        intent = OrderIntent.model_validate(rec["intent"])
        inc = int(cum_filled) - int(rec["filled_qty"])
        if inc > 0:
            prev_val = rec["filled_qty"] * rec["avg_price"]
            inc_price = (cum_filled * avg_price - prev_val) / inc if cum_filled else avg_price
            if self.on_fill:
                self.on_fill(rec, intent, inc, round(inc_price, 4), source)
        state = OrderState.FILLED if (status == "FILLED" or cum_filled >= rec["quantity"]) else OrderState.PARTIALLY_FILLED
        if OrderState(rec["state"]) == state and inc <= 0:
            return rec
        rec = self.book.transition(intent_id, state, note=f"fill {cum_filled}/{rec['quantity']} via {source}", filled_qty=int(cum_filled), avg_price=float(avg_price))
        self.events.append("ORDER_FILL", {"intent_id": intent_id, "cum_filled": cum_filled, "avg_price": avg_price, "increment": inc, "source": source},
                           self.principal, correlation_id=intent.decision_id or intent.correlation_id, causation_id=intent_id)
        return rec

    async def cancel(self, caller: Principal, intent_id: str, reason: str) -> dict:
        require(caller, Capability.REQUEST_EXECUTION, "cancel order")
        rec = self.book.get(intent_id)
        if rec is None or self.book.is_final(rec):
            return rec or {}
        intent = OrderIntent.model_validate(rec["intent"])
        venue = self.venues.get(intent.account_id)
        if venue is None or not rec.get("broker_order_id"):
            return rec
        ticket = self._ticket(intent, rec["client_tag"])
        try:
            ack = await asyncio.wait_for(venue.cancel(rec["broker_order_id"], ticket), timeout=self.settings.order_ack_timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            self.book.note(intent_id, f"cancel outcome unknown: {exc}")
            if OrderState(rec["state"]) != OrderState.UNKNOWN:
                rec = self.book.transition(intent_id, OrderState.UNKNOWN, note="cancel outcome unknown", last_error=str(exc))
            return rec
        if ack.status == "CANCELLED":
            rec = self.book.transition(intent_id, OrderState.CANCELLED, note=reason)
            self._event("ORDER_CANCELLED", {"intent_id": intent_id, "reason": reason}, intent)
        return rec

    def describe(self) -> dict:
        return {"submitted": self.submitted, "rejected": self.rejected, "unknown": self.unknown, "venues": {k: v.kind for k, v in self.venues.items()},
                "kill_switch_b": self.safety.kill_file_engaged(), "last_ok_ts": self.last_ok_ts}
