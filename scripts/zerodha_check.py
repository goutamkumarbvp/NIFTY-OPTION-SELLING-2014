#!/usr/bin/env python3
"""Zerodha Kite connectivity check (run after scripts/zerodha_login.py):

    python scripts/zerodha_check.py            # profile, tokens, expiries, option quotes, 15 s of ticks
    python scripts/zerodha_check.py --no-feed
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal.config import get_settings  # noqa: E402
from terminal.market.universe import Universe  # noqa: E402
from terminal.market.zerodha import ZerodhaSession, tick_from_kite  # noqa: E402


async def main() -> int:
    s = get_settings()
    session = ZerodhaSession(s, s.runtime_dir)
    missing = session.missing_credentials()
    if missing:
        print("✗ missing:", ", ".join(missing))
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
        atm = round((await session._call("ltp", f"NSE:{found['NIFTY']['trading_symbol']}") if "NIFTY" in found else {}).get(f"NSE:{found.get('NIFTY', {}).get('trading_symbol')}", {}).get("last_price", nifty.base_spot) / nifty.strike_step) * nifty.strike_step
        strikes = [atm + i * nifty.strike_step for i in range(-3, 4)]
        quotes = await session.option_quotes(nifty, exps[0], strikes)
        print(f"→ {len(quotes)} option quotes for {exps[0]} around {atm:.0f}; sample:", dict(list(quotes.items())[:2]))
    if "--no-feed" in sys.argv:
        return 0
    print("→ streaming ticks for 15 s …")
    ticker = session.ticker()
    tokens = [int(v["token"]) for v in found.values()]
    counter = {"n": 0}
    done = threading.Event()

    def on_connect(ws, resp):
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_ticks(ws, ticks):
        for t in ticks:
            f = tick_from_kite(t)
            if f:
                counter["n"] += 1
                if counter["n"] <= 8:
                    print(f"   token={f['token']:<8} ltp={f['ltp']:<12} chg={f['change_pct']:+.2f}%")

    ticker.on_connect, ticker.on_ticks = on_connect, on_ticks
    ticker.connect(threaded=True)
    t_end = time.time() + 15
    while time.time() < t_end:
        await asyncio.sleep(0.5)
    try:
        ticker.close()
    except Exception:
        pass
    print(f"✓ received {counter['n']} ticks. Now run:  DATA_SOURCE=zerodha python -m terminal")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
