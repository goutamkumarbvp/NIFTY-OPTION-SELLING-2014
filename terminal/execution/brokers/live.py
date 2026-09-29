"""Live broker adapters (Kotak Neo / Zerodha Kite).

They are wired only when TRADING_ENV=LIVE and both LIVE_TRADING and
LIVE_ORDERS_ENABLED are true. Missing credentials or SDKs fail closed with a
descriptive error instead of falling back to a simulated fill.
"""
from __future__ import annotations

import time
from typing import Dict, List

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
            used = next((float(v) for k, v in data.items() if any(x in k.lower() for x in ("marginused", "used", "utilis")) and str(v).replace(".", "", 1).replace("-", "", 1).isdigit()), None)
            return {"available": float(data.get("Net") or data.get("net") or 0), "used": used, "raw": lim}
        except Exception as exc:
            self.last_error = str(exc)
            return {}

    async def fetch_order(self, order: Order) -> dict | None:
        if not order.broker_order_id:
            return None
        rep = await self.session._call("order_report", order_id=order.broker_order_id)
        rows = rep.get("data") if isinstance(rep, dict) else rep
        rows = rows if isinstance(rows, list) else ([rows] if isinstance(rows, dict) else [])
        for r in rows:
            if str(r.get("nOrdNo") or r.get("orderId") or "") == order.broker_order_id:
                st = str(r.get("ordSt") or r.get("status") or "").lower()
                status = "FILLED" if st in ("complete", "completed", "traded", "filled") else ("CANCELLED" if "cancel" in st else ("REJECTED" if "reject" in st else "OPEN"))
                return {"status": status, "filled_qty": int(float(r.get("fldQty") or r.get("filledQty") or 0)), "avg_price": float(r.get("avgPrc") or r.get("avgPrice") or 0), "message": str(r.get("rejRsn") or r.get("message") or st)}
        return None

    async def broker_positions(self) -> List[dict]:
        rep = await self.session.positions()
        rows = rep.get("data") if isinstance(rep, dict) else rep
        out = []
        for r in rows if isinstance(rows, list) else []:
            buy = float(r.get("flBuyQty") or r.get("buyQty") or 0) + float(r.get("cfBuyQty") or 0)
            sell = float(r.get("flSellQty") or r.get("sellQty") or 0) + float(r.get("cfSellQty") or 0)
            net = int(buy - sell)
            avg = float(r.get("buyAmt") or 0) / buy if net > 0 and buy else (float(r.get("sellAmt") or 0) / sell if net < 0 and sell else 0.0)
            out.append({"trading_symbol": str(r.get("trdSym") or r.get("tradingSymbol") or ""), "token": str(r.get("tok") or ""), "exchange": str(r.get("exSeg") or ""), "net_qty": net, "avg_price": round(avg, 2)})
        return out

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
            used = float((eq.get("utilised") or {}).get("debits") or 0) + float((com.get("utilised") or {}).get("debits") or 0)
            return {"available": float(eq.get("net") or 0) + float(com.get("net") or 0), "used": used, "raw": m}
        except Exception as exc:
            self.last_error = str(exc)
            return {}

    async def fetch_order(self, order: Order) -> dict | None:
        if not order.broker_order_id:
            return None
        hist = await self.session._call("order_history", order.broker_order_id)
        if not hist:
            return None
        last = hist[-1] if isinstance(hist, list) else hist
        st = str(last.get("status") or "").upper()
        status = "FILLED" if st == "COMPLETE" else ("CANCELLED" if st == "CANCELLED" else ("REJECTED" if st == "REJECTED" else "OPEN"))
        return {"status": status, "filled_qty": int(last.get("filled_quantity") or 0), "avg_price": float(last.get("average_price") or 0), "message": str(last.get("status_message") or st)}

    async def broker_positions(self) -> List[dict]:
        pos = await self.session.positions()
        out = []
        for r in (pos or {}).get("net", []) or []:
            out.append({"trading_symbol": str(r.get("tradingsymbol") or ""), "token": str(r.get("instrument_token") or ""), "exchange": str(r.get("exchange") or ""), "net_qty": int(r.get("quantity") or 0), "avg_price": float(r.get("average_price") or 0)})
        return out

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
            used = data.get("utiliseddebits")
            return {"available": float(data.get("net") or data.get("availablecash") or 0), "used": float(used) if used is not None else None, "raw": m}
        except Exception as exc:
            self.last_error = str(exc)
            return {}

    async def fetch_order(self, order: Order) -> dict | None:
        if not order.broker_order_id:
            return None
        book = await self.session._call("orderBook")
        rows = (book or {}).get("data") or []
        for r in rows if isinstance(rows, list) else []:
            if str(r.get("orderid") or "") == order.broker_order_id:
                st = str(r.get("status") or r.get("orderstatus") or "").lower()
                status = "FILLED" if st in ("complete", "completed") else ("CANCELLED" if "cancel" in st else ("REJECTED" if "reject" in st else "OPEN"))
                return {"status": status, "filled_qty": int(float(r.get("filledshares") or 0)), "avg_price": float(r.get("averageprice") or 0), "message": str(r.get("text") or st)}
        return None

    async def broker_positions(self) -> List[dict]:
        pos = await self.session.positions()
        out = []
        for r in (pos or {}).get("data") or []:
            out.append({"trading_symbol": str(r.get("tradingsymbol") or ""), "token": str(r.get("symboltoken") or ""), "exchange": str(r.get("exchange") or ""), "net_qty": int(float(r.get("netqty") or 0)), "avg_price": float(r.get("avgnetprice") or r.get("netprice") or 0)})
        return out

    def status(self) -> dict:
        base = super().status()
        base["session"] = self.session.status()
        return base
