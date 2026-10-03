"""Accounts: live broker accounts and separate paper accounts. Nothing is ever transferred between them."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Account:
    account_id: str
    broker: str            # "paper" for paper accounts
    kind: str              # LIVE | PAPER
    display: str
    enabled: bool = True
    funds: dict = field(default_factory=dict)   # {available, margin_used, total, ts, sod_funds, sod_date}


class AccountRegistry:
    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}

    def add(self, acct: Account) -> Account:
        if acct.kind not in ("LIVE", "PAPER"):
            raise ValueError("account kind must be LIVE or PAPER")
        if acct.kind == "PAPER" and acct.broker != "paper":
            raise ValueError("paper accounts must use the paper broker")
        if acct.kind == "LIVE" and acct.broker == "paper":
            raise ValueError("live accounts cannot use the paper broker")
        self.accounts[acct.account_id] = acct
        return acct

    def get(self, account_id: str) -> Account:
        try:
            return self.accounts[account_id]
        except KeyError:
            raise KeyError(f"UNKNOWN_ACCOUNT:{account_id}") from None

    def live(self) -> list[Account]:
        return [a for a in self.accounts.values() if a.kind == "LIVE"]

    def paper(self) -> list[Account]:
        return [a for a in self.accounts.values() if a.kind == "PAPER"]

    def describe(self) -> list[dict]:
        return [{"account_id": a.account_id, "broker": a.broker, "kind": a.kind, "display": a.display, "enabled": a.enabled, "funds": a.funds}
                for a in self.accounts.values()]
