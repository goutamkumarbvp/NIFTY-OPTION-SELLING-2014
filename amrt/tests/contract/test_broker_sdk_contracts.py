"""Broker contract tests: every SDK call our adapters make must bind to the installed official SDK's real signature.

The SDK client is replaced by a recording mock (no network); each recorded call is then bound against
inspect.signature of the real SDK method. Live behaviour (responses, auth, rate limits) is NOT verified
here — that needs broker access, which this environment does not have (see docs/BROKER_CAPABILITY_MATRIX.md).
"""
import asyncio
import importlib
import inspect
from unittest.mock import MagicMock

import pytest
from harness import env

from amrt.config import Settings
from amrt.core.clock import ManualClock
from amrt.core.enums import OrderType, Side
from amrt.execution.intents import BrokerOrderRequest

pytestmark = pytest.mark.contract

CASES = {
    "kotak": ("amrt.brokers.kotak", "KotakSession", "neo_api_client", "NeoAPI"),
    "zerodha": ("amrt.brokers.zerodha", "ZerodhaSession", "kiteconnect", "KiteConnect"),
    "angel": ("amrt.brokers.angel", "AngelSession", "SmartApi", "SmartConnect"),
    "groww": ("amrt.brokers.groww", "GrowwSession", "growwapi", "GrowwAPI"),
}
UPSTOX_GROUPS = {"orders": "OrderApi", "portfolio": "PortfolioApi", "user": "UserApi", "quotes": "MarketQuoteApi", "options": "OptionsApi"}
CREDS = dict(NEO_CONSUMER_KEY="ck", NEO_MOBILE_NUMBER="+910000000000", NEO_UCC="U", NEO_MPIN="1234", NEO_TOTP_SECRET="JBSWY3DPEHPK3PXP",
             ZERODHA_API_KEY="k", ZERODHA_API_SECRET="s", ZERODHA_ACCESS_TOKEN="t", ANGEL_API_KEY="k", ANGEL_CLIENT_CODE="c", ANGEL_PIN="1234",
             ANGEL_TOTP_SECRET="JBSWY3DPEHPK3PXP", UPSTOX_ACCESS_TOKEN="t", GROWW_ACCESS_TOKEN="t")


def req(broker: str) -> BrokerOrderRequest:
    ref = {"trading_symbol": "NIFTY26OCT25000CE", "token": "12345", "segment": "nse_fo", "instrument_key": "NSE_FO|12345", "exchange": "NFO"}
    return BrokerOrderRequest(account_id="A", instrument_key="NSE:NIFTY:2026-10-06:25000:CE", trading_symbol="NIFTY26OCT25000CE", broker_ref=ref,
                              exchange="NSE", segment="NSE_FO", side=Side.BUY, quantity=65, order_type=OrderType.LIMIT, limit_price=101.5,
                              product="NRML", validity="DAY", client_tag="AMRTabc123")


async def exercise(sess, broker):
    await sess.login()
    for coro in (lambda: sess._place(req(broker)), lambda: sess._cancel("OID1"), sess.fetch_order_book, sess.fetch_positions, sess.fetch_funds,
                 lambda: sess.fetch_expiries("NIFTY")):
        try:
            await coro()
        except TypeError as e:
            if "argument" in str(e):
                raise
        except Exception:
            pass


def check_calls(mock, cls, skip=()):
    bad = []
    for name, args, kwargs in mock.mock_calls:
        top = name.split(".")[0]
        if not top or "." in name or top in skip:
            continue
        real = getattr(cls, top, None)
        if real is None:
            bad.append(f"{cls.__name__}.{top} does not exist")
            continue
        try:
            inspect.signature(real).bind(None, *args, **kwargs)
        except TypeError as e:
            bad.append(f"{cls.__name__}.{top}{args}{kwargs}: {e}")
    return bad


@pytest.mark.parametrize("broker", sorted(CASES))
def test_sdk_calls_bind_to_real_signatures(broker, monkeypatch, tmp_path):
    mod_name, sess_name, sdk_mod, sdk_cls = CASES[broker]
    sdk = pytest.importorskip(sdk_mod)
    cls = getattr(sdk, sdk_cls)
    env(monkeypatch, tmp_path, **CREDS)
    mock = MagicMock()
    mock.placeOrder.return_value = "OID1"
    sess = getattr(importlib.import_module(mod_name), sess_name)(Settings(), ManualClock(), sdk_factory=lambda: mock)
    if broker == "angel":
        sess._master_loader = lambda: []
    asyncio.run(exercise(sess, broker))
    assert mock.mock_calls, "adapter made no SDK calls"
    assert check_calls(mock, cls) == []


def test_upstox_calls_bind_to_real_signatures(monkeypatch, tmp_path):
    up = pytest.importorskip("upstox_client")
    from amrt.brokers.upstox import UpstoxSession, order_body_kwargs
    up.PlaceOrderRequest(**order_body_kwargs(req("upstox")))                      # field names validated by the real model
    env(monkeypatch, tmp_path, **CREDS)
    apis = type("Apis", (), {})()
    for g in UPSTOX_GROUPS:
        setattr(apis, g, MagicMock())
    sess = UpstoxSession(Settings(), ManualClock(), sdk_factory=lambda: apis)
    asyncio.run(exercise(sess, "upstox"))
    bad = []
    for g, cname in UPSTOX_GROUPS.items():
        bad += check_calls(getattr(apis, g), getattr(up, cname))
    assert bad == []
