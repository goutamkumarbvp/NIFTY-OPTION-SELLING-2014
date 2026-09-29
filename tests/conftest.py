import asyncio
import tempfile

import pytest
import pytest_asyncio

from terminal.config import Settings


def make_settings(**overrides) -> Settings:
    base = dict(RUNTIME_DIR=tempfile.mkdtemp(prefix="aiterm-"), SAFETY_GATE_OPEN_ON_START="true", AGENT_CYCLE_SECONDS="60", DATA_SOURCE="kotak",
                TERMINAL_MODE="MANUAL", TRADING_ENV="PAPER", LLM_ENABLED="false", TERMINAL_START_TIME="00:00", TERMINAL_END_TIME="23:59",
                EXIT_RETRY_SECONDS="0.5", NEO_CONSUMER_KEY="test", NEO_MOBILE_NUMBER="+910000000000", NEO_UCC="TEST1", NEO_MPIN="000000", NEO_TOTP_SECRET="JBSWY3DPEHPK3PXP")
    base.update({k: str(v) for k, v in overrides.items()})
    return Settings(_env_file=None, **base)


def make_terminal(settings: Settings, seed: int = 7):
    """A Terminal wired to the scripted test feed (the product itself is live-only)."""
    from terminal.app import Terminal
    from terminal.market.universe import Universe
    from tests.fakefeed import ScriptedFeed

    universe = Universe(settings.runtime_dir, settings.market_list)
    feed = ScriptedFeed(universe.all(), interval=0.2, seed=seed)
    t = Terminal(settings=settings, feed=feed)
    feed.terminal = t
    t.scheduler.assume_open = True
    return t


async def wait_live(t, timeout: float = 8.0) -> None:
    """Wait until every chain in focus has live quotes on its rows."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
        if t.chains and all(ch.live_rows >= len(ch.rows) * 2 * 0.9 for ch in t.chains.values()):
            return
    raise AssertionError("scripted feed did not populate live quotes in time")


@pytest_asyncio.fixture
async def terminal(request):
    overrides = getattr(request, "param", {}) or {}
    t = make_terminal(make_settings(**overrides))
    await t.start()
    await wait_live(t)
    await t.risk.evaluate()
    yield t
    await t.stop()


@pytest.fixture
def settings():
    return make_settings()
