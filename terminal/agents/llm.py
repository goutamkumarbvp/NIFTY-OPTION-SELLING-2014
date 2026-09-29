"""Optional LLM reasoning gateway.

Disabled by default (``LLM_ENABLED=false``). When enabled with an Anthropic key
the NarratorAgent asks Claude for a concise trading desk briefing built from the
structured evidence the deterministic agents produced. The LLM never places
orders and never overrides a veto: it explains, it does not decide.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("terminal.llm")

SYSTEM_PROMPT = (
    "You are the narrator of an Indian options-selling trading desk (NSE, BSE, MCX). "
    "You receive structured findings from specialist agents (market analyst, volatility, options flow, "
    "event risk, sentinel, risk guardian, strategy selector). Write a crisp desk briefing for a professional "
    "trader: 1) one-line market read, 2) what the council decided and why, 3) key risks to watch, 4) if a trade "
    "is proposed, the exact structure, strikes, size and exit plan. Under 180 words. Never invent numbers that "
    "are not in the evidence. Do not give personal investment advice; this is decision support for the operator."
)


class LLMGateway:
    def __init__(self, settings) -> None:
        self.s = settings
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.llm_enabled and self.s.llm_api_key)

    def status(self) -> dict:
        return {"enabled": self.enabled, "provider": self.s.llm_provider, "model": self.s.llm_model, "calls": self.calls, "failures": self.failures, "last_error": self.last_error}

    async def brief(self, evidence: Dict[str, Any]) -> Optional[str]:
        if not self.enabled:
            return None
        self.calls += 1
        try:
            if self.s.llm_provider.lower() == "anthropic":
                return await asyncio.wait_for(self._anthropic(evidence), timeout=self.s.llm_timeout_seconds)
            return await asyncio.wait_for(self._openai_compatible(evidence), timeout=self.s.llm_timeout_seconds)
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("LLM brief failed: %s", self.last_error)
            return None

    async def _anthropic(self, evidence: Dict[str, Any]) -> str:
        import anthropic

        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.s.llm_api_key)
        try:
            response = await self._client.messages.create(
                model=self.s.llm_model,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Council evidence (JSON):\n" + json.dumps(evidence, default=str)}],
            )
        except anthropic.RateLimitError as exc:
            raise RuntimeError("rate limited") from exc
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"api error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise RuntimeError("connection error") from exc
        if response.stop_reason == "refusal":
            return "(narration unavailable: the model declined this request)"
        return "".join(block.text for block in response.content if block.type == "text").strip()

    async def _openai_compatible(self, evidence: Dict[str, Any]) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        async with httpx.AsyncClient(timeout=self.s.llm_timeout_seconds) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {self.s.llm_api_key}"},
                                  json={"model": self.s.llm_model, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(evidence, default=str)}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
