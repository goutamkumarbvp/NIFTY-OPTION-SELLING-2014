"""Broker adapter contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

from terminal.core.models import Order, OptionQuote

QuoteLookup = Callable[[str], Optional[OptionQuote]]


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
        return {}

    async def broker_positions(self) -> List[dict]:
        return []

    def status(self) -> dict:
        return {"name": self.name, "connected": self.connected, "live": self.live, "last_error": self.last_error}
