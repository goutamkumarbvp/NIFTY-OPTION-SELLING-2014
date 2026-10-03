"""Pre-authorized protective workflows: flatten an account, exit one position.

They only ever create reduce-only MARKET intents, which still pass the Risk
Kernel (reduce-only sizing, no duplicate exits, no exits on instruments with an
UNKNOWN order) and the Execution Gateway. Residual exposure is reported after
every run; a failed exit keeps the emergency state and is retried on the next
monitor cycle (bounded), never doubled.
"""
from __future__ import annotations

import logging

from amrt.core.enums import AuthorizationKind, Mode, OrderPurpose, OrderType, Origin, Side
from amrt.core.errors import PermissionDenied
from amrt.execution.intents import AuthorizationContext
from amrt.security.identity import Capability, Principal, require

log = logging.getLogger("amrt.protective")


class ProtectiveWorkflow:
    def __init__(self, principal: Principal, pipeline, ledger, book, accounts, mode_ctl, events, clock) -> None:
        self.principal = principal
        self.pipeline, self.ledger, self.book, self.accounts, self.mode_ctl, self.events, self.clock = pipeline, ledger, book, accounts, mode_ctl, events, clock
        self.runs: list[dict] = []

    def _authorized(self, requester: Principal) -> None:
        if not (requester.can(Capability.REQUEST_PROTECTIVE_ACTION) or requester.can(Capability.MANUAL_QUICK_EXIT)):
            require(requester, Capability.REQUEST_PROTECTIVE_ACTION, "protective workflow")

    def _mode_for(self, account_id: str) -> Mode:
        acct = self.accounts.get(account_id)
        if acct.kind == "PAPER":
            return Mode.PAPER
        if self.mode_ctl.mode == Mode.PAPER:
            raise PermissionDenied("LIVE exposure while in PAPER MODE: switch to MANUAL to run live protective exits")
        return self.mode_ctl.mode

    async def exit_position(self, account_id: str, instrument_key: str, reason: str, requester: Principal, correlation_id: str | None = None) -> dict:
        self._authorized(requester)
        acct = self.accounts.get(account_id)
        mode = self._mode_for(account_id)
        pos = self.ledger.position(account_id, instrument_key)
        if pos is None or pos.net_qty == 0:
            return {"instrument_key": instrument_key, "status": "FLAT"}
        if any(u["instrument_key"] == instrument_key for u in self.book.unknown(account_id)):
            return {"instrument_key": instrument_key, "status": "WAITING_RECONCILIATION", "detail": "an order on this instrument is in ORDER STATE UNKNOWN"}
        remaining = abs(pos.net_qty) - self.book.inflight_reducing_qty(account_id, instrument_key)
        lots = remaining // max(1, pos.lot_size)
        if lots <= 0:
            return {"instrument_key": instrument_key, "status": "EXIT_IN_FLIGHT"}
        side = Side.BUY if pos.net_qty < 0 else Side.SELL
        auth = AuthorizationContext(kind=AuthorizationKind.PROTECTIVE_PREAUTH, approved_by=f"protective:{requester.id}", approved_at=self.clock.ts())
        cid = correlation_id or f"PROTECT-{account_id}"
        intent = self.pipeline.make_intent(account_id=account_id, broker=acct.broker, mode=mode, instrument_key=instrument_key, side=side, lots=lots,
                                           order_type=OrderType.MARKET, limit_price=None, purpose=OrderPurpose.PROTECTIVE, reduce_only=True,
                                           origin=Origin.PROTECTIVE_WORKFLOW, authorization=auth, correlation_id=cid, note=reason)
        res = await self.pipeline.submit(intent)
        return {"instrument_key": instrument_key, "status": res["order"].get("state"), "intent_id": intent.intent_id, "failed_rules": res["decision"]["failed_rules"]}

    async def flatten(self, account_id: str, reason: str, requester: Principal) -> dict:
        self._authorized(requester)
        cid = f"FLATTEN-{account_id}-{int(self.clock.ts())}"
        self.events.append("PROTECTIVE_FLATTEN_STARTED", {"account_id": account_id, "reason": reason, "requested_by": requester.id}, self.principal, correlation_id=cid)
        results = []
        # buy back shorts first (they carry the unbounded risk), then sell longs
        for p in sorted(self.ledger.open_positions(account_id), key=lambda p: p.net_qty):
            try:
                results.append(await self.exit_position(account_id, p.instrument_key, reason, requester, correlation_id=cid))
            except PermissionDenied as exc:
                results.append({"instrument_key": p.instrument_key, "status": "REFUSED", "detail": str(exc)})
        residual = [{"instrument_key": p.instrument_key, "net_qty": p.net_qty} for p in self.ledger.open_positions(account_id)]
        summary = {"account_id": account_id, "reason": reason, "results": results, "residual_positions": residual,
                   "complete": not residual, "correlation_id": cid, "ts": self.clock.ts()}
        self.runs.append(summary)
        self.events.append("PROTECTIVE_FLATTEN_RESULT", summary, self.principal, correlation_id=cid)
        return summary
