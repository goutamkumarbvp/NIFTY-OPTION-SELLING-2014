"""Shared FastAPI dependencies and helpers for the route modules."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from terminal.api.auth import current_user, require  # noqa: F401  (re-exported for routes)
from terminal.app import Terminal


def terminal(request: Request) -> Terminal:
    return request.app.state.terminal


def ok(data: Any = None, **extra: Any) -> dict:
    return {"ok": True, "data": data, **extra}
