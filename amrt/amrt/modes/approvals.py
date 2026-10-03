"""Approval gate (Manual Mode, and Paper Mode): the owner accepts, modifies or rejects proposals.

Accepting turns each proposed leg into an OrderIntent authorised by the owner
(OWNER_APPROVAL with the step-up time). Legs are submitted hedges first, shorts
last; if a leg fails, later legs are not sent and the owner is alerted with the
partial structure — no automatic compensating order is invented.
In PAPER mode with paper auto-approve on, approvals for PAPER accounts are
granted automatically and recorded as "paper-auto:<owner>".
"""
from __future__ import annotations

from sqlalchemy import select

from amrt.core.enums import AuthorizationKind, Mode, OrderPurpose, OrderType, Origin, Side
from amrt.core.errors import PermissionDenied
from amrt.core.ids import new_id
from amrt.execution.intents import AuthorizationContext
from amrt.security.identity import Capability, Principal, require
from amrt.storage.db import approvals

APPROVAL_TTL = 15 * 60


class ApprovalGate:
    def __init__(self, db, events, clock, pipeline, accounts, mode_ctl, alerts, instruments) -> None:
        self.db, self.events, self.clock, self.pipeline, self.accounts, self.mode_ctl, self.alerts, self.instruments = \
            db, events, clock, pipeline, accounts, mode_ctl, alerts, instruments

    def create(self, package: dict, account_id: str) -> dict:
        aid = new_id("AP")
        now = self.clock.ts()
        body = {"package": package, "account_id": account_id, "proposed_action": package.get("proposed_action"), "mode": self.mode_ctl.mode.value}
        with self.db.tx() as conn:
            conn.execute(approvals.insert().values(approval_id=aid, decision_id=package["decision_id"], status="PENDING", created_at=now,
                                                   expires_at=min(now + APPROVAL_TTL, package.get("expires_at") or now + APPROVAL_TTL), body=body))
        self.events.append("APPROVAL_REQUESTED", {"approval_id": aid, "decision_id": package["decision_id"], "account_id": account_id},
                           self.pipeline.principal, correlation_id=package["decision_id"])
        return {"approval_id": aid, "status": "PENDING"}

    def get(self, aid: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(approvals).where(approvals.c.approval_id == aid)).first()
        return dict(row._mapping) if row else None

    def pending(self) -> list[dict]:
        now = self.clock.ts()
        with self.db.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(approvals).where(approvals.c.status == "PENDING").order_by(approvals.c.created_at.desc()))]
        live = []
        for r in rows:
            if r["expires_at"] < now:
                self._set(r["approval_id"], "EXPIRED", None)
            else:
                live.append(r)
        return live

    def history(self, limit: int = 100) -> list[dict]:
        with self.db.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(approvals).order_by(approvals.c.created_at.desc()).limit(limit))]

    def _set(self, aid: str, status: str, by: str | None) -> None:
        with self.db.tx() as conn:
            conn.execute(approvals.update().where(approvals.c.approval_id == aid).values(status=status, decided_at=self.clock.ts(), decided_by=by))

    def reject(self, aid: str, principal: Principal, reason: str) -> dict:
        require(principal, Capability.APPROVE_ACTION, "reject proposal")
        rec = self.get(aid)
        if rec is None or rec["status"] != "PENDING":
            raise PermissionDenied("approval is not pending")
        self._set(aid, "REJECTED", principal.id)
        self.events.append("APPROVAL_REJECTED", {"approval_id": aid, "reason": reason}, principal, correlation_id=rec["decision_id"])
        return {"approval_id": aid, "status": "REJECTED"}

    async def accept(self, aid: str, principal: Principal, step_up_at: float | None, modifications: dict | None = None, auto: bool = False) -> dict:
        require(principal, Capability.APPROVE_ACTION, "approve proposal")
        rec = self.get(aid)
        if rec is None or rec["status"] != "PENDING":
            raise PermissionDenied("approval is not pending")
        if rec["expires_at"] < self.clock.ts():
            self._set(aid, "EXPIRED", None)
            raise PermissionDenied("approval expired")
        body = rec["body"]
        acct = self.accounts.get(body["account_id"])
        mode = self.mode_ctl.mode
        if (acct.kind == "PAPER") != (mode == Mode.PAPER):
            raise PermissionDenied(f"account {acct.account_id} ({acct.kind}) cannot be traded in {mode.value} mode")
        action = body.get("proposed_action")
        if not action or not action.get("legs"):
            raise PermissionDenied("nothing to execute")
        mods = modifications or {}
        legs = sorted(action["legs"], key=lambda leg_: 0 if leg_["side"] == "BUY" else 1)
        approved_by = f"paper-auto:{principal.id}" if auto else principal.id
        self._set(aid, "APPROVED", approved_by)
        self.events.append("APPROVAL_GRANTED", {"approval_id": aid, "modifications": mods, "auto": auto}, principal, correlation_id=rec["decision_id"])
        auth = AuthorizationContext(kind=AuthorizationKind.OWNER_APPROVAL, approved_by=approved_by, approval_id=aid, step_up_at=step_up_at, approved_at=self.clock.ts())
        results = []
        for i, leg in enumerate(legs):
            lots = int(mods.get("lots", leg["lots"]))
            if lots <= 0 or lots > leg["lots"] * 4:
                raise PermissionDenied("modified lots out of bounds")
            otype = OrderType(leg.get("order_type", "LIMIT"))
            price = mods.get("limit_prices", {}).get(leg["instrument_key"], leg.get("limit_price")) if otype == OrderType.LIMIT else None
            intent = self.pipeline.make_intent(account_id=acct.account_id, broker=acct.broker, mode=mode, instrument_key=leg["instrument_key"], side=Side(leg["side"]),
                                               lots=lots, order_type=otype, limit_price=price, purpose=OrderPurpose(leg.get("purpose", "ENTRY")),
                                               reduce_only=bool(leg.get("reduce_only", False)), origin=Origin.OWNER_MANUAL, authorization=auth,
                                               correlation_id=rec["decision_id"], decision_id=rec["decision_id"], strategy_id=action.get("strategy_id"),
                                               strategy_version=action.get("strategy_version"), idem_parts=(aid, i))
            res = await self.pipeline.submit(intent)
            results.append(res)
            if res["order"].get("state") in ("REJECTED_PRE_TRADE", "REJECTED", "ORDER STATE UNKNOWN"):
                if i < len(legs) - 1:
                    self.alerts.emit("CRITICAL", "execution", "Structure partially executed", f"leg {i + 1}/{len(legs)} {res['order'].get('state')}; remaining legs not sent. Review positions.")
                break
        return {"approval_id": aid, "status": "APPROVED", "results": results}

    async def manual_ticket(self, principal: Principal, step_up_at: float | None, *, account_id: str, instrument_key: str, side: str, lots: int,
                            order_type: str, limit_price: float | None, reduce_only: bool, note: str = "") -> dict:
        require(principal, Capability.CREATE_MANUAL_INTENT, "manual order ticket")
        acct = self.accounts.get(account_id)
        mode = self.mode_ctl.mode
        if (acct.kind == "PAPER") != (mode == Mode.PAPER):
            raise PermissionDenied(f"account {account_id} ({acct.kind}) cannot be traded in {mode.value} mode")
        auth = AuthorizationContext(kind=AuthorizationKind.OWNER_APPROVAL, approved_by=principal.id, approval_id=new_id("MT"), step_up_at=step_up_at, approved_at=self.clock.ts())
        cid = new_id("MAN")
        intent = self.pipeline.make_intent(account_id=account_id, broker=acct.broker, mode=mode, instrument_key=instrument_key, side=Side(side), lots=lots,
                                           order_type=OrderType(order_type), limit_price=limit_price if OrderType(order_type) == OrderType.LIMIT else None,
                                           purpose=OrderPurpose.EXIT if reduce_only else OrderPurpose.ENTRY, reduce_only=reduce_only, origin=Origin.OWNER_MANUAL,
                                           authorization=auth, correlation_id=cid, note=note)
        return await self.pipeline.submit(intent)
