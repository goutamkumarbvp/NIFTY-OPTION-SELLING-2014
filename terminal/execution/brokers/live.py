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
        if not scrip:
            order.status = OrderStatus.REJECTED
            order.message = "ZERODHA_TRADING_SYMBOL_NOT_FOUND"
            return order
        k = self.session.kite
        oid = await self.session._call("place_order", variety=k.VARIETY_REGULAR, exchange=scrip["exchange"], tradingsymbol=scrip["trading_symbol"],
                                       transaction_type=k.TRANSACTION_TYPE_BUY if order.side == Side.BUY else k.TRANSACTION_TYPE_SELL, quantity=order.quantity,
                                       product=k.PRODUCT_NRML, order_type=k.ORDER_TYPE_LIMIT if order.order_type == OrderType.LIMIT else k.ORDER_TYPE_MARKET,
                                       price=order.limit_price if order.order_type == OrderType.LIMIT else None, validity=k.VALIDITY_DAY, tag="AITERM")
        order.broker_order_id = str(oid or "")
        order.status = OrderStatus.OPEN if order.broker_order_id else OrderStatus.REJECTED
        order.message = f"KITE_ORDER:{order.broker_order_id}"
        order.updated_at = time.time()
        return order

    async def cancel(self, order: Order) -> Order:
        if order.broker_order_id:
            await self.session._call("cancel_order", variety=self.session.kite.VARIETY_REGULAR, order_id=order.broker_order_id)
            order.status = OrderStatus.CANCELLED
            order.updated_at = time.time()
        return order

    async def margins(self) -> Dict[str, float]:
        try:
            m = await self.session.margins()
            eq = (m or {}).get("equity", {}) or {}
            com = (m or {}).get("commodity", {}) or {}
            return {"available": float(eq.get("net") or 0) + float(com.get("net") or 0), "raw": m}
        except Exception as exc:
            self.last_error = str(exc)
            return {}

    def status(self) -> dict:
        base = super().status()
        base["session"] = self.session.status()
        return base


class AngelOneBroker(Broker):
    name = "angel"
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
        if not scrip:
            order.status = OrderStatus.REJECTED
            order.message = "ANGEL_TRADING_SYMBOL_NOT_FOUND"
            return order
        params = {"variety": "NORMAL", "tradingsymbol": scrip["trading_symbol"], "symboltoken": str(scrip["token"]), "transactiontype": "BUY" if order.side == Side.BUY else "SELL",
                  "exchange": scrip["exchange"], "ordertype": "LIMIT" if order.order_type == OrderType.LIMIT else "MARKET", "producttype": "CARRYFORWARD", "duration": "DAY",
                  "price": str(order.limit_price or 0), "squareoff": "0", "stoploss": "0", "quantity": str(order.quantity), "ordertag": "AITERM"}
        oid = await self.session._call("placeOrder", params)
        order.broker_order_id = str(oid or "")
        order.status = OrderStatus.OPEN if order.broker_order_id else OrderStatus.REJECTED
        order.message = f"ANGEL_ORDER:{order.broker_order_id}"
        order.updated_at = time.time()
        return order

    async def cancel(self, order: Order) -> Order:
        if order.broker_order_id:
            await self.session._call("cancelOrder", order.broker_order_id, "NORMAL")
            order.status = OrderStatus.CANCELLED
            order.updated_at = time.time()
        return order

    async def margins(self) -> Dict[str, float]:
        try:
            m = await self.session.margins()
            data = (m or {}).get("data") or {}
            return {"available": float(data.get("net") or data.get("availablecash") or 0), "raw": m}
        except Exception as exc:
            self.last_error = str(exc)
            return {}

    def status(self) -> dict:
        base = super().status()
        base["session"] = self.session.status()
        return base
