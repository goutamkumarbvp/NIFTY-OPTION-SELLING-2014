"""HMAC signing used to bind authority to artefacts.

* The Risk Kernel signs every RiskDecision; the Execution Gateway verifies it.
* The Execution Gateway signs a SubmissionTicket; broker order clients verify it.

Each signer owns a random per-process key. A Verifier exposes only `verify`, so
holding a verifier never lets a component mint a signature.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from amrt.core.ids import canonical_json


class Signer:
    def __init__(self, name: str, key: bytes | None = None) -> None:
        self.name = name
        self.__key = key or os.urandom(32)

    def sign(self, payload: dict[str, Any]) -> str:
        return hmac.new(self.__key, canonical_json(payload).encode(), hashlib.sha256).hexdigest()

    def verifier(self) -> Verifier:
        return Verifier(self.name, self.__key)


class Verifier:
    def __init__(self, name: str, key: bytes) -> None:
        self.name = name
        self.__key = key

    def verify(self, payload: dict[str, Any], signature: str) -> bool:
        if not isinstance(signature, str) or not signature:
            return False
        expected = hmac.new(self.__key, canonical_json(payload).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
