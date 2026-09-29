#!/usr/bin/env python3
"""Kotak Neo connectivity check. Run locally where your .env lives:

    python scripts/kotak_check.py            # login, discover tokens, sample chain, 15 s of ticks
    python scripts/kotak_check.py --no-feed  # skip the WebSocket part

Nothing is written except runtime/kotak_tokens.json. Secrets are never printed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal.config import get_settings  # noqa: E402
from terminal.core.models import Exchange  # noqa: E402
from terminal.market.kotak import KotakNeoSession, rows_of  # noqa: E402
from terminal.market.universe import Universe  # noqa: E402


async def main() -> int:
    s = get_settings()
    session = KotakNeoSession(s, s.runtime_dir)
    missing = session.missing_credentials()
    if missing:
        print("✗ missing in .env:", ", ".join(missing))
        return 2
    print("→ logging in (TOTP + MPIN) …")
    try:
        print("✓ login:", await session.connect())
    except Exception as exc:
        print("✗ login failed:", exc)
        return 1
    universe = Universe(s.runtime_dir, s.market_list)
    print("→ resolving instrument tokens …")
    found = await session.resolve_index_tokens(universe.all(), include_vix=True)
    for sym, v in found.items():
        print(f"   {sym:<12} token={v['token']:<12} seg={v['segment']:<8} {v.get('trading_symbol') or v.get('name')}")
    missing_syms = [u.symbol for u in universe.all() if u.symbol not in found]
    if missing_syms:
        print("   ⚠ not resolved:", ", ".join(missing_syms), "(edit INDEX_NAMES in terminal/market/kotak.py if Kotak names differ)")
    nifty = universe.get("NIFTY") if universe.has("NIFTY") else universe.all()[0]
    print(f"→ expiries for {nifty.symbol} …")
    exps = await session.expiries(nifty)
    print("  ", dict(list(exps.items())[:5]) or "none parsed")
    if exps:
        first = sorted(exps)[0]
        print(f"→ option chain {nifty.symbol} {first} …")
        try:
            raw = await session._call("option_chain", exchange="NSE" if nifty.exchange == Exchange.NSE else nifty.exchange.value, underlying=nifty.symbol, expiry=exps[first])
            rows = rows_of(raw)
            print(f"   raw type={type(raw).__name__} rows={len(rows)} sample={json.dumps(rows[:2], default=str)[:600]}")
            parsed = await session.option_chain(nifty, first)
            print(f"   parsed {len(parsed)} quotes; sample:", dict(list(parsed.items())[:2]))
            if not parsed:
                print("   ⚠ parser could not read this layout — paste the 'sample' line above to the developer.")
        except Exception as exc:
            print("   ✗ option chain failed:", exc)
    if "--no-feed" in sys.argv:
        return 0
    print("→ streaming ticks for 15 s …")
    from neo_api_client.websocket.feed import WsToken  # type: ignore
    from terminal.market.kotak import tick_from_message

    count, t_end = 0, time.time() + 15
    try:
        async with session.websocket() as ws:
            await ws.subscribe_scrips([WsToken(v["segment"], str(v["token"])) for v in found.values()])
            async for msg in ws:
                f = tick_from_message(msg)
                if f:
                    count += 1
                    if count <= 8:
                        print(f"   {f['kind']:<14} token={f['token']:<10} ltp={f['ltp']:<12} chg={f['change_pct']:+.2f}%")
                if time.time() > t_end:
                    break
    except Exception as exc:
        print("✗ feed error:", exc)
        return 1
    print(f"✓ received {count} ticks. You can now run:  DATA_SOURCE=kotak python -m terminal")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
