"""Secret redaction for logs, events, API payloads and LLM prompts.

Two layers: (1) any mapping key that looks like a secret is masked, (2) every
registered secret *value* (broker keys, tokens, passwords loaded at startup) is
replaced wherever it appears inside strings.
"""
from __future__ import annotations

import logging
import re
from typing import Any

SECRET_KEY_RE = re.compile(r"(pass(word)?|secret|token|api[_-]?key|consumer[_-]?key|mpin|\bpin\b|totp|authorization|cookie|session|private[_-]?key|access[_-]?code|otp)", re.I)
MASK = "***REDACTED***"


class Redactor:
    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, *values: str | None) -> None:
        for v in values:
            if v and isinstance(v, str) and len(v) >= 4:
                self._values.add(v)

    def text(self, s: str) -> str:
        out = s
        for v in sorted(self._values, key=len, reverse=True):
            if v in out:
                out = out.replace(v, MASK)
        return out

    def value(self, obj: Any, key: str | None = None) -> Any:
        if key is not None and SECRET_KEY_RE.search(str(key)) and obj not in (None, "", False):
            if not isinstance(obj, (dict, list)):
                return MASK
        if isinstance(obj, dict):
            return {k: self.value(v, k) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.value(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj


REDACTOR = Redactor()


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = REDACTOR.text(msg)
            if red != msg:
                record.msg, record.args = red, ()
        except Exception:
            pass
        return True
