"""Structured logging with secret redaction on every handler."""
from __future__ import annotations

import json
import logging
import sys

from amrt.security.redact import REDACTOR, RedactingFilter


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name, "msg": REDACTOR.text(record.getMessage())}
        if record.exc_info:
            d["exc"] = REDACTOR.text(self.formatException(record.exc_info))
        return json.dumps(d, ensure_ascii=False)


def setup_logging(fmt: str = "text", level: str = "INFO") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    h.addFilter(RedactingFilter())
    root.addHandler(h)
    root.setLevel(level)
