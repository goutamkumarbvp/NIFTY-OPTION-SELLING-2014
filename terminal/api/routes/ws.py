from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from terminal.api.auth import ws_user

router = APIRouter()


@router.websocket("/ws")
async def websocket(ws: WebSocket):
    user = ws_user(ws)
    if user is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    t = ws.app.state.terminal
    t.ws_clients.add(ws)
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    async def _fan(topic: str, payload: Any) -> None:
        try:
            queue.put_nowait({"type": "event", "topic": topic, "payload": payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload})
        except asyncio.QueueFull:
            pass

    t.bus.subscribe("*", _fan)
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": t.snapshot()}, default=str))

        async def _pump():
            while True:
                item = await queue.get()
                await ws.send_text(json.dumps(item, default=str))

        pump = asyncio.create_task(_pump())
        try:
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
        finally:
            pump.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        t.bus.unsubscribe("*", _fan)
        t.ws_clients.discard(ws)
