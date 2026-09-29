"""Kotak Neo (official SDK ``kotakneoapi`` 3.x) session shared by the live feed,
the option-chain poller and the live broker.

Design rules
* One authenticated session per process (TOTP + MPIN); re-login on demand.
* Every SDK call runs in a worker thread (the SDK is synchronous except the
  SFeed WebSocket, which is asyncio-native and consumed on the main loop).
* Broker payloads are parsed defensively: Kotak field names vary between
  endpoints (``pSymbol``/``pTrdSymbol``/``pSymbolName``/``dStrikePrice`` ...),
  so every parser works from a list of candidate keys and never trusts one.
* The parsers are pure functions so they are unit-tested without the SDK.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from terminal.core.models import Exchange, OptionType, Underlying

log = logging.getLogger("terminal.kotak")

# Kotak "cash" segments carry the indices; F&O segments carry options / futures.
INDEX_SEGMENT = {Exchange.NSE: "nse_cm", Exchange.BSE: "bse_cm", Exchange.MCX: "mcx_fo"}
FO_SEGMENT = {Exchange.NSE: "nse_fo", Exchange.BSE: "bse_fo", Exchange.MCX: "mcx_fo"}
CHAIN_EXCHANGE = {Exchange.NSE: "NSE", Exchange.BSE: "BSE", Exchange.MCX: "MCX"}

# Candidate index names as they appear in Kotak's scrip master (first match wins).
INDEX_NAMES: Dict[str, List[str]] = {
    "NIFTY": ["Nifty 50", "NIFTY 50", "NIFTY"],
    "BANKNIFTY": ["Nifty Bank", "NIFTY BANK", "BANKNIFTY"],
    "FINNIFTY": ["Nifty Fin Service", "NIFTY FIN SERVICE", "FINNIFTY"],
    "MIDCPNIFTY": ["NIFTY MID SELECT", "Nifty Midcap Select", "MIDCPNIFTY"],
    "SENSEX": ["SENSEX", "BSE SENSEX"],
    "BANKEX": ["BANKEX", "BSE BANKEX"],
    "INDIAVIX": ["India VIX", "INDIA VIX", "INDIAVIX"],
}

_EXPIRY_FORMATS = ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d%b%y", "%d-%b-%y", "%Y%m%d")


# --------------------------------------------------------------------------- pure helpers
def _pick(row: Dict[str, Any], *names: str, default: Any = None) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, "", "NA", "-"):
            return v
    return default


def _num(x: Any, default: float = 0.0) -> float:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return default


def parse_expiry(value: Any) -> Optional[dt.date]:
    """Kotak returns expiries as strings in several layouts or as epoch seconds."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    if re.fullmatch(r"\d{9,13}", s):
        secs = int(s) / (1000 if len(s) > 10 else 1)
        return dt.datetime.fromtimestamp(secs, dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
    for fmt in _EXPIRY_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def rows_of(payload: Any) -> List[Dict[str, Any]]:
    """Extract the list of records from a Kotak response envelope."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "Data", "result", "records", "rows", "option_chain", "optionChain", "chain"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                inner = rows_of(v)
                if inner:
                    return inner
        if any(k in payload for k in ("pSymbol", "instrument_token", "strike", "dStrikePrice", "strikePrice")):
            return [payload]
    return []


def response_error(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if payload.get("error") or payload.get("Error"):
        return str(payload.get("error") or payload.get("Error"))[:300]
    stat = str(payload.get("stat") or payload.get("status") or "").lower()
    if stat in ("not_ok", "error", "failed", "fail"):
        return str(payload.get("errMsg") or payload.get("message") or payload)[:300]
    code = payload.get("code")
    if isinstance(code, (int, str)) and str(code).isdigit() and int(code) >= 400:
        return str(payload.get("message") or payload)[:300]
    return None


def parse_scrip_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "token": str(_pick(row, "pSymbol", "instrument_token", "token", "tk", default="")),
        "trading_symbol": str(_pick(row, "pTrdSymbol", "trading_symbol", "tradingsymbol", "ts", default="")),
        "name": str(_pick(row, "pSymbolName", "symbol_name", "pDesc", "name", default="")),
        "segment": str(_pick(row, "pExchSeg", "exchange_segment", "exch_seg", default="")),
        "expiry": parse_expiry(_pick(row, "pExpiryDate", "expiry", "expiry_date", "lExpiryDate")),
        "strike": _num(_pick(row, "dStrikePrice", "strike", "strike_price", "strikePrice", default=0)) / (100.0 if "dStrikePrice" in row and _num(row.get("dStrikePrice")) > 1e6 else 1.0),
        "option_type": str(_pick(row, "pOptionType", "option_type", "optionType", default="")).upper(),
        "inst_type": str(_pick(row, "pInstType", "instrument_type", "pInstName", default="")).upper(),
        "lot_size": int(_num(_pick(row, "lLotSize", "lot_size", "lotSize", default=0))),
    }


def choose_index_token(rows: List[Dict[str, Any]], candidates: List[str]) -> Optional[Dict[str, Any]]:
    parsed = [parse_scrip_row(r) for r in rows]
    for cand in candidates:
        c = cand.lower()
        for p in parsed:
            if c in (p["name"].lower(), p["trading_symbol"].lower()):
                return p
    for cand in candidates:
        c = cand.lower()
        for p in parsed:
            if c in p["name"].lower() or c in p["trading_symbol"].lower():
                return p
    return None


def choose_nearest_future(rows: List[Dict[str, Any]], today: dt.date) -> Optional[Dict[str, Any]]:
    futs = [p for p in (parse_scrip_row(r) for r in rows) if p["expiry"] and p["expiry"] >= today and (p["inst_type"].startswith("FUT") or p["option_type"] in ("", "XX", "FF"))]
    futs = [f for f in futs if f["option_type"] not in ("CE", "PE")]
    return min(futs, key=lambda f: f["expiry"]) if futs else None


def parse_chain_rows(rows: List[Dict[str, Any]]) -> Dict[Tuple[float, str], Dict[str, Any]]:
    """Normalise option-chain records into {(strike, 'CE'|'PE'): quote}."""
    out: Dict[Tuple[float, str], Dict[str, Any]] = {}
    for row in rows:
        strike = _num(_pick(row, "strike", "strikePrice", "dStrikePrice", "strike_price", default=0))
        if strike <= 0:
            continue
        # some layouts nest CE / PE under one strike row
        nested = [(k, row[k]) for k in ("CE", "PE", "ce", "pe", "call", "put") if isinstance(row.get(k), dict)]
        if nested:
            for k, q in nested:
                ot = "CE" if k.lower() in ("ce", "call") else "PE"
                out[(strike, ot)] = _quote_from(q, strike)
            continue
        ot = str(_pick(row, "optionType", "option_type", "pOptionType", "type", default="")).upper()
        if ot not in ("CE", "PE"):
            ts = str(_pick(row, "tradingSymbol", "trading_symbol", "pTrdSymbol", default="")).upper()
            ot = "CE" if ts.endswith("CE") else ("PE" if ts.endswith("PE") else "")
        if ot:
            out[(strike, ot)] = _quote_from(row, strike)
    return out


def _quote_from(q: Dict[str, Any], strike: float) -> Dict[str, Any]:
    return {
        "ltp": _num(_pick(q, "ltp", "lastPrice", "last_price", "last_traded_price", "lp", default=0)),
        "oi": int(_num(_pick(q, "oi", "openInterest", "open_interest", default=0))),
        "oi_change": int(_num(_pick(q, "oiChange", "oi_change", "changeinOpenInterest", "change_in_oi", default=0))),
        "volume": int(_num(_pick(q, "volume", "totalTradedVolume", "volume_traded_today", "vol", default=0))),
        "iv": _num(_pick(q, "iv", "impliedVolatility", "implied_volatility", default=0)),
        "bid": _num(_pick(q, "bid", "bidPrice", "best_bid_price", "bp", default=0)),
        "ask": _num(_pick(q, "ask", "askPrice", "best_ask_price", "sp", default=0)),
        "token": str(_pick(q, "token", "instrument_token", "pSymbol", "tk", default="")),
        "trading_symbol": str(_pick(q, "tradingSymbol", "trading_symbol", "pTrdSymbol", default="")),
        "strike": strike,
    }


def tick_from_message(msg: Any) -> Optional[Dict[str, Any]]:
    """Turn an SFeed message (SFeedIndex / SFeedScrip / SFeedScripLite) into plain fields."""
    g = lambda *names: next((getattr(msg, n) for n in names if getattr(msg, n, None) is not None), None)  # noqa: E731
    ltp = g("last_traded_price", "ltp")
    if ltp is None or float(ltp) <= 0:
        return None
    close = g("close_price") or 0.0
    pct = g("net_change_percent")
    if pct is None and close:
        pct = (float(ltp) / float(close) - 1) * 100
    return {
        "token": str(g("instrument_token") or ""), "segment": str(g("exchange_segment") or ""), "ltp": float(ltp), "prev_close": float(close or 0.0),
        "open": float(g("open_price") or 0.0), "high": float(g("high_price") or 0.0), "low": float(g("low_price") or 0.0), "change_pct": float(pct or 0.0),
        "volume": int(g("volume_traded_today") or 0), "oi": int(g("open_interest") or 0), "kind": type(msg).__name__,
    }


# --------------------------------------------------------------------------- session
class KotakNeoSession:
    provider = "kotak"

    def __init__(self, settings, runtime_dir: Optional[Path] = None) -> None:
        self.s = settings
        self.client = None
        self.authenticated = False
        self.logged_in_at: float = 0.0
        self.last_error = ""
        self.tokens: Dict[str, Dict[str, Any]] = {}  # our symbol -> {token, segment, trading_symbol}
        self._option_cache: Dict[Tuple[str, str, float, str], Dict[str, Any]] = {}
        self._expiry_cache: Dict[str, Dict[str, str]] = {}  # underlying -> {iso: kotak string}
        self._lock = asyncio.Lock()
        self.calls = 0
        self.errors = 0
        self.runtime_dir = runtime_dir

    # ---------------------------------------------------------------- auth
    def missing_credentials(self) -> List[str]:
        s = self.s
        return [k for k, v in {"NEO_CONSUMER_KEY": s.neo_consumer_key, "NEO_MOBILE_NUMBER": s.neo_mobile_number, "NEO_UCC": s.neo_ucc, "NEO_MPIN": s.neo_mpin, "NEO_TOTP_SECRET": s.neo_totp_secret}.items() if not v]

    async def connect(self) -> Dict[str, Any]:
        async with self._lock:
            missing = self.missing_credentials()
            if missing:
                raise RuntimeError("KOTAK_CREDENTIALS_MISSING: " + ", ".join(missing))
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(None, self._login_sync)
            except Exception as exc:
                self.authenticated = False
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                self.errors += 1
                raise
            self.authenticated = True
            self.logged_in_at = time.time()
            self.last_error = ""
            return result

    def _login_sync(self) -> Dict[str, Any]:
        try:
            from neo_api_client import NeoAPI  # type: ignore
            import pyotp  # type: ignore
        except Exception as exc:
            raise RuntimeError("KOTAK_SDK_NOT_INSTALLED: pip install kotakneoapi==3.0.6 pyotp") from exc
        s = self.s
        self.client = NeoAPI(consumer_key=s.neo_consumer_key, environment=getattr(s, "neo_environment", "prod") or "prod")
        totp = pyotp.TOTP(s.neo_totp_secret.replace(" ", "")).now()
        login = self.client.totp_login(mobile_number=s.neo_mobile_number, ucc=s.neo_ucc, totp=totp)
        err = response_error(login)
        if err:
            raise RuntimeError(f"KOTAK_TOTP_LOGIN_FAILED: {err}")
        validate = self.client.totp_validate(mpin=s.neo_mpin)
        err = response_error(validate)
        if err:
            raise RuntimeError(f"KOTAK_MPIN_VALIDATE_FAILED: {err}")
        return {"login": _status(login), "validate": _status(validate)}

    async def ensure(self) -> None:
        if not self.authenticated or self.client is None:
            await self.connect()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous SDK method by name in a worker thread (after ensuring login)."""
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
            if "session" in err.lower() or "token" in err.lower() or "unauthor" in err.lower() or "401" in err:
                self.authenticated = False
            raise RuntimeError(err)
        return out

    # ---------------------------------------------------------------- discovery
    async def resolve_index_tokens(self, universe: List[Underlying], include_vix: bool = True) -> Dict[str, Dict[str, Any]]:
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
        found: Dict[str, Dict[str, Any]] = {}
        targets: List[Tuple[str, Exchange, bool]] = [(u.symbol, u.exchange, u.exchange == Exchange.MCX) for u in universe]
        if include_vix:
            targets.append(("INDIAVIX", Exchange.NSE, False))
        for sym, exch, is_commodity in targets:
            try:
                if is_commodity:
                    rows = rows_of(await self._call("search_scrip", exchange_segment="mcx_fo", symbol=sym))
                    hit = choose_nearest_future(rows, today)
                else:
                    seg = INDEX_SEGMENT[exch]
                    hit = None
                    for cand in INDEX_NAMES.get(sym, [sym]):
                        rows = rows_of(await self._call("search_scrip", exchange_segment=seg, symbol=cand))
                        hit = choose_index_token(rows, [cand] + INDEX_NAMES.get(sym, []))
                        if hit:
                            break
                if hit and hit["token"]:
                    found[sym] = {"token": hit["token"], "segment": hit["segment"] or (INDEX_SEGMENT[exch] if not is_commodity else "mcx_fo"), "trading_symbol": hit["trading_symbol"], "name": hit["name"], "expiry": hit["expiry"].isoformat() if hit.get("expiry") else None}
                else:
                    log.warning("kotak: no scrip found for %s", sym)
            except Exception as exc:
                log.warning("kotak: token discovery failed for %s: %s", sym, exc)
        self.tokens.update(found)
        if self.runtime_dir:
            try:
                (self.runtime_dir / "kotak_tokens.json").write_text(json.dumps(self.tokens, indent=1, default=str))
            except Exception:
                pass
        return found

    async def expiries(self, u: Underlying) -> Dict[str, str]:
        """ISO date -> Kotak's own expiry string (needed for option_chain / search)."""
        if u.symbol in self._expiry_cache:
            return self._expiry_cache[u.symbol]
        out: Dict[str, str] = {}
        try:
            payload = await self._call("expiries", exchange=CHAIN_EXCHANGE[u.exchange], underlying=u.symbol)
            values: List[Any] = []
            if isinstance(payload, dict):
                for key in ("data", "expiries", "expiry", "result"):
                    v = payload.get(key)
                    if isinstance(v, list):
                        values = v
                        break
                    if isinstance(v, dict):
                        for kk in ("expiries", "expiry", "data"):
                            if isinstance(v.get(kk), list):
                                values = v[kk]
                                break
            elif isinstance(payload, list):
                values = payload
            for v in values:
                raw = v if not isinstance(v, dict) else _pick(v, "expiry", "expiryDate", "expiry_date", "date", "value")
                d = parse_expiry(raw)
                if d:
                    out[d.isoformat()] = str(raw)
        except Exception as exc:
            log.warning("kotak: expiries failed for %s: %s", u.symbol, exc)
        if out:
            self._expiry_cache[u.symbol] = out
        return out

    async def option_chain(self, u: Underlying, expiry_iso: str) -> Dict[Tuple[float, str], Dict[str, Any]]:
        exps = await self.expiries(u)
        kotak_expiry = exps.get(expiry_iso, expiry_iso)
        payload = await self._call("option_chain", exchange=CHAIN_EXCHANGE[u.exchange], underlying=u.symbol, expiry=kotak_expiry)
        rows = rows_of(payload)
        parsed = parse_chain_rows(rows)
        if not parsed and not getattr(self, "_chain_shape_logged", False):
            self._chain_shape_logged = True
            log.warning("kotak: could not parse option_chain payload; sample=%s", json.dumps(payload, default=str)[:800])
        for (strike, ot), q in parsed.items():
            if q.get("trading_symbol") or q.get("token"):
                self._option_cache[(u.symbol, expiry_iso, strike, ot)] = q
        return parsed

    async def option_quotes(self, u: Underlying, expiry_iso: str, strikes: Optional[Iterable[float]] = None) -> Dict[Tuple[float, str], Dict[str, Any]]:
        """Generic live-quote entry point (Kotak returns the whole chain; strikes are a filter)."""
        parsed = await self.option_chain(u, expiry_iso)
        if strikes:
            wanted = {float(x) for x in strikes}
            parsed = {k: v for k, v in parsed.items() if k[0] in wanted}
        return parsed

    async def resolve_option(self, u: Underlying, expiry_iso: str, strike: float, option_type: OptionType) -> Optional[Dict[str, Any]]:
        key = (u.symbol, expiry_iso, float(strike), option_type.value)
        cached = self._option_cache.get(key)
        if cached and cached.get("trading_symbol"):
            return cached
        exps = await self.expiries(u)
        kotak_expiry = exps.get(expiry_iso)
        try:
            rows = rows_of(await self._call("search_scrip", exchange_segment=FO_SEGMENT[u.exchange], symbol=u.symbol, expiry=kotak_expiry, option_type=option_type.value, strike_price=str(int(strike) if float(strike).is_integer() else strike)))
        except Exception as exc:
            log.warning("kotak: search_scrip failed for %s %s %s%s: %s", u.symbol, expiry_iso, strike, option_type.value, exc)
            return None
        want = dt.date.fromisoformat(expiry_iso)
        for r in rows:
            p = parse_scrip_row(r)
            if p["option_type"] == option_type.value and abs(p["strike"] - float(strike)) < 1e-6 and (p["expiry"] is None or p["expiry"] == want):
                hit = {"trading_symbol": p["trading_symbol"], "token": p["token"], "segment": p["segment"] or FO_SEGMENT[u.exchange], "lot_size": p["lot_size"]}
                self._option_cache[key] = hit
                return hit
        return None

    async def quotes(self, tokens: List[Dict[str, str]]) -> Any:
        return await self._call("quotes", instrument_tokens=tokens, quote_type="all")

    async def limits(self) -> Dict[str, Any]:
        return await self._call("limits")

    async def positions(self) -> Any:
        return await self._call("positions")

    def websocket(self):
        if self.client is None or not self.authenticated:
            raise RuntimeError("KOTAK_NOT_AUTHENTICATED")
        return self.client.create_websocket()

    def status(self) -> dict:
        return {"provider": self.provider, "authenticated": self.authenticated, "logged_in_at": self.logged_in_at, "calls": self.calls, "errors": self.errors, "last_error": self.last_error, "needs_daily_login": False,
                "tokens": {k: v.get("trading_symbol") or v.get("token") for k, v in self.tokens.items()}, "missing_credentials": self.missing_credentials()}


def _status(x: Any) -> str:
    if isinstance(x, dict):
        return str(x.get("stat") or x.get("status") or ("OK" if not x.get("error") else "ERROR"))
    return "OK"
