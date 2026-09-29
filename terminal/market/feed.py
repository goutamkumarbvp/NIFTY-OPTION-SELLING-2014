"""Live market data feeds (Kotak Neo SFeed, Zerodha KiteTicker, Angel One SmartWebSocketV2).

The terminal is live-only: every feed is event-driven and forwards each tick or
quote update the broker pushes, with no sampling. ``MarketFeed`` measures the
observed inter-tick interval per symbol so the dashboard can show the actual
resolution the broker delivers (Kotak SFeed updates are sub-second).
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Dict, List

from terminal.core.models import Tick, Underlying

log = logging.getLogger("terminal.feed")

TickHandler = Callable[[Tick], Awaitable[None]]


class MarketFeed(ABC):
    name = "abstract"

    def __init__(self) -> None:
        self._handlers: List[TickHandler] = []
        self.connected = False
        self.last_tick_ts: float = 0.0
        self.tick_count = 0
        self.errors = 0
        self.option_count = 0
        self._last_by_symbol: Dict[str, float] = {}
        self._interval_ema: Dict[str, float] = {}  # observed seconds between updates, per symbol
        self._min_interval: float = 0.0

    def on_tick(self, handler: TickHandler) -> None:
        self._handlers.append(handler)

    def on_option_quote(self, handler) -> None:
        """handler(symbol, fields) for streamed option quotes (live feeds only)."""
        self._option_handlers = getattr(self, "_option_handlers", [])
        self._option_handlers.append(handler)

    async def _emit_option(self, symbol: str, fields: Dict) -> None:
        now = time.time()
        self.last_tick_ts = now
        self.option_count += 1
        self._observe(symbol, now)
        for h in getattr(self, "_option_handlers", []):
            try:
                await h(symbol, fields)
            except Exception:
                log.exception("option quote handler failed")

    async def subscribe_options(self, entries: List[Dict]) -> int:
        """Subscribe streamed quotes for option contracts: [{symbol, token, exchange/segment}]. No-op by default."""
        return 0

    def streamed_options(self) -> int:
        return len(getattr(self, "_option_tokens", {}))

    def _observe(self, key: str, ts: float) -> None:
        prev = self._last_by_symbol.get(key)
        self._last_by_symbol[key] = ts
        if prev is not None and ts > prev:
            dt = ts - prev
            ema = self._interval_ema.get(key)
            self._interval_ema[key] = dt if ema is None else 0.9 * ema + 0.1 * dt
            self._min_interval = dt if not self._min_interval else min(self._min_interval, dt)

    def tick_resolution(self) -> Dict[str, float]:
        """Observed update interval statistics (seconds)."""
        vals = list(self._interval_ema.values())
        return {"median_interval_s": round(sorted(vals)[len(vals) // 2], 3) if vals else None, "min_interval_s": round(self._min_interval, 4) if self._min_interval else None,
                "symbols": len(vals), "per_symbol_ms": {k: round(v * 1000, 1) for k, v in sorted(self._interval_ema.items())[:20]}}

    async def _emit(self, tick: Tick) -> None:
        self.last_tick_ts = tick.ts
        self.tick_count += 1
        self._observe(tick.symbol, tick.ts)
        for h in self._handlers:
            try:
                await h(tick)
            except Exception:  # pragma: no cover - defensive
                log.exception("tick handler failed")

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    def is_fresh(self, stale_seconds: float) -> bool:
        return self.connected and (time.time() - self.last_tick_ts) <= stale_seconds

    def status(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "last_tick_age": round(time.time() - self.last_tick_ts, 2) if self.last_tick_ts else None,
            "ticks": self.tick_count,
            "option_quotes": self.option_count,
            "errors": self.errors,
            "resolution": self.tick_resolution(),
        }


class KotakNeoFeed(MarketFeed):
    """Kotak Neo live feed (SDK 3.x SFeed WebSocket).

    Indices (NIFTY, BANKNIFTY, SENSEX, INDIAVIX ...) stream from the cash
    segments; MCX underlyings stream as the nearest futures contract. The
    session (login, token discovery) is shared with the broker and the
    option-chain poller. Read-only.
    """

    name = "kotak_neo"

    def __init__(self, universe: List[Underlying], session) -> None:
        super().__init__()
        self.universe = universe
        self.session = session
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._token_map: Dict[str, str] = {}  # instrument token -> our symbol
        self._option_tokens: Dict[str, str] = {}  # option token -> our option symbol
        self._ws = None
        self.market_status: Dict[str, str] = {}
        self.reconnects = 0

    async def subscribe_options(self, entries: List[Dict]) -> int:
        from neo_api_client.websocket.feed import WsToken  # type: ignore
        new = [e for e in entries if str(e["token"]) not in self._option_tokens]
        for e in new:
            self._option_tokens[str(e["token"])] = e["symbol"]
        if new and self._ws is not None and self.connected:
            try:
                await self._ws.subscribe_scrips([WsToken(e.get("segment") or e.get("exchange") or "nse_fo", str(e["token"])) for e in new])
            except Exception as exc:
                self.errors += 1
                log.warning("kotak option subscribe failed: %s", exc)
        return len(new)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        await self.session.connect()
        found = await self.session.resolve_index_tokens(self.universe, include_vix=True)
        if not found:
            raise RuntimeError("KOTAK_NO_INSTRUMENT_TOKENS_RESOLVED")
        self._token_map = {str(v["token"]): sym for sym, v in found.items()}
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="kotak-sfeed")
        log.info("Kotak Neo feed starting for %s", ", ".join(found))

    async def stop(self) -> None:
        self.connected = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._ws = None

    async def reconnect(self) -> None:
        await self.stop()
        self.session.authenticated = False
        self.reconnects += 1
        await self.start()

    async def _run(self) -> None:
        from terminal.market.kotak import tick_from_message
        try:
            from neo_api_client.websocket.feed import WsToken  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            self.errors += 1
            log.error("kotak SDK missing: %s", exc)
            return
        backoff = 2.0
        while not self._stop.is_set():
            try:
                ws = self.session.websocket()
                async with ws:
                    self._ws = ws
                    self.connected = True
                    await ws.subscribe_exchange()
                    await ws.subscribe_scrips([WsToken(v["segment"], str(v["token"])) for v in self.session.tokens.values()])
                    if self._option_tokens:
                        await ws.subscribe_scrips([WsToken("nse_fo", tok) for tok in self._option_tokens])
                    backoff = 2.0
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        kind = type(msg).__name__
                        if kind == "SFeedMarketStatus":
                            self.market_status[str(getattr(msg, "exchange_segment", ""))] = str(getattr(msg, "status", ""))
                            continue
                        fields = tick_from_message(msg)
                        if not fields:
                            continue
                        osym = self._option_tokens.get(fields["token"])
                        if osym is not None:
                            await self._emit_option(osym, {"ltp": fields["ltp"], "oi": fields["oi"], "volume": fields["volume"]})
                            continue
                        sym = self._token_map.get(fields["token"])
                        if sym is None:
                            continue
                        await self._emit(Tick(symbol=sym, ltp=fields["ltp"], change_pct=round(fields["change_pct"], 2), open=fields["open"], high=fields["high"], low=fields["low"],
                                              prev_close=fields["prev_close"], volume=fields["volume"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.connected = False
                log.warning("kotak feed error: %s (retry in %.0fs)", exc, backoff)
                if "auth" in str(exc).lower() or "token" in str(exc).lower():
                    self.session.authenticated = False
                    try:
                        await self.session.connect()
                    except Exception as e2:
                        log.warning("kotak re-login failed: %s", e2)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self.connected = False

    def status(self) -> dict:
        base = super().status()
        base.update({"session": self.session.status(), "market_status": self.market_status, "reconnects": self.reconnects})
        return base


class ZerodhaFeed(MarketFeed):
    """Zerodha Kite live feed (KiteTicker WebSocket, bridged from its thread into asyncio).

    Indices stream from NSE/BSE, MCX underlyings as the nearest future. The
    ticker owns its own reconnect logic; ``reconnect()`` re-subscribes rather
    than restarting the (non-restartable) reactor thread.
    """

    name = "zerodha_kite"

    def __init__(self, universe: List[Underlying], session) -> None:
        super().__init__()
        self.universe = universe
        self.session = session
        self._ticker = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._token_map: Dict[int, str] = {}
        self._option_tokens: Dict[int, str] = {}
        self.reconnects = 0
        self.socket_connected = False

    async def subscribe_options(self, entries: List[Dict]) -> int:
        new = [e for e in entries if int(e["token"]) not in self._option_tokens]
        for e in new:
            self._option_tokens[int(e["token"])] = e["symbol"]
        if new and self._ticker is not None and self.socket_connected:
            try:
                toks = [int(e["token"]) for e in new]
                self._ticker.subscribe(toks)
                self._ticker.set_mode(self._ticker.MODE_FULL, toks)
            except Exception as exc:
                self.errors += 1
                log.warning("zerodha option subscribe failed: %s", exc)
        return len(new)

    async def start(self) -> None:
        if self._ticker is not None:
            return
        await self.session.connect()
        found = await self.session.resolve_index_tokens(self.universe, include_vix=True)
        if not found:
            raise RuntimeError("ZERODHA_NO_INSTRUMENT_TOKENS_RESOLVED")
        self._token_map = {int(v["token"]): sym for sym, v in found.items()}
        self._loop = asyncio.get_running_loop()
        from terminal.market.zerodha import tick_from_kite
        ticker = self.session.ticker()
        tokens = list(self._token_map)

        def on_connect(ws, response):
            self.socket_connected = True
            self.connected = True
            all_tokens = tokens + list(self._option_tokens)
            ws.subscribe(all_tokens)
            ws.set_mode(ws.MODE_FULL, all_tokens)

        def on_ticks(ws, ticks):
            for t in ticks:
                f = tick_from_kite(t)
                if not f:
                    continue
                osym = self._option_tokens.get(f["token"])
                if osym is not None and self._loop and not self._loop.is_closed():
                    depth = t.get("depth") or {}
                    bid = (depth.get("buy") or [{}])[0].get("price", 0) if depth.get("buy") else 0
                    ask = (depth.get("sell") or [{}])[0].get("price", 0) if depth.get("sell") else 0
                    asyncio.run_coroutine_threadsafe(self._emit_option(osym, {"ltp": f["ltp"], "oi": f["oi"], "volume": f["volume"], "bid": bid, "ask": ask}), self._loop)
                    continue
                sym = self._token_map.get(f["token"])
                if sym is None:
                    continue
                tick = Tick(symbol=sym, ltp=f["ltp"], change_pct=round(f["change_pct"], 2), open=f["open"], high=f["high"], low=f["low"], prev_close=f["prev_close"], volume=f["volume"])
                if self._loop and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(self._emit(tick), self._loop)

        def on_close(ws, code, reason):
            self.socket_connected = False
            self.connected = False
            log.warning("zerodha ticker closed: %s %s", code, reason)

        def on_error(ws, code, reason):
            self.errors += 1
            log.warning("zerodha ticker error: %s %s", code, reason)

        def on_reconnect(ws, attempts):
            self.reconnects += 1

        ticker.on_connect = on_connect
        ticker.on_ticks = on_ticks
        ticker.on_close = on_close
        ticker.on_error = on_error
        ticker.on_reconnect = on_reconnect
        ticker.connect(threaded=True)
        self._ticker = ticker
        self.connected = True  # optimistic until the socket reports
        log.info("Zerodha Kite feed starting for %s", ", ".join(found))

    async def stop(self) -> None:
        self.connected = False
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception:
                pass
            try:
                self._ticker.stop()
            except Exception:
                pass
        self._ticker = None

    async def reconnect(self) -> None:
        self.reconnects += 1
        if self._ticker is None:
            await self.start()
            return
        try:
            if self._ticker.is_connected():
                self._ticker.resubscribe()
                self.connected = True
        except Exception as exc:
            self.errors += 1
            log.warning("zerodha resubscribe failed: %s", exc)

    def status(self) -> dict:
        base = super().status()
        base.update({"session": self.session.status(), "socket_connected": self.socket_connected, "reconnects": self.reconnects})
        return base


class AngelOneFeed(MarketFeed):
    """Angel One SmartAPI feed (SmartWebSocketV2 in SNAP_QUOTE mode, run in a thread)."""

    name = "angel_smartapi"

    def __init__(self, universe: List[Underlying], session) -> None:
        super().__init__()
        self.universe = universe
        self.session = session
        self._ws = None
        self._thread = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._token_map: Dict[str, str] = {}
        self._option_tokens: Dict[str, str] = {}
        self.reconnects = 0

    async def subscribe_options(self, entries: List[Dict]) -> int:
        from terminal.market.angel import WS_EXCHANGE_TYPE
        new = [e for e in entries if str(e["token"]) not in self._option_tokens]
        for e in new:
            self._option_tokens[str(e["token"])] = e["symbol"]
        if new and self._ws is not None and self.connected:
            groups: Dict[int, List[str]] = {}
            for e in new:
                groups.setdefault(WS_EXCHANGE_TYPE.get(str(e.get("exchange") or "NFO").upper(), 2), []).append(str(e["token"]))
            try:
                self._ws.subscribe("aiterm-opt", self._ws.SNAP_QUOTE, [{"exchangeType": k, "tokens": v} for k, v in groups.items()])
            except Exception as exc:
                self.errors += 1
                log.warning("angel option subscribe failed: %s", exc)
        return len(new)

    async def start(self) -> None:
        if self._ws is not None:
            return
        await self.session.connect()
        found = await self.session.resolve_index_tokens(self.universe, include_vix=True)
        if not found:
            raise RuntimeError("ANGEL_NO_INSTRUMENT_TOKENS_RESOLVED")
        self._token_map = {str(v["token"]): sym for sym, v in found.items()}
        self._loop = asyncio.get_running_loop()
        from terminal.market.angel import WS_EXCHANGE_TYPE, tick_from_smart
        groups: Dict[int, List[str]] = {}
        for v in found.values():
            groups.setdefault(WS_EXCHANGE_TYPE.get(str(v["exchange"]).upper(), 1), []).append(str(v["token"]))
        token_list = [{"exchangeType": k, "tokens": toks} for k, toks in groups.items()]
        ws = self.session.websocket()
        feed = self

        def on_open(wsapp):
            feed.connected = True
            ws.subscribe("aiterm", ws.SNAP_QUOTE, token_list)
            if feed._option_tokens:
                ws.subscribe("aiterm-opt", ws.SNAP_QUOTE, [{"exchangeType": 2, "tokens": list(feed._option_tokens)}])

        def on_data(wsapp, message):
            f = tick_from_smart(message) if isinstance(message, dict) else None
            if not f:
                return
            osym = feed._option_tokens.get(f["token"])
            if osym is not None and feed._loop and not feed._loop.is_closed():
                asyncio.run_coroutine_threadsafe(feed._emit_option(osym, {"ltp": f["ltp"], "oi": f["oi"], "volume": f["volume"]}), feed._loop)
                return
            sym = feed._token_map.get(f["token"])
            if sym is None:
                return
            tick = Tick(symbol=sym, ltp=f["ltp"], change_pct=round(f["change_pct"], 2), open=f["open"], high=f["high"], low=f["low"], prev_close=f["prev_close"], volume=f["volume"])
            if feed._loop and not feed._loop.is_closed():
                asyncio.run_coroutine_threadsafe(feed._emit(tick), feed._loop)

        def on_error(wsapp=None, error=None):
            feed.errors += 1
            log.warning("angel ws error: %s", error)

        def on_close(wsapp=None):
            feed.connected = False

        ws.on_open, ws.on_data, ws.on_error, ws.on_close = on_open, on_data, on_error, on_close
        import threading
        self._ws = ws
        self._thread = threading.Thread(target=self._run_ws, name="angel-ws", daemon=True)
        self._thread.start()
        self.connected = True
        log.info("Angel One feed starting for %s", ", ".join(found))

    def _run_ws(self) -> None:
        try:
            self._ws.connect()
        except Exception as exc:
            self.errors += 1
            self.connected = False
            log.warning("angel ws thread ended: %s", exc)

    async def stop(self) -> None:
        self.connected = False
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:
                pass
        self._ws = None
        self._thread = None

    async def reconnect(self) -> None:
        self.reconnects += 1
        await self.stop()
        if not self.session.authenticated:
            await self.session.connect()
        await self.start()

    def status(self) -> dict:
        base = super().status()
        base.update({"session": self.session.status(), "reconnects": self.reconnects})
        return base
