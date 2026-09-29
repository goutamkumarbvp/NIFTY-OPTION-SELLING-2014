# NIFTY AI Options Terminal (V30)

An AI-agent driven options trading terminal for **NSE, BSE and MCX** with a
**MANUAL** mode (agents advise, you approve) and an **AUTO** mode (agents trade
inside a hard risk envelope). Built from the *AI Market Risk Terminal V3.5.2*
flow chart, re-designed around an **agent council** and a **safety-first
execution path**.

```
python -m pip install -r requirements.txt
python -m terminal            # → http://127.0.0.1:8600
```

Out of the box it runs on a realistic multi-market **simulator** with a paper
broker, so every panel, agent and risk control works without credentials.
Point `DATA_SOURCE=kotak` / `BROKER=kotak|zerodha` at real accounts when ready.

---

## What you get

| Layer | Highlights |
|---|---|
| **Web dashboard** | Overview, Option Chain (OI walls, greeks, PCR, candles), AI Council, Strategies (payoff preview, live SL/target bars), Approvals, Orders & Positions, Risk Control, Reports (performance + backtester), System (health, logs, hash-chained audit), Settings. Live via WebSocket. |
| **Agent council** (9 agents) | MarketAnalyst · VolatilityAgent · OptionsFlow · EventRisk · Sentinel · RiskGuardian · StrategySelector · ExecutionTactician · PostTradeReviewer, plus an optional LLM **narrator** (Claude) that explains decisions and never trades. |
| **Strategies** | Short straddle, delta strangle, iron condor, iron fly, bull-put / bear-call credit spreads, jade lizard. Entry, stop-loss, target, trailing lock, square-off, delta-based adjustments. |
| **Risk manager** | Pre-trade gates on every order (gate, kill switch, session, lots, positions, per-market lots, margin, daily loss, feed freshness, LIVE interlock) and a 1-second portfolio monitor that **halts and flattens** on breach. |
| **Execution** | Paper broker with bid/ask crossing, slippage and full Indian option charges; Kotak Neo and Zerodha Kite live adapters (fail closed without credentials). |
| **Markets** | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY (NSE) · SENSEX, BANKEX (BSE) · CRUDEOIL, NATURALGAS, GOLD, SILVER (MCX) with correct expiry weekdays and sessions. |
| **Ops** | SQLite persistence, tamper-evident audit trail, alerts to dashboard / Telegram / e-mail, health monitor, feed supervisor with auto-reconnect, role-based auth. |

## The two modes

| | MANUAL | AUTO |
|---|---|---|
| Council runs every cycle | ✔ | ✔ |
| Council entry decisions | become **proposals** in the Approvals panel (expire in 15 min) | **deployed immediately** (up to `AUTO_MAX_TRADES_PER_DAY`) |
| Manual order ticket / manual strategy deploy | ✔ | ✔ |
| Stop-loss / target / square-off exits | automatic | automatic |
| Adjustments (roll untested leg) | suggested as alert | executed |
| Sentinel protective flatten | if `PROTECT_IN_MANUAL=true` | ✔ |
| Hard limits (daily loss, margin, lots) → **HALT** | ✔ | ✔ |
| Kill switch | flattens everything, closes gate, forces MANUAL | same |

The **safety gate** must be opened explicitly before *any* entry is possible in
either mode. Exits are always allowed.

## LIVE trading interlock

Live orders require all three of `TRADING_ENV=LIVE`, `LIVE_TRADING=true` and
`LIVE_ORDERS_ENABLED=true` plus broker credentials. Anything less silently runs
the paper broker. There is no hidden "demo fill" on the live path: a missing
SDK or credential is a hard error.

## Configuration

Copy `.env.example` to `.env`. Every risk limit, schedule and agent weight is
also editable live from the dashboard and persisted in `runtime/terminal.sqlite3`.

Key variables:

- `TERMINAL_MODE`, `TRADING_ENV`, `DATA_SOURCE`, `BROKER`, `MARKETS`
- `CAPITAL`, `MAX_DAILY_LOSS`, `MAX_OPEN_LOTS`, `MAX_MARGIN_UTILISATION_PCT`, `PER_TRADE_STOP_LOSS_PCT`, `PER_TRADE_TARGET_PCT`, `TRAILING_LOCK_PCT`
- `AGENT_CYCLE_SECONDS`, `AUTO_MIN_CONSENSUS`, `AUTO_MIN_CONFIDENCE`
- `LLM_ENABLED`, `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` (optional narration)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SMTP_*` (optional alerts)
- `API_AUTH_TOKEN` or `TERMINAL_USERS=name:password:role,...` (roles: viewer, trader, admin) — required for non-loopback binds

## Running

```bash
scripts/start_terminal.sh          # Linux / macOS (creates .venv, installs, starts)
scripts\start_terminal.bat         # Windows
docker compose up --build          # container on :8600 with a persistent /data volume
python -m pytest -q                # test-suite (30 tests)
```

Interactive API docs: `http://127.0.0.1:8600/api/docs`.

## Testing the safety controls in simulation

Open *Risk Control → Simulation stress test*, inject a `-3%` shock on NIFTY or
`+30%` on INDIAVIX and watch the Sentinel veto / protect, or lower *max daily
loss* and see the risk manager halt and flatten. The audit trail records every
step.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design, data
flow and extension points. The previous V27.5 codebase is preserved in
`legacy/` for reference.

## Disclaimer

Trading options involves substantial risk. This software is decision support
and automation tooling; it is not investment advice. Test thoroughly in PAPER
mode before enabling LIVE.
