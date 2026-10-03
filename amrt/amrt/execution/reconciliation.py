"""Reconciliation: the broker is the source of truth for live orders, positions and funds.

* Working and UNKNOWN orders are matched to the venue order book by broker order
  id or client tag; fills are applied as increments (idempotent).
* An UNKNOWN order missing from ≥ 2 successful complete order-book reads spanning
  the grace window becomes NOT_FOUND_AT_BROKER, with the evidence recorded.
* Positions that differ from the ledger are recorded as a divergence: the ledger
  adopts the broker figures (so risk sees real exposure) and new risk is frozen
  until the owner reviews (FREEZE code POSITION_DIVERGENCE).
* Funds/margin snapshots are stored; the first successful read of each IST day
  becomes the start-of-day funds used as the margin-risk denominator.
"""
from __future__ import annotations

import logging

from amrt.core.enums import OrderState
from amrt.execution.intents import OrderIntent
from amrt.security.identity import Capability, Principal, require

log = logging.getLogger("amrt.reconcile")


class Reconciler:
    def __init__(self, principal: Principal, gateway, order_book, ledger, accounts, instruments, safety, events, clock, settings, db=None) -> None:
        require(principal, Capability.RECONCILE, "construct reconciler")
        self.principal = principal
        self.gateway = gateway
        self.book = order_book
        self.ledger = ledger
        self.accounts = accounts
        self.instruments = instruments
        self.safety = safety
        self.events = events
        self.clock = clock
        self.settings = settings
        self.db = db
        self.status: dict[str, dict] = {}
        self._missing: dict[str, list[float]] = {}

    def account_status(self, account_id: str) -> dict:
        return self.status.get(account_id, {"last_ok_ts": None, "divergence": False, "last_error": None, "runs": 0})

    def _symbol_to_key(self, broker: str, report) -> str | None:
        if report.instrument_key:
            return report.instrument_key
        for inst in self.instruments.instruments.values():
            ref = inst.broker_refs.get(broker, {})
            if report.trading_symbol and (ref.get("trading_symbol") == report.trading_symbol or inst.trading_symbol == report.trading_symbol):
                return inst.key
        return None

    async def reconcile_account(self, account_id: str) -> dict:
        acct = self.accounts.get(account_id)
        venue = self.gateway.venues.get(account_id)
        st = dict(self.account_status(account_id))
        st["runs"] = st.get("runs", 0) + 1
        if venue is None:
            st["last_error"] = "no venue"
            self.status[account_id] = st
            return st
        now = self.clock.ts()
        try:
            book = await venue.order_book()
            positions = await venue.positions()
            funds = await venue.funds()
        except Exception as exc:  # noqa: BLE001
            st["last_error"] = f"{type(exc).__name__}: {exc}"
            self.status[account_id] = st
            self.events.append("RECONCILIATION_FAILED", {"account_id": account_id, "error": st["last_error"]}, self.principal)
            return st
        by_id = {r.broker_order_id: r for r in book}
        by_tag = {r.client_tag: r for r in book if r.client_tag}
        resolved = 0
        for rec in self.book.working(account_id):
            rep = by_id.get(rec.get("broker_order_id") or "") or by_tag.get(rec["client_tag"])
            intent = OrderIntent.model_validate(rec["intent"])
            if rep is None:
                if rec["state"] == OrderState.UNKNOWN.value:
                    seen = self._missing.setdefault(rec["intent_id"], [])
                    seen.append(now)
                    grace = self.settings.unknown_not_found_grace_seconds
                    if len(seen) >= 2 and now - (rec["unknown_since"] or now) >= grace and now - seen[0] >= min(grace, 1.0):
                        self.book.transition(rec["intent_id"], OrderState.NOT_FOUND_AT_BROKER,
                                             note=f"absent from {len(seen)} complete order-book reads over {now - seen[0]:.0f}s")
                        self.events.append("ORDER_RESOLVED_NOT_FOUND", {"intent_id": rec["intent_id"], "reads": len(seen)}, self.principal,
                                           correlation_id=intent.decision_id or intent.correlation_id, causation_id=rec["intent_id"])
                        self._missing.pop(rec["intent_id"], None)
                        resolved += 1
                continue
            self._missing.pop(rec["intent_id"], None)
            was_unknown = rec["state"] == OrderState.UNKNOWN.value
            if rep.filled_qty > rec["filled_qty"]:
                self.gateway.apply_fill(rec["intent_id"], rep.filled_qty, rep.avg_price, rep.status, source="reconciliation")
                rec = self.book.get(rec["intent_id"])
            target = {"FILLED": OrderState.FILLED, "REJECTED": OrderState.REJECTED, "CANCELLED": OrderState.CANCELLED,
                      "OPEN": OrderState.ACKNOWLEDGED, "PARTIAL": OrderState.PARTIALLY_FILLED}.get(rep.status)
            cur = OrderState(rec["state"])
            if target is not None and target != cur and not self.book.is_final(rec):
                try:
                    self.book.transition(rec["intent_id"], target, note=f"reconciled: broker status {rep.status} {rep.message}".strip(), broker_order_id=rep.broker_order_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("reconcile transition %s → %s refused: %s", cur, target, exc)
            if was_unknown:
                resolved += 1
                self.events.append("ORDER_UNKNOWN_RESOLVED", {"intent_id": rec["intent_id"], "broker_status": rep.status, "filled": rep.filled_qty}, self.principal,
                                   correlation_id=intent.decision_id or intent.correlation_id, causation_id=rec["intent_id"])
        # positions
        diffs = []
        broker_net: dict[str, tuple[int, float]] = {}
        for p in positions:
            key = self._symbol_to_key(acct.broker, p)
            if key is None:
                diffs.append({"trading_symbol": p.trading_symbol, "issue": "UNMAPPED_INSTRUMENT", "broker_qty": p.net_qty})
                continue
            broker_net[key] = (p.net_qty, p.avg_price)
        ledger_net = {p.instrument_key: p for p in self.ledger.open_positions(account_id)}
        for key in set(broker_net) | set(ledger_net):
            bq = broker_net.get(key, (0, 0.0))[0]
            lq = ledger_net[key].net_qty if key in ledger_net else 0
            if bq != lq:
                diffs.append({"instrument_key": key, "broker_qty": bq, "ledger_qty": lq})
                lot = ledger_net[key].lot_size if key in ledger_net else (self.instruments.instruments[key].lot_size if key in self.instruments.instruments else 1)
                self.ledger.adopt(account_id, key, bq, broker_net.get(key, (0, 0.0))[1], lot, simulated=acct.kind == "PAPER")
        divergence = bool(diffs)
        if divergence:
            self.events.append("POSITION_DIVERGENCE", {"account_id": account_id, "diffs": diffs}, self.principal)
            self.safety.freeze(self.principal, "POSITION_DIVERGENCE", f"{len(diffs)} position difference(s) on {account_id}; ledger adopted broker figures")
        # funds
        day = self.clock.ist().date().isoformat()
        f = dict(acct.funds)
        f.update({"available": funds.available, "margin_used": funds.margin_used, "total": funds.total, "ts": funds.ts})
        if f.get("sod_date") != day and funds.total:
            f["sod_date"], f["sod_funds"] = day, funds.total
        acct.funds = f
        st.update({"last_ok_ts": now, "divergence": divergence or (st.get("divergence") and self.safety.state["freeze"]["active"]
                                                                    and any(r["code"] == "POSITION_DIVERGENCE" for r in self.safety.state["freeze"]["reasons"])),
                   "last_error": None, "unknown_resolved": st.get("unknown_resolved", 0) + resolved, "orders_seen": len(book), "positions_seen": len(positions),
                   "last_diffs": diffs})
        self.status[account_id] = st
        return st

    async def reconcile_all(self) -> dict[str, dict]:
        out = {}
        for a in self.accounts.accounts.values():
            if a.account_id in self.gateway.venues:
                out[a.account_id] = await self.reconcile_account(a.account_id)
        return out
