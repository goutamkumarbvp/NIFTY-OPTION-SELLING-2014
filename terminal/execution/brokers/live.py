"""Live broker adapters (Kotak Neo / Zerodha Kite).

They are wired only when TRADING_ENV=LIVE and both LIVE_TRADING and
LIVE_ORDERS_ENABLED are true. Missing credentials or SDKs fail closed with a
descriptive error instead of falling back to a simulated fill.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict

from terminal.core.models import Order, OrderStatus, OrderType, Side
from terminal.execution.brokers.base import Broker, QuoteLookup


class KotakNeoBroker(Broker):
    name = "kotak_neo"
    live = True

    def __init__(self, settings, session, universe) -> None:
        super().__init__()
        self.s = settings
        self.session = session
        self.universe = universe

    async def connect(self) -> None:
        await self.session.connect()
        self.connected = True

    async def place(self, order: Order, quote_lookup: QuoteLookup) -> Order:
        if not self.session.authenticated:
            raise RuntimeError("BROKER_NOT_CONNECTED")
        u = self.universe.get(order.underlying)
        scrip = await self.session.resolve_option(u, order.expiry, order.strike, order.option_type)
        if not scrip or not scrip.get("trading_symbol"):
            order.status = OrderStatus.REJECTED
            order.message = "KOTAK_TRADING_SYMBOL_NOT_FOUND"
            return order
        seg = {"NSE": "nse_fo", "BSE": "bse_fo", "MCX": "mcx_fo"}[order.exchange.value]
        payload = dict(exchange_segment=seg, product="NRML", price=str(order.limit_price or 0), order_type="L" if order.order_type == OrderType.LIMIT else "MKT",
                       quantity=str(order.quantity), validity="DAY", trading_symbol=scrip["trading_symbol"], transaction_type="B" if order.side == Side.BUY else "S", tag="AITERM")
        resp = await self.session._call("place_order", **payload)
        data = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else (resp if isinstance(resp, dict) else {})
        order.broker_order_id = str(data.get("nOrdNo") or data.get("orderId") or data.get("order_id") or "")
        order.status = OrderStatus.OPEN if order.broker_order_id else OrderStatus.REJECTED
        order.message = str(resp)[:300]
        order.updated_at = time.time()
        return order

    async def cancel(self, order: Order) -> Order:
        if order.broker_order_id:
            await self.session._call("cancel_order", order_id=order.broker_order_id)
            order.status = OrderStatus.CANCELLED
            order.updated_at = time.time()
        return order

    async def margins(self) -> Dict[str, float]:
        try:
            lim = await self.session.limits()
            data = lim.get("data", lim) if isinstance(lim, dict) else {}
            return {"available": float(data.get("Net") or data.get("net") or 0), "raw": lim}
        except Exception as exc:
            self.last_error = str(exc)
            return {}

    def status(self) -> dict:
        base = super().status()
        base["session"] = self.session.status()
        return base


class ZerodhaBroker(Broker):
    name = "zerodha"
    live = True

    def __init__(self, settings) -> None:
        super().__init__()
        self.s = settings
        self._kite = None

    async def connect(self) -> None:
        if not (self.s.zerodha_api_key and self.s.zerodha_access_token):
            raise RuntimeError("ZERODHA_CREDENTIALS_MISSING")
        try:
            from kiteconnect import KiteConnect  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("KITE_SDK_NOT_INSTALLED: pip install kiteconnect") from exc
        self._kite = KiteConnect(api_key=self.s.zerodha_api_key)
        self._kite.set_access_token(self.s.zerodha_access_token)
        self.connected = True

    async def place(self, order: Order, quote_lookup: QuoteLookup) -> Order:
        if not self.connected or self._kite is None:
            raise RuntimeError("BROKER_NOT_CONNECTED")
        loop = asyncio.get_running_loop()
        k = self._kite
        exch = {"NSE": "NFO", "BSE": "BFO", "MCX": "MCX"}[order.exchange.value]
        oid = await loop.run_in_executor(None, lambda: k.place_order(variety=k.VARIETY_REGULAR, exchange=exch, tradingsymbol=order.symbol,
                                                                     transaction_type=k.TRANSACTION_TYPE_BUY if order.side == Side.BUY else k.TRANSACTION_TYPE_SELL,
                                                                     quantity=order.quantity, product=k.PRODUCT_NRML,
                                                                     order_type=k.ORDER_TYPE_LIMIT if order.order_type == OrderType.LIMIT else k.ORDER_TYPE_MARKET,
                                                                     price=order.limit_price, tag="AITERM"))
        order.broker_order_id = str(oid)
        order.status = OrderStatus.OPEN
        order.updated_at = time.time()
        return order

    async def cancel(self, order: Order) -> Order:
        if self._kite is not None and order.broker_order_id:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._kite.cancel_order(variety=self._kite.VARIETY_REGULAR, order_id=order.broker_order_id))
            order.status = OrderStatus.CANCELLED
        return order
