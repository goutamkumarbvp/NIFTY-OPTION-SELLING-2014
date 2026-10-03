"""Identifiers, canonical JSON and hashing used for idempotency keys, intent hashes and the event chain."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def idempotency_key(*parts: Any) -> str:
    """Stable key for one logical order: identical inputs always map to the same key."""
    return "IK" + sha256_hex(canonical_json(list(parts)))[:30]


def broker_tag(key: str, max_len: int = 20) -> str:
    """Alphanumeric client tag derived from an idempotency key (brokers cap tag length)."""
    clean = "".join(ch for ch in key if ch.isalnum()).upper()
    return clean[:max_len]
