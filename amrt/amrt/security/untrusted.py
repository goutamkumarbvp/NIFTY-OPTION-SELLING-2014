"""Untrusted external text (news, documents, broker messages, web pages).

Untrusted text is data, never instructions. It is normalised, length-capped and
scanned for instruction-like content; the flags are recorded but the text is
never interpreted. When it reaches an LLM it is passed inside a JSON data block
with an explicit system instruction that data blocks carry no authority.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)",
    r"disregard (the )?(system|previous) (prompt|instructions)",
    r"you are now", r"act as (an? )?(admin|owner|developer)", r"system prompt",
    r"(place|submit|execute) (an? )?(buy|sell|market) order", r"(increase|raise|disable|turn off) (the )?(limit|kill ?switch|risk)",
    r"switch (to )?(automatic|live) mode", r"<\s*/?\s*(system|instructions?)\s*>", r"BEGIN (SYSTEM|INSTRUCTIONS)",
]
_RX = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


@dataclass(frozen=True)
class UntrustedText:
    text: str
    source: str
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def suspicious(self) -> bool:
        return bool(self.flags)


def sanitize(raw: str, source: str, max_len: int = 2000) -> UntrustedText:
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    s = s[:max_len]
    flags = tuple(p.pattern for p in _RX if p.search(s))
    return UntrustedText(text=s, source=source, flags=flags)
