"""Zerodha Kite Connect session shared by the live feed, the option-quote
poller and the live broker.

Kite specifics
* Access tokens are issued per day through a browser login: ``login_url()`` →
  user logs in → ``request_token`` → ``generate_session(request_token,
  api_secret)`` → ``access_token`` (valid until ~06:00 IST next day). The
  session persists the token in ``runtime/zerodha_session.json`` so a morning
  login (``scripts/zerodha_login.py`` or the System panel) is enough.
* Instruments come from the daily instrument dump; every symbol / token is
  resolved from it, never constructed by hand.
* Kite has no option-chain endpoint: quotes are fetched for the strikes the
  terminal needs (≤ 500 instruments per call).
* The KiteTicker WebSocket is thread + callback based; ticks are bridged into
  the asyncio loop.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from terminal.core.models import Exchange, OptionType, Underlying
from terminal.market.live_base import LiveSession

log = logging.getLogger("terminal.zerodha")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX_EXCHANGE = {Exchange.NSE: "NSE", Exchange.BSE: "BSE", Exchange.MCX: "MCX"}
FO_EXCHANGE = {Exchange.NSE: "NFO", Exchange.BSE: "BFO", Exchange.MCX: "MCX"}
INDEX_NAMES: Dict[str, List[str]] = {
    "NIFTY": ["NIFTY 50"], "BANKNIFTY": ["NIFTY BANK"], "FINNIFTY": ["NIFTY FIN SERVICE"], "MIDCPNIFTY": ["NIFTY MID SELECT"],
    "SENSEX": ["SENSEX"], "BANKEX": ["BANKEX"], "INDIAVIX": ["INDIA VIX"],
}
# well-known Kite index tokens, used only if the instrument dump lookup fails
FALLBACK_INDEX_TOKENS = {"NIFTY": (256265, "NSE"), "BANKNIFTY": (260105, "NSE"), "FINNIFTY": (257801, "NSE"), "MIDCPNIFTY": (288009, "NSE"), "INDIAVIX": (264969, "NSE"), "SENSEX": (265, "BSE"), "BANKEX": (274441, "BSE")}


# --------------------------------------------------------------------------- pure helpers
def _date(v: Any) -> dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_instrument(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "token": int(_f(row.get("instrument_token"), 0)), "tradingsymbol": str(row.get("tradingsymbol") or ""), "name": str(row.get("name") or ""),
        "exchange": str(row.get("exchange") or ""), "segment": str(row.get("segment") or ""), "expiry": _date(row.get("expiry")),
        "strike": _f(row.get("strike")), "instrument_type": str(row.get("instrument_type") or "").upper(), "lot_size": int(_f(row.get("lot_size"), 0)), "tick_size": _f(row.get("tick_size"), 0.05),
    }


def choose_index(instruments: Iterable[Dict[str, Any]], names: List[str]) -> Dict[str, Any] | None:
    wanted = {n.upper() for n in names}
    for row in instruments:
        p = parse_instrument(row)
        if p["tradingsymbol"].upper() in wanted and (p["segment"].upper() == "INDICES" or p["instrument_type"] in ("EQ", "")):
            return p
    return None


def choose_nearest_future(instruments: Iterable[Dict[str, Any]], name: str, today: dt.date) -> Dict[str, Any] | None:
    futs = [p for p in (parse_instrument(r) for r in instruments) if p["name"].upper() == name.upper() and p["instrument_type"] == "FUT" and p["expiry"] and p["expiry"] >= today]
    return min(futs, key=lambda p: p["expiry"]) if futs else None


def option_instruments(instruments: Iterable[Dict[str, Any]], name: str, expiry: dt.date) -> Dict[Tuple[float, str], Dict[str, Any]]:
    out: Dict[Tuple[float, str], Dict[str, Any]] = {}
    for row in instruments:
        p = parse_instrument(row)
        if p["name"].upper() == name.upper() and p["instrument_type"] in ("CE", "PE") and p["expiry"] == expiry:
            out[(p["strike"], p["instrument_type"])] = p
    return out


def expiries_for(instruments: Iterable[Dict[str, Any]], name: str, today: dt.date) -> List[dt.date]:
    exps = {p["expiry"] for p in (parse_instrument(r) for r in instruments) if p["name"].upper() == name.upper() and p["instrument_type"] in ("CE", "PE") and p["expiry"] and p["expiry"] >= today}
    return sorted(exps)


def parse_quote(q: Dict[str, Any]) -> Dict[str, Any]:
    depth = q.get("depth") or {}
    buy = (depth.get("buy") or [{}])[0] if depth.get("buy") else {}
    sell = (depth.get("sell") or [{}])[0] if depth.get("sell") else {}
    ohlc = q.get("ohlc") or {}
    return {
        "ltp": _f(q.get("last_price")), "oi": int(_f(q.get("oi"))), "volume": int(_f(q.get("volume") or q.get("volume_traded"))), "bid": _f(buy.get("price")), "ask": _f(sell.get("price")),
        "oi_change": 0, "iv": 0.0, "token": str(q.get("instrument_token") or ""), "prev_close": _f(ohlc.get("close")), "open": _f(ohlc.get("open")), "high": _f(ohlc.get("high")), "low": _f(ohlc.get("low")),
    }


def tick_from_kite(tick: Dict[str, Any]) -> Dict[str, Any] | None:
    ltp = _f(tick.get("last_price"))
    if ltp <= 0:
        return None
    ohlc = tick.get("ohlc") or {}
    close = _f(ohlc.get("close"))
    pct = tick.get("change")
    if pct is None and close:
        pct = (ltp / close - 1) * 100
    return {"token": int(_f(tick.get("instrument_token"))), "ltp": ltp, "prev_close": close, "open": _f(ohlc.get("open")), "high": _f(ohlc.get("high")), "low": _f(ohlc.get("low")),
            "change_pct": _f(pct), "volume": int(_f(tick.get("volume_traded") or tick.get("volume"))), "oi": int(_f(tick.get("oi")))}


# --------------------------------------------------------------------------- session
class ZerodhaSession(LiveSession):
    provider = "zerodha"
    needs_daily_login = True
    client_attr = "kite"

    def __init__(self, settings, runtime_dir: Path | None = None) -> None:
        super().__init__(settings, runtime_dir)
        self.kite = None
        self.access_token: str = settings.zerodha_access_token or ""
        self._instruments: Dict[str, List[Dict[str, Any]]] = {}
        self._instruments_day: dt.date | None = None
    # ---------------------------------------------------------------- auth
    def missing_credentials(self) -> List[str]:
        out = []
        if not self.s.zerodha_api_key:
            out.append("ZERODHA_API_KEY")
        if not (self.access_token or self._saved_token() or (self.s.zerodha_request_token and self.s.zerodha_api_secret)):
            out.append("ZERODHA_ACCESS_TOKEN (or ZERODHA_REQUEST_TOKEN + ZERODHA_API_SECRET, or run scripts/zerodha_login.py)")
        return out

    def _session_file(self) -> Path | None:
        return (self.runtime_dir / "zerodha_session.json") if self.runtime_dir else None

    def _saved_token(self) -> str:
        f = self._session_file()
        if not f or not f.exists():
            return ""
        try:
            data = json.loads(f.read_text())
            if data.get("date") == dt.datetime.now(IST).date().isoformat() and data.get("access_token"):
                return str(data["access_token"])
        except Exception:
            pass
        return ""

    def _save_token(self, token: str, user_id: str = "") -> None:
        f = self._session_file()
        if f:
            try:
                f.write_text(json.dumps({"access_token": token, "user_id": user_id, "date": dt.datetime.now(IST).date().isoformat()}))
            except Exception:
                pass

    def _kite_class(self):
        try:
            from kiteconnect import KiteConnect  # type: ignore
        except Exception as exc:
            raise RuntimeError("KITE_SDK_NOT_INSTALLED: pip install kiteconnect") from exc
        return KiteConnect

    def login_url(self) -> str:
        if not self.s.zerodha_api_key:
            raise RuntimeError("ZERODHA_API_KEY_MISSING")
        return self._kite_class()(api_key=self.s.zerodha_api_key).login_url()

    async def exchange_request_token(self, request_token: str) -> Dict[str, Any]:
        if not self.s.zerodha_api_secret:
            raise RuntimeError("ZERODHA_API_SECRET_MISSING")
        loop = asyncio.get_running_loop()

        def _gen():
            kite = self._kite_class()(api_key=self.s.zerodha_api_key)
            data = kite.generate_session(request_token, api_secret=self.s.zerodha_api_secret)
            return data

        data = await loop.run_in_executor(None, _gen)
        self.access_token = str(data.get("access_token") or "")
        self.user_id = str(data.get("user_id") or "")
        if not self.access_token:
            raise RuntimeError("ZERODHA_SESSION_NO_ACCESS_TOKEN")
        self._save_token(self.access_token, self.user_id)
        self.authenticated = False
        await self.connect()
        return {"user_id": self.user_id, "login_time": str(data.get("login_time") or "")}

    async def connect(self) -> Dict[str, Any]:
        async with self._lock:
            if not self.s.zerodha_api_key:
                raise RuntimeError("ZERODHA_CREDENTIALS_MISSING: ZERODHA_API_KEY")
            token = self.access_token or self._saved_token()
            loop = asyncio.get_running_loop()
            if not token and self.s.zerodha_request_token and self.s.zerodha_api_secret:
                try:
                    kite = self._kite_class()(api_key=self.s.zerodha_api_key)
                    data = await loop.run_in_executor(None, lambda: kite.generate_session(self.s.zerodha_request_token, api_secret=self.s.zerodha_api_secret))
                    token = str(data.get("access_token") or "")
                    self.user_id = str(data.get("user_id") or "")
                    self._save_token(token, self.user_id)
                except Exception as exc:
                    raise RuntimeError(f"ZERODHA_REQUEST_TOKEN_EXCHANGE_FAILED: {exc} (request tokens are single-use; run scripts/zerodha_login.py)") from exc
            if not token:
                raise RuntimeError("ZERODHA_CREDENTIALS_MISSING: " + ", ".join(self.missing_credentials()))
            try:
                kite = self._kite_class()(api_key=self.s.zerodha_api_key, access_token=token)
                profile = await loop.run_in_executor(None, kite.profile)
            except Exception as exc:
                self._mark_login_failed(exc)
                raise RuntimeError(f"ZERODHA_LOGIN_FAILED: {exc} (access tokens expire daily; run scripts/zerodha_login.py)") from exc
            self.kite = kite
            self.access_token = token
            self.user_id = str(profile.get("user_id") or self.user_id)
            self._mark_logged_in()
            return {"user_id": self.user_id, "user_name": profile.get("user_name")}

    # ---------------------------------------------------------------- instruments
    async def instruments(self, exchange: str) -> List[Dict[str, Any]]:
        today = dt.datetime.now(IST).date()
        if self._instruments_day != today:
            self._instruments.clear()
            self._instruments_day = today
        if exchange not in self._instruments:
            rows = await self._call("instruments", exchange)
            self._instruments[exchange] = list(rows or [])
            log.info("zerodha: loaded %d instruments for %s", len(self._instruments[exchange]), exchange)
        return self._instruments[exchange]

    async def resolve_index_tokens(self, universe: List[Underlying], include_vix: bool = True) -> Dict[str, Dict[str, Any]]:
        today = dt.datetime.now(IST).date()
        found: Dict[str, Dict[str, Any]] = {}
        targets = [(u.symbol, u.exchange) for u in universe] + ([("INDIAVIX", Exchange.NSE)] if include_vix else [])
        for sym, exch in targets:
            try:
                if exch == Exchange.MCX:
                    hit = choose_nearest_future(await self.instruments("MCX"), sym, today)
                else:
                    hit = choose_index(await self.instruments(INDEX_EXCHANGE[exch]), INDEX_NAMES.get(sym, [sym]))
                if hit is None and sym in FALLBACK_INDEX_TOKENS:
                    tok, ex = FALLBACK_INDEX_TOKENS[sym]
                    hit = {"token": tok, "tradingsymbol": INDEX_NAMES.get(sym, [sym])[0], "exchange": ex, "expiry": None}
                if hit and hit["token"]:
                    found[sym] = {"token": int(hit["token"]), "exchange": hit["exchange"], "trading_symbol": hit["tradingsymbol"], "expiry": hit["expiry"].isoformat() if hit.get("expiry") else None}
                else:
                    log.warning("zerodha: no instrument for %s", sym)
            except Exception as exc:
                log.warning("zerodha: token discovery failed for %s: %s", sym, exc)
        self.tokens.update(found)
        if self.runtime_dir:
            try:
                (self.runtime_dir / "zerodha_tokens.json").write_text(json.dumps(self.tokens, indent=1, default=str))
            except Exception:
                pass
        return found

    async def expiries(self, u: Underlying) -> List[str]:
        today = dt.datetime.now(IST).date()
        return [d.isoformat() for d in expiries_for(await self.instruments(FO_EXCHANGE[u.exchange]), u.symbol, today)]

    async def option_quotes(self, u: Underlying, expiry_iso: str, strikes: Iterable[float] | None = None) -> Dict[Tuple[float, str], Dict[str, Any]]:
        """Live quotes for the option strikes of one expiry (LTP, OI, volume, bid/ask)."""
        exchange = FO_EXCHANGE[u.exchange]
        table = option_instruments(await self.instruments(exchange), u.symbol, dt.date.fromisoformat(expiry_iso))
        if not table:
            return {}
        wanted = set(float(x) for x in strikes) if strikes else None
        keys = [k for k in table if wanted is None or k[0] in wanted]
        if not keys:
            return {}
        out: Dict[Tuple[float, str], Dict[str, Any]] = {}
        for i in range(0, len(keys), 400):
            batch = keys[i:i + 400]
            symbols = [f"{exchange}:{table[k]['tradingsymbol']}" for k in batch]
            resp = await self._call("quote", *symbols) or {}
            for k, sym in zip(batch, symbols):
                q = resp.get(sym)
                if not q:
                    continue
                parsed = parse_quote(q)
                parsed["trading_symbol"] = table[k]["tradingsymbol"]
                parsed["token"] = str(table[k]["token"])
                out[k] = parsed
                self._option_cache[(u.symbol, expiry_iso, k[0], k[1])] = {"trading_symbol": table[k]["tradingsymbol"], "token": table[k]["token"], "exchange": exchange, "lot_size": table[k]["lot_size"]}
        return out

    async def resolve_option(self, u: Underlying, expiry_iso: str, strike: float, option_type: OptionType) -> Dict[str, Any] | None:
        key = (u.symbol, expiry_iso, float(strike), option_type.value)
        if key in self._option_cache:
            return self._option_cache[key]
        exchange = FO_EXCHANGE[u.exchange]
        table = option_instruments(await self.instruments(exchange), u.symbol, dt.date.fromisoformat(expiry_iso))
        row = table.get((float(strike), option_type.value))
        if not row:
            return None
        hit = {"trading_symbol": row["tradingsymbol"], "token": row["token"], "exchange": exchange, "lot_size": row["lot_size"]}
        self._option_cache[key] = hit
        return hit

    async def margins(self) -> Dict[str, Any]:
        return await self._call("margins")

    async def positions(self) -> Any:
        return await self._call("positions")

    def ticker(self):
        if not self.authenticated or not self.access_token:
            raise RuntimeError("ZERODHA_NOT_AUTHENTICATED")
        from kiteconnect import KiteTicker  # type: ignore
        return KiteTicker(self.s.zerodha_api_key, self.access_token)

