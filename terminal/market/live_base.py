"""Shared base for live broker sessions (Kotak Neo, Zerodha Kite, Angel One).

Every adapter provides the same surface to the terminal:
  connect() / ensure() / missing_credentials()
  resolve_index_tokens(universe) -> {our symbol: {token, exchange|segment, trading_symbol}}
  option_quotes(u, expiry_iso, strikes) -> {(strike, 'CE'|'PE'): quote}
  resolve_option(u, expiry_iso, strike, option_type) -> {trading_symbol, token, ...}
  lookup_trading_symbol(trading_symbol) -> (underlying, expiry_iso, strike, ot)
  status()
The base class owns the option cache, the reverse trading-symbol index, the
executor-backed SDK call wrapper and the status payload; adapters keep only
the broker-specific parsing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from terminal.core.models import OptionType, Underlying

log = logging.getLogger("terminal.live")


class LiveSession:
    provider = "abstract"
    needs_daily_login = False
    client_attr = "client"  # name of the attribute holding the SDK client

    def __init__(self, settings, runtime_dir: Path | None = None) -> None:
        self.s = settings
        self.runtime_dir = runtime_dir
        self.authenticated = False
        self.logged_in_at = 0.0
        self.last_error = ""
        self.user_id = ""
        self.calls = 0
        self.errors = 0
        self.tokens: Dict[str, Dict[str, Any]] = {}
        self._option_cache: Dict[Tuple[str, str, float, str], Dict[str, Any]] = {}
        self._by_trading_symbol: Dict[str, Tuple[str, str, float, str]] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- to implement
    def missing_credentials(self) -> List[str]:
        raise NotImplementedError

    async def connect(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def resolve_index_tokens(self, universe: List[Underlying], include_vix: bool = True) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError

    async def option_quotes(self, u: Underlying, expiry_iso: str, strikes: Iterable[float] | None = None) -> Dict[Tuple[float, str], Dict[str, Any]]:
        raise NotImplementedError

    async def resolve_option(self, u: Underlying, expiry_iso: str, strike: float, option_type: OptionType) -> Dict[str, Any] | None:
        raise NotImplementedError

    def _response_error(self, payload: Any) -> str | None:
        return None

    def _auth_error(self, message: str) -> bool:
        m = message.lower()
        return any(x in m for x in ("session", "token", "unauthor", "401", "expired"))

    # ---------------------------------------------------------------- shared
    async def ensure(self) -> None:
        if not self.authenticated or getattr(self, self.client_attr, None) is None:
            await self.connect()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous SDK method by name in a worker thread after ensuring login."""
        await self.ensure()
        fn = getattr(getattr(self, self.client_attr), method)
        loop = asyncio.get_running_loop()
        self.calls += 1
        try:
            out = await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except Exception as exc:
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            if type(exc).__name__ in ("TokenException", "PermissionException") or self._auth_error(str(exc)):
                self.authenticated = False
            raise
        err = self._response_error(out)
        if err:
            self.errors += 1
            self.last_error = err
            if self._auth_error(err):
                self.authenticated = False
            raise RuntimeError(err)
        return out

    def _cache_option(self, key: Tuple[str, str, float, str], hit: Dict[str, Any]) -> Dict[str, Any]:
        self._option_cache[key] = hit
        ts = str(hit.get("trading_symbol") or "")
        if ts:
            self._by_trading_symbol[ts.upper()] = key
        return hit

    def _index_trading_symbol(self, key: Tuple[str, str, float, str], hit: Dict[str, Any]) -> None:
        self._cache_option(key, hit)

    def lookup_trading_symbol(self, trading_symbol: str) -> Tuple[str, str, float, str] | None:
        return self._by_trading_symbol.get(str(trading_symbol).upper())

    def _mark_logged_in(self) -> None:
        self.authenticated = True
        self.logged_in_at = time.time()
        self.last_error = ""

    def _mark_login_failed(self, exc: Exception) -> None:
        self.authenticated = False
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"[:300]

    def status(self) -> dict:
        return {"provider": self.provider, "authenticated": self.authenticated, "logged_in_at": self.logged_in_at, "user_id": self.user_id, "calls": self.calls, "errors": self.errors,
                "last_error": self.last_error, "tokens": {k: v.get("trading_symbol") or v.get("token") for k, v in self.tokens.items()}, "missing_credentials": self.missing_credentials(),
                "needs_daily_login": self.needs_daily_login, "cached_contracts": len(self._option_cache)}
