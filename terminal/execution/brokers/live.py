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

    def __init__(self, settings) -> None:
        super().__init__()
        self.s = settings
        self._client = None

    async def connect(self) -> None:
        s = self.s
        if not all([s.neo_consumer_key, s.neo_mobile_number, s.neo_ucc, s.neo_mpin, s.neo_totp_secret]):
            raise RuntimeError("KOTAK_CREDENTIALS_MISSING")
        try:
            from neo_api_client import NeoAPI  # type: ignore
            import pyotp  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("KOTAK_SDK_NOT_INSTALLED") from exc
        loop = asyncio.get_running_loop()

        def _login():
            c = NeoAPI(consumer_key=s.neo_consumer_key, environment="prod", access_token=None, neo_fin_key=None)
            c.login(mobilenumber=s.neo_mobile_number, ucc=s.neo_ucc, totp=pyotp.TOTP(s.neo_totp_secret).now())
            c.session_2fa(OTP=s.neo_mpin)
            return c

        self._client = await loop.run_in_executor(None, _login)
        self.connected = True

    async def place(self, order: Order, quote_lookup: QuoteLookup) -> Order:
        if not self.connected or self._client is None:
            raise RuntimeError("BROKER_NOT_CONNECTED")
        seg = {"NSE": "nse_fo", "BSE": "bse_fo", "MCX": "mcx_fo"}[order.exchange.value]
        loop = asyncio.get_running_loop()
        payload = dict(exchange_segment=seg, product="NRML", price=str(order.limit_price or 0), order_type="L" if order.order_type == OrderType.LIMIT else "MKT",
                       quantity=str(order.quantity), validity="DAY", trading_symbol=order.symbol, transaction_type="B" if order.side == Side.BUY else "S", tag="AITERM")
        resp = await loop.run_in_executor(None, lambda: self._client.place_order(**payload))
        order.broker_order_id = str((resp or {}).get("nOrdNo") or (resp or {}).get("orderId") or "")
        order.status = OrderStatus.OPEN if order.broker_order_id else OrderStatus.REJECTED
        order.message = str(resp)[:300]
        order.updated_at = time.time()
        return order

    async def cancel(self, order: Order) -> Order:
        if self._client is not None and order.broker_order_id:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._client.cancel_order(order_id=order.broker_order_id))
            order.status = OrderStatus.CANCELLED
        return order

    async def margins(self) -> Dict[str, float]:
        if self._client is None:
            return {}
        loop = asyncio.get_running_loop()
        try:
            lim = await loop.run_in_executor(None, lambda: self._client.limits(segment="ALL", exchange="ALL", product="ALL"))
            return {"available": float(lim.get("Net", 0) or 0), "raw": lim}
        except Exception as exc:
            self.last_error = str(exc)
            return {}


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
