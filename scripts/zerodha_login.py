#!/usr/bin/env python3
"""Daily Zerodha Kite login. Run every trading morning where your .env lives:

    python scripts/zerodha_login.py                 # prints the login URL, asks for the request_token
    python scripts/zerodha_login.py <request_token> # non-interactive

Exchanges the request_token for today's access_token, stores it in
runtime/zerodha_session.json (the terminal picks it up automatically) and
updates ZERODHA_ACCESS_TOKEN in .env. Needs ZERODHA_API_KEY + ZERODHA_API_SECRET.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from terminal.config import get_settings  # noqa: E402
from terminal.market.zerodha import ZerodhaSession  # noqa: E402


def _update_env(token: str) -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    text = env.read_text(encoding="utf-8")
    if re.search(r"^ZERODHA_ACCESS_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^ZERODHA_ACCESS_TOKEN=.*$", f"ZERODHA_ACCESS_TOKEN={token}", text, flags=re.M)
    else:
        text += f"\nZERODHA_ACCESS_TOKEN={token}\n"
    env.write_text(text, encoding="utf-8")


async def main() -> int:
    s = get_settings()
    if not s.zerodha_api_key or not s.zerodha_api_secret:
        print("✗ set ZERODHA_API_KEY and ZERODHA_API_SECRET in .env first")
        return 2
    session = ZerodhaSession(s, s.runtime_dir)
    request_token = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not request_token:
        print("1. Open this URL, log in to Kite and authorise the app:\n   " + session.login_url())
        print("2. You are redirected to your app's redirect URL; copy the request_token=... value from it.")
        request_token = input("request_token: ").strip()
    m = re.search(r"request_token=([A-Za-z0-9]+)", request_token)
    if m:
        request_token = m.group(1)
    try:
        result = await session.exchange_request_token(request_token)
    except Exception as exc:
        print("✗", exc)
        return 1
    _update_env(session.access_token)
    print(f"✓ session created for {result.get('user_id')} — token saved to runtime/zerodha_session.json and .env")
    print("  now run:  DATA_SOURCE=zerodha python -m terminal   (or python scripts/zerodha_check.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
