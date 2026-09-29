"""Broker adapter contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Dict, List

from terminal.core.models import OptionQuote, Order

QuoteLookup = Callable[[str], OptionQuote | None]


class Broker(ABC):
    name = "abstract"
    live = False

    def __init__(self) -> None:
        self.connected = False
        self.last_error = ""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def place(self, order: Order, quote_lookup: QuoteLookup) -> Order: ...

    @abstractmethod
    async def cancel(self, order: Order) -> Order: ...

    async def margins(self) -> Dict[str, float]:
        """{"available": float, "used": float} when the broker reports them."""
        return {}

    async def broker_positions(self) -> List[dict]:
        """[{"trading_symbol", "token", "exchange", "net_qty", "avg_price"}] from the broker book."""
        return []

    async def fetch_order(self, order: Order) -> dict | None:
        """Current broker state of an order: {"status": OPEN|FILLED|CANCELLED|REJECTED, "filled_qty", "avg_price", "message"}.
        None when the broker cannot report it (yet)."""
        return None

    def status(self) -> dict:
        return {"name": self.name, "connected": self.connected, "live": self.live, "last_error": self.last_error}
