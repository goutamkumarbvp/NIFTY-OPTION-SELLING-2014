import asyncio
import os
import tempfile

import pytest
import pytest_asyncio

from terminal.config import Settings, reset_settings_cache


def make_settings(**overrides) -> Settings:
    base = dict(RUNTIME_DIR=tempfile.mkdtemp(prefix="aiterm-"), SAFETY_GATE_OPEN_ON_START="true", TICK_INTERVAL_SECONDS="0.2", AGENT_CYCLE_SECONDS="60",
                TERMINAL_MODE="MANUAL", TRADING_ENV="PAPER", LLM_ENABLED="false", TERMINAL_START_TIME="00:00", TERMINAL_END_TIME="23:59",
                EXIT_RETRY_SECONDS="0.5")
    base.update({k: str(v) for k, v in overrides.items()})
    return Settings(_env_file=None, **base)


@pytest_asyncio.fixture
async def terminal(request):
    from terminal.app import Terminal

    overrides = getattr(request, "param", {}) or {}
    t = Terminal(settings=make_settings(**overrides), seed=7)
    await t.start()
    # wait for first ticks and chains
    for _ in range(50):
        await asyncio.sleep(0.1)
        if t.chains:
            break
    await t.risk.evaluate()
    yield t
    await t.stop()


@pytest.fixture
def settings():
    return make_settings()
