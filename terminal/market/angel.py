"""Angel One SmartAPI session shared by the live feed, the option-quote poller
and the live broker.

Angel specifics
* Login is client code + trading PIN + TOTP (``generateSession``); the JWT is
  valid for the trading day and the feed token authenticates the WebSocket.
* The instrument master is a public daily JSON (no auth); every token and
  trading symbol is resolved from it. Strikes are stored ×100 and expiries as
  ``07OCT2026``.
* WebSocket prices arrive in paise (÷100); quotes come from ``getMarketData``
  in batches of 50 tokens.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from terminal.core.models import Exchange, OptionType, Underlying

log = logging.getLogger("terminal.angel")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
FO_EXCHANGE = {Exchange.NSE: "NFO", Exchange.BSE: "BFO", Exchange.MCX: "MCX"}
INDEX_EXCHANGE = {Exchange.NSE: "NSE", Exchange.BSE: "BSE", Exchange.MCX: "MCX"}
WS_EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}
INDEX_NAMES: Dict[str, List[str]] = {
    "NIFTY": ["Nifty 50", "NIFTY 50", "NIFTY"], "BANKNIFTY": ["Nifty Bank", "NIFTY BANK", "BANKNIFTY"], "FINNIFTY": ["Nifty Fin Service", "NIFTY FIN SERVICE", "FINNIFTY"],
    "MIDCPNIFTY": ["NIFTY MID SELECT", "Nifty Midcap Select", "MIDCPNIFTY"], "SENSEX": ["SENSEX", "BSE SENSEX"], "BANKEX": ["BANKEX"], "INDIAVIX": ["India VIX", "INDIA VIX", "INDIAVIX"],
}
# Angel's well-known index tokens (used when the master lookup fails)
FALLBACK_INDEX_TOKENS = {"NIFTY": ("99926000", "NSE"), "BANKNIFTY": ("99926009", "NSE"), "FINNIFTY": ("99926037", "NSE"), "MIDCPNIFTY": ("99926074", "NSE"), "INDIAVIX": ("99926017", "NSE"), "SENSEX": ("99919000", "BSE"), "BANKEX": ("99919012", "BSE")}


# --------------------------------------------------------------------------- pure helpers
def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_expiry(value: Any) -> Optional[dt.date]:
    if value in (None, ""):
        return None
    s = str(value).strip()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d%b%y"):
        try:
            return dt.datetime.strptime(s.upper() if fmt.startswith("%d%b") else s, fmt).date()
        except ValueError:
            continue
    return None


def parse_master_row(row: Dict[str, Any]) -> Dict[str, Any]:
    strike = _f(row.get("strike"))
    return {
        "token": str(row.get("token") or row.get("symboltoken") or ""), "symbol": str(row.get("symbol") or row.get("tradingsymbol") or ""), "name": str(row.get("name") or ""),
        "expiry": parse_expiry(row.get("expiry")), "strike": strike / 100.0 if strike > 0 else 0.0, "lot_size": int(_f(row.get("lotsize"), 0)),
        "instrument_type": str(row.get("instrumenttype") or "").upper(), "exchange": str(row.get("exch_seg") or row.get("exchange") or "").upper(), "tick_size": _f(row.get("tick_size"), 5) / 100.0,
    }


def option_type_of(p: Dict[str, Any]) -> str:
    sym = p["symbol"].upper()
    if p["instrument_type"].startswith("OPT") and sym.endswith(("CE", "PE")):
        return sym[-2:]
    return ""


def choose_index(rows: Iterable[Dict[str, Any]], names: List[str], exchange: str) -> Optional[Dict[str, Any]]:
    wanted = [n.upper() for n in names]
    parsed = [parse_master_row(r) for r in rows if str(r.get("exch_seg", "")).upper() == exchange]
    for w in wanted:
        for p in parsed:
            if p["instrument_type"] in ("AMXIDX", "INDEX", "") and (p["symbol"].upper() == w or p["name"].upper() == w):
                return p
    return None


def choose_nearest_future(rows: Iterable[Dict[str, Any]], name: str, exchange: str, today: dt.date) -> Optional[Dict[str, Any]]:
    futs = [p for p in (parse_master_row(r) for r in rows) if p["exchange"] == exchange and p["name"].upper() == name.upper() and p["instrument_type"].startswith("FUT") and p["expiry"] and p["expiry"] >= today]
    return min(futs, key=lambda p: p["expiry"]) if futs else None


def option_instruments(rows: Iterable[Dict[str, Any]], name: str, exchange: str, expiry: dt.date) -> Dict[Tuple[float, str], Dict[str, Any]]:
    out: Dict[Tuple[float, str], Dict[str, Any]] = {}
    for r in rows:
        p = parse_master_row(r)
        if p["exchange"] != exchange or p["name"].upper() != name.upper() or p["expiry"] != expiry:
            continue
        ot = option_type_of(p)
        if ot:
            out[(p["strike"], ot)] = p
    return out


def expiries_for(rows: Iterable[Dict[str, Any]], name: str, exchange: str, today: dt.date) -> List[dt.date]:
    exps = {p["expiry"] for p in (parse_master_row(r) for r in rows) if p["exchange"] == exchange and p["name"].upper() == name.upper() and p["expiry"] and p["expiry"] >= today and option_type_of(p)}
    return sorted(exps)


def parse_market_quote(q: Dict[str, Any]) -> Dict[str, Any]:
    depth = q.get("depth") or {}
    buy = (depth.get("buy") or [{}])[0] if depth.get("buy") else {}
    sell = (depth.get("sell") or [{}])[0] if depth.get("sell") else {}
    return {"ltp": _f(q.get("ltp")), "oi": int(_f(q.get("opnInterest") or q.get("openInterest"))), "volume": int(_f(q.get("tradeVolume") or q.get("volume"))),
            "bid": _f(buy.get("price")), "ask": _f(sell.get("price")), "oi_change": 0, "iv": 0.0, "token": str(q.get("symbolToken") or ""), "trading_symbol": str(q.get("tradingSymbol") or ""),
            "prev_close": _f(q.get("close")), "open": _f(q.get("open")), "high": _f(q.get("high")), "low": _f(q.get("low"))}


def tick_from_smart(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """SmartWebSocketV2 parsed packet -> plain fields (prices are in paise)."""
    ltp = _f(msg.get("last_traded_price")) / 100.0
    if ltp <= 0:
        return None
    close = _f(msg.get("closed_price")) / 100.0
    return {"token": str(msg.get("token") or ""), "exchange_type": int(_f(msg.get("exchange_type"))), "ltp": ltp, "prev_close": close, "open": _f(msg.get("open_price_of_the_day")) / 100.0,
            "high": _f(msg.get("high_price_of_the_day")) / 100.0, "low": _f(msg.get("low_price_of_the_day")) / 100.0, "change_pct": (ltp / close - 1) * 100 if close else 0.0,
            "volume": int(_f(msg.get("volume_trade_for_the_day"))), "oi": int(_f(msg.get("open_interest")))}


def response_error(payload: Any) -> Optional[str]:
    if isinstance(payload, dict) and payload.get("status") is False:
        return str(payload.get("message") or payload.get("errorcode") or payload)[:300]
    return None


# --------------------------------------------------------------------------- session
class AngelOneSession:
    provider = "angel"

    def __init__(self, settings, runtime_dir: Optional[Path] = None) -> None:
        self.s = settings
        self.runtime_dir = runtime_dir
        self.client = None
        self.jwt_token = ""
        self.feed_token = ""
        self.refresh_token = ""
        self.authenticated = False
        self.logged_in_at = 0.0
        self.last_error = ""
        self.user_id = ""
        self.calls = 0
        self.errors = 0
        self.tokens: Dict[str, Dict[str, Any]] = {}
        self._master: List[Dict[str, Any]] = []
        self._master_day: Optional[dt.date] = None
        self._option_cache: Dict[Tuple[str, str, float, str], Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- auth
    def missing_credentials(self) -> List[str]:
        s = self.s
        return [k for k, v in {"ANGEL_API_KEY": s.angel_api_key, "ANGEL_CLIENT_CODE": s.angel_client_code, "ANGEL_PIN": s.angel_pin, "ANGEL_TOTP_SECRET": s.angel_totp_secret}.items() if not v]

    async def connect(self) -> Dict[str, Any]:
        async with self._lock:
            missing = self.missing_credentials()
            if missing:
                raise RuntimeError("ANGEL_CREDENTIALS_MISSING: " + ", ".join(missing))
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(None, self._login_sync)
            except Exception as exc:
                self.authenticated = False
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                raise
            self.authenticated = True
            self.logged_in_at = time.time()
            self.last_error = ""
            return result

    def _login_sync(self) -> Dict[str, Any]:
        try:
            from SmartApi import SmartConnect  # type: ignore
            import pyotp  # type: ignore
        except Exception as exc:
            raise RuntimeError("ANGEL_SDK_NOT_INSTALLED: pip install smartapi-python pyotp logzero websocket-client") from exc
        s = self.s
        client = SmartConnect(api_key=s.angel_api_key)
        totp = pyotp.TOTP(s.angel_totp_secret.replace(" ", "")).now()
        resp = client.generateSession(s.angel_client_code, s.angel_pin, totp)
        err = response_error(resp)
        if err or not isinstance(resp, dict) or not resp.get("data"):
            raise RuntimeError(f"ANGEL_LOGIN_FAILED: {err or resp}")
        data = resp["data"]
        self.client = client
        self.jwt_token = str(data.get("jwtToken") or "").replace("Bearer ", "")
        self.refresh_token = str(data.get("refreshToken") or "")
        self.feed_token = str(data.get("feedToken") or client.getfeedToken() or "")
        self.user_id = str(data.get("clientcode") or s.angel_client_code)
        return {"user_id": self.user_id, "name": data.get("name")}

    async def ensure(self) -> None:
        if not self.authenticated or self.client is None:
            await self.connect()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        await self.ensure()
        fn = getattr(self.client, method)
        loop = asyncio.get_running_loop()
        self.calls += 1
        try:
            out = await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except Exception as exc:
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            raise
        err = response_error(out)
        if err:
            self.errors += 1
            self.last_error = err
            if "token" in err.lower() or "session" in err.lower() or "AG8001" in err or "AB8050" in err:
                self.authenticated = False
            raise RuntimeError(err)
        return out

    # ---------------------------------------------------------------- instrument master
    async def master(self) -> List[Dict[str, Any]]:
        today = dt.datetime.now(IST).date()
        if self._master and self._master_day == today:
            return self._master
        cache = (self.runtime_dir / "angel_scrip_master.json") if self.runtime_dir else None
        rows: List[Dict[str, Any]] = []
        if cache and cache.exists() and dt.datetime.fromtimestamp(cache.stat().st_mtime, IST).date() == today:
            try:
                rows = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        if not rows:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(SCRIP_MASTER_URL)
                r.raise_for_status()
                rows = r.json()
            if cache:
                try:
                    cache.write_text(json.dumps(rows), encoding="utf-8")
                except Exception:
                    pass
            log.info("angel: loaded %d instruments", len(rows))
        self._master, self._master_day = rows, today
        return rows

    async def resolve_index_tokens(self, universe: List[Underlying], include_vix: bool = True) -> Dict[str, Dict[str, Any]]:
        today = dt.datetime.now(IST).date()
        rows = await self.master()
        found: Dict[str, Dict[str, Any]] = {}
        targets = [(u.symbol, u.exchange) for u in universe] + ([("INDIAVIX", Exchange.NSE)] if include_vix else [])
        for sym, exch in targets:
            try:
                if exch == Exchange.MCX:
                    hit = choose_nearest_future(rows, sym, "MCX", today)
                else:
                    hit = choose_index(rows, INDEX_NAMES.get(sym, [sym]), INDEX_EXCHANGE[exch])
                if hit is None and sym in FALLBACK_INDEX_TOKENS:
                    tok, ex = FALLBACK_INDEX_TOKENS[sym]
                    hit = {"token": tok, "symbol": INDEX_NAMES.get(sym, [sym])[0], "exchange": ex, "expiry": None}
                if hit and hit["token"]:
                    found[sym] = {"token": str(hit["token"]), "exchange": hit["exchange"], "trading_symbol": hit["symbol"], "expiry": hit["expiry"].isoformat() if hit.get("expiry") else None}
                else:
                    log.warning("angel: no instrument for %s", sym)
            except Exception as exc:
                log.warning("angel: token discovery failed for %s: %s", sym, exc)
        self.tokens.update(found)
        if self.runtime_dir:
            try:
                (self.runtime_dir / "angel_tokens.json").write_text(json.dumps(self.tokens, indent=1, default=str))
            except Exception:
                pass
        return found

    async def expiries(self, u: Underlying) -> List[str]:
        today = dt.datetime.now(IST).date()
        return [d.isoformat() for d in expiries_for(await self.master(), u.symbol, FO_EXCHANGE[u.exchange], today)]

    async def option_quotes(self, u: Underlying, expiry_iso: str, strikes: Optional[Iterable[float]] = None) -> Dict[Tuple[float, str], Dict[str, Any]]:
        exchange = FO_EXCHANGE[u.exchange]
        table = option_instruments(await self.master(), u.symbol, exchange, dt.date.fromisoformat(expiry_iso))
        if not table:
            return {}
        wanted = {float(x) for x in strikes} if strikes else None
        keys = [k for k in table if wanted is None or k[0] in wanted]
        out: Dict[Tuple[float, str], Dict[str, Any]] = {}
        by_token = {table[k]["token"]: k for k in keys}
        tokens = list(by_token)
        for i in range(0, len(tokens), 50):
            batch = tokens[i:i + 50]
            resp = await self._call("getMarketData", "FULL", {exchange: batch})
            data = (resp or {}).get("data") or {}
            for q in data.get("fetched") or []:
                k = by_token.get(str(q.get("symbolToken")))
                if k is None:
                    continue
                parsed = parse_market_quote(q)
                parsed["trading_symbol"] = parsed["trading_symbol"] or table[k]["symbol"]
                out[k] = parsed
                self._option_cache[(u.symbol, expiry_iso, k[0], k[1])] = {"trading_symbol": table[k]["symbol"], "token": table[k]["token"], "exchange": exchange, "lot_size": table[k]["lot_size"]}
            await asyncio.sleep(0.25)  # SmartAPI market-data rate limit is per second
        return out

    async def resolve_option(self, u: Underlying, expiry_iso: str, strike: float, option_type: OptionType) -> Optional[Dict[str, Any]]:
        key = (u.symbol, expiry_iso, float(strike), option_type.value)
        if key in self._option_cache:
            return self._option_cache[key]
        exchange = FO_EXCHANGE[u.exchange]
        table = option_instruments(await self.master(), u.symbol, exchange, dt.date.fromisoformat(expiry_iso))
        row = table.get((float(strike), option_type.value))
        if not row:
            return None
        hit = {"trading_symbol": row["symbol"], "token": row["token"], "exchange": exchange, "lot_size": row["lot_size"]}
        self._option_cache[key] = hit
        return hit

    async def margins(self) -> Dict[str, Any]:
        return await self._call("rmsLimit")

    async def positions(self) -> Any:
        return await self._call("position")

    def websocket(self):
        if not self.authenticated or not self.feed_token:
            raise RuntimeError("ANGEL_NOT_AUTHENTICATED")
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore
        return SmartWebSocketV2(self.jwt_token, self.s.angel_api_key, self.s.angel_client_code, self.feed_token, max_retry_attempt=5)

    def status(self) -> dict:
        return {"provider": self.provider, "authenticated": self.authenticated, "logged_in_at": self.logged_in_at, "user_id": self.user_id, "calls": self.calls, "errors": self.errors,
                "last_error": self.last_error, "tokens": {k: v.get("trading_symbol") or v.get("token") for k, v in self.tokens.items()}, "missing_credentials": self.missing_credentials(),
                "needs_daily_login": False}
