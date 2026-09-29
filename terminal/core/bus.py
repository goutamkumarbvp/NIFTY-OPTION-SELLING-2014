"""Asynchronous in-process event bus with topic subscriptions and history."""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

log = logging.getLogger("terminal.bus")

Handler = Callable[[str, Any], Awaitable[None]]


class EventBus:
    def __init__(self, history: int = 500) -> None:
        self._subs: Dict[str, List[Handler]] = collections.defaultdict(list)
        self._wildcard: List[Handler] = []
        self._history: Deque[dict] = collections.deque(maxlen=history)

    def subscribe(self, topic: str, handler: Handler) -> None:
        if topic == "*":
            self._wildcard.append(handler)
        else:
            self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        if topic == "*":
            if handler in self._wildcard:
                self._wildcard.remove(handler)
        elif handler in self._subs.get(topic, []):
            self._subs[topic].remove(handler)

    async def publish(self, topic: str, payload: Any = None) -> None:
        self._history.append({"ts": time.time(), "topic": topic, "payload": _jsonable(payload)})
        handlers = list(self._subs.get(topic, [])) + list(self._wildcard)
        if not handlers:
            return
        results = await asyncio.gather(*(h(topic, payload) for h in handlers), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.exception("event handler failed for %s: %s", topic, r)

    def publish_nowait(self, topic: str, payload: Any = None) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish(topic, payload))

    def history(self, topic_prefix: str = "", limit: int = 100) -> List[dict]:
        rows = [h for h in self._history if h["topic"].startswith(topic_prefix)]
        return rows[-limit:]


def _jsonable(payload: Any) -> Any:
    if payload is None:
        return None
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, dict):
        return {k: _jsonable(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(v) for v in payload]
    return payload
