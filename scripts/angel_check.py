#!/usr/bin/env python3
"""Angel One SmartAPI connectivity check (run where your .env lives):

    python scripts/angel_check.py            # login, tokens, expiries, option quotes, 15 s of ticks
    python scripts/angel_check.py --no-feed
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal.config import get_settings  # noqa: E402
from terminal.market.angel import WS_EXCHANGE_TYPE, AngelOneSession, tick_from_smart  # noqa: E402
from terminal.market.universe import Universe  # noqa: E402


async def main() -> int:
    s = get_settings()
    session = AngelOneSession(s, s.runtime_dir)
    missing = session.missing_credentials()
    if missing:
        print("✗ missing in .env:", ", ".join(missing))
        return 2
    try:
        print("✓ login:", await session.connect())
    except Exception as exc:
        print("✗", exc)
        return 1
    universe = Universe(s.runtime_dir, s.market_list)
    found = await session.resolve_index_tokens(universe.all(), include_vix=True)
    for sym, v in found.items():
        print(f"   {sym:<12} token={v['token']:<10} {v['exchange']:<4} {v['trading_symbol']}")
    nifty = universe.get("NIFTY") if universe.has("NIFTY") else universe.all()[0]
    exps = await session.expiries(nifty)
    print(f"→ {nifty.symbol} expiries:", exps[:5])
    if exps:
        atm = round(nifty.base_spot / nifty.strike_step) * nifty.strike_step
        strikes = [atm + i * nifty.strike_step for i in range(-3, 4)]
        quotes = await session.option_quotes(nifty, exps[0], strikes)
        print(f"→ {len(quotes)} option quotes for {exps[0]} around {atm:.0f}; sample:", dict(list(quotes.items())[:2]))
        if not quotes:
            print("   ⚠ no quotes returned (market closed, or strike range far from spot — adjust base_spot in universe.json)")
    if "--no-feed" in sys.argv:
        return 0
    print("→ streaming ticks for 15 s …")
    ws = session.websocket()
    groups = {}
    for v in found.values():
        groups.setdefault(WS_EXCHANGE_TYPE.get(v["exchange"], 1), []).append(str(v["token"]))
    token_list = [{"exchangeType": k, "tokens": t} for k, t in groups.items()]
    counter = {"n": 0}

    def on_open(wsapp):
        ws.subscribe("check", ws.SNAP_QUOTE, token_list)

    def on_data(wsapp, message):
        f = tick_from_smart(message) if isinstance(message, dict) else None
        if f:
            counter["n"] += 1
            if counter["n"] <= 8:
                print(f"   token={f['token']:<10} ltp={f['ltp']:<12} chg={f['change_pct']:+.2f}%")

    ws.on_open, ws.on_data, ws.on_error, ws.on_close = on_open, on_data, (lambda *a: print("   ws error", a[-1] if a else "")), (lambda *a: None)
    threading.Thread(target=ws.connect, daemon=True).start()
    t_end = time.time() + 15
    while time.time() < t_end:
        await asyncio.sleep(0.5)
    try:
        ws.close_connection()
    except Exception:
        pass
    print(f"✓ received {counter['n']} ticks. Now run:  DATA_SOURCE=angel python -m terminal")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
