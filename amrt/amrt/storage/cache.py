"""Disposable cache and rate limiter: Redis when configured, in-process otherwise.

Nothing safety-critical lives only here: the cache can be dropped and rebuilt
(a permitted self-healing action) without changing any decision.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any


class InMemoryCache:
    name = "memory"

    def __init__(self) -> None:
        self._d: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            v = self._d.get(key)
            if v is None:
                return None
            if v[0] and v[0] < time.time():
                self._d.pop(key, None)
                return None
            return v[1]

    def set(self, key: str, value: Any, ttl: float = 0) -> None:
        with self._lock:
            self._d[key] = (time.time() + ttl if ttl else 0.0, value)

    def incr(self, key: str, ttl: float) -> int:
        with self._lock:
            exp, v = self._d.get(key, (0.0, 0))
            if exp and exp < time.time():
                v, exp = 0, 0.0
            v = int(v) + 1
            self._d[key] = (exp or time.time() + ttl, v)
            return v

    def flush(self) -> None:
        with self._lock:
            self._d.clear()

    def ping(self) -> bool:
        return True


class RedisCache:
    name = "redis"

    def __init__(self, url: str, client=None) -> None:
        import redis
        self.url = url
        self.r = client or redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0, decode_responses=True)

    def get(self, key: str) -> Any:
        v = self.r.get("amrt:" + key)
        return json.loads(v) if v is not None else None

    def set(self, key: str, value: Any, ttl: float = 0) -> None:
        if ttl:
            self.r.set("amrt:" + key, json.dumps(value, default=str), px=int(ttl * 1000))
        else:
            self.r.set("amrt:" + key, json.dumps(value, default=str))

    def incr(self, key: str, ttl: float) -> int:
        k = "amrt:" + key
        pipe = self.r.pipeline()
        pipe.incr(k)
        pipe.pexpire(k, int(ttl * 1000), nx=True)
        return int(pipe.execute()[0])

    def flush(self) -> None:
        for k in self.r.scan_iter("amrt:*"):
            self.r.delete(k)

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception:
            return False


class RateLimiter:
    def __init__(self, cache, limit: int, window_seconds: float) -> None:
        self.cache, self.limit, self.window = cache, limit, window_seconds

    def allow(self, key: str) -> bool:
        try:
            return self.cache.incr(f"rl:{key}:{int(time.time() // self.window)}", self.window) <= self.limit
        except Exception:
            return True  # a cache outage must not lock the owner out; failures are reported via health


def make_cache(url: str):
    if url:
        return RedisCache(url)
    return InMemoryCache()
