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

The terminal is **live-data only**: it needs one broker feed (Kotak Neo by
default, or Zerodha / Angel One) and consumes every tick the broker pushes at
the broker's own resolution (Kotak SFeed updates are sub-second; nothing is
sampled or simulated). Fills are simulated on the paper broker until the LIVE
interlocks are switched on.

---

## What you get

| Layer | Highlights |
|---|---|
| **Web dashboard** | Overview, Option Chain (OI walls, greeks, PCR, candles), AI Council, Strategies (payoff preview, live SL/target bars), Approvals, Orders & Positions, Risk Control, Reports (performance + backtester), System (health, logs, hash-chained audit), Settings. Live via WebSocket. |
| **Agent council** (9 agents) | MarketAnalyst · VolatilityAgent · OptionsFlow · EventRisk · Sentinel · RiskGuardian · StrategySelector · ExecutionTactician · PostTradeReviewer, plus an optional LLM **narrator** (Claude) that explains decisions and never trades. |
| **Strategies** | Short straddle, delta strangle, iron condor, iron fly, bull-put / bear-call credit spreads, jade lizard. Entry, stop-loss, target, trailing lock, square-off, delta-based adjustments. |
| **Risk manager** | Pre-trade gates on every order (gate, kill switch, session, lots, positions, per-market lots, margin, daily loss, feed freshness, LIVE interlock) and a 1-second portfolio monitor that **halts and flattens** on breach. |
| **Execution** | Paper broker with bid/ask crossing, slippage and full Indian option charges; Kotak Neo, Zerodha Kite and Angel One SmartAPI live data + order adapters (fail closed without credentials). |
| **Markets** | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY (NSE) · SENSEX, BANKEX (BSE) · CRUDEOIL, NATURALGAS, GOLD, SILVER (MCX) with correct expiry weekdays and sessions. |
| **Tick resolution** | Every broker update is processed as it arrives; stop-loss / target checks run on each option tick (throttled to `TICK_EVAL_MIN_INTERVAL_MS`), indicators use `CANDLE_SECONDS` bars and 1-second bars are available on demand; the dashboard shows the measured tick interval. |
| **Guardian agent** (self-healing) | Watches every internal loop's heartbeat, the feed, the broker session and API error rate, book reconciliation, stuck exits, event-loop lag, error bursts and host resources. Turns a symptom into an incident with a diagnosis (plus an optional Claude root-cause note) and a concrete remedy — reconnect feed, re-login session, restart loop, adopt broker book, pause entries, prune history, flatten — and **asks you for permission** before applying it (dashboard Approvals or Telegram `/heal approve <id>`). Remedies that do not clear the symptom escalate; incidents that clear on their own close as self-recovered. `GUARDIAN_AUTO_APPLY=low|medium|all` can pre-approve by risk level; flatten always needs a human. |
| **Ops** | SQLite persistence, tamper-evident audit trail, alerts to dashboard / Telegram / e-mail, health monitor, feed supervisor with auto-reconnect, role-based auth. |

## What the AI layer does (V30.1)

- **Copilot** (Copilot panel, `/api/copilot/chat`): a Claude tool-use agent over the terminal's own state (overview, chain, indicators, risk, positions, council, journal, backtests). Its only write tool *proposes* a plan into Approvals; it cannot place orders, change mode or edit limits. Without an LLM key it answers deterministically from the same tools. Enable with `LLM_ENABLED=true`, `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-opus-5-5`, `LLM_API_KEY=...`.
- **Council evaluation** (Journal & Learning panel): every decision is journaled with the features it saw and scored after `DECISION_SCORE_MINUTES` (30) against the underlying's forward move relative to its expected move. The scorecard shows each agent's hit-rate, so weight changes are measured, not guessed.
- **Learned entry-quality model**: an online logistic regression trained on scored decisions; the `LearnedModel` agent votes with P(next window is seller-friendly), with confidence that grows with samples. Weights are visible on the dashboard.
- **Adaptive strategy selection**: the StrategySelector Thompson-samples each structure's win-rate posterior per regime (from the PostTradeReviewer's statistics), favouring proven structures while still exploring.
- **Daily journal**: written at the end of the operating window (P&L, runs, council mix, alerts, lessons, narrator brief) with "similar past days" retrieval.
- **Sentinel order-flow anomalies**: OI unwinds, PCR flips, bid-ask blowouts at ATM (veto) and cross-market divergence, in addition to sigma moves and VIX spikes.

## Live-readiness (Phase A)

- **Order reconciliation**: every working live order is polled and fills (full or partial), cancellations and rejections are applied to the book, so the exit guard always sees the truth before retrying.
- **Position & margin reconciliation**: the broker's book and margin are compared every `RECONCILE_SECONDS`; mismatches alert (adopt from the System panel / `POST /api/broker/reconcile?adopt=true`); the broker's used margin replaces the SPAN estimate while fresh.
- **Restart recovery**: positions, today's working orders and active/exiting runs are restored on start; exiting runs resume through the exit guard.
- **Option-quote streaming**: held and near-ATM contracts are subscribed on the broker WebSocket (Kotak SFeed, KiteTicker, SmartWebSocketV2) so stop-losses act on ticks, not polls.
- **Preflight**: the startup banner prints exactly which interlocks and providers are active and refuses inconsistent configs. Profiles: `.env.paper`, `.env.live`.

## Operations (Phase D)

- **Telegram commands** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`): `/status /pnl /positions /runs /pause /resume /kill confirm /gate open|close /mode manual|auto /ask <question>`; only the configured chat id is honoured.
- **Metrics**: Prometheus text at `/metrics`. **Logs**: `LOG_FORMAT=json` for structured lines. **Deploy**: `deploy/terminal.service` (systemd), `docker compose up --build`. **CI**: GitHub Actions runs ruff + pytest on every push.
- **Release**: `python scripts/build_release.py` builds the distributable zip.

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

## Connecting your Kotak Neo account (live market data)

1. Install the broker SDK (kept optional so the simulator needs nothing):
   ```bash
   python -m pip install -r requirements-kotak.txt
   ```
2. In the Kotak Neo API portal create an app and copy its **consumer key**.
   Enable TOTP in the Neo app and note the **TOTP secret** (the base32 key
   shown when you set up the authenticator), your **UCC** (client code),
   registered **mobile number** with country code and your **MPIN**.
3. Put them in `.env` (never in chat, never in git):
   ```text
   DATA_SOURCE=kotak
   NEO_CONSUMER_KEY=...
   NEO_MOBILE_NUMBER=+91XXXXXXXXXX
   NEO_UCC=...
   NEO_MPIN=...
   NEO_TOTP_SECRET=...
   ```
4. Verify the connection before starting the terminal:
   ```bash
   python scripts/kotak_check.py
   ```
   It logs in (TOTP + MPIN), resolves the index / futures tokens for every
   underlying, fetches a sample option chain and streams ticks for 15 s. If
   an instrument is not resolved or the chain layout is not parsed, the
   script prints the raw sample so the mapping in `terminal/market/kotak.py`
   can be adjusted.
5. Start: `python -m terminal`. The *System* panel shows session state, the
   resolved instruments, API call counts and live option-quote polls, with a
   **Reconnect** button. Sessions expire daily; the feed supervisor re-logs
   in automatically.

What flows from Kotak: index / futures ticks over the SFeed WebSocket (NIFTY,
BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX, INDIAVIX; MCX underlyings as
the nearest future) and real option quotes (LTP, OI, volume, IV) polled from
`option_chain` and overlaid on the model chain, so strikes, greeks, PCR and
max-pain are computed from live prices. Trading stays on the **paper broker**
until you also set `TRADING_ENV=LIVE`, `LIVE_TRADING=true`,
`LIVE_ORDERS_ENABLED=true` and `BROKER=kotak`.

## Connecting your Zerodha account (second broker option)

Kite Connect issues a fresh access token every trading day through a browser
login, so the flow is: configure once, log in each morning.

1. `python -m pip install -r requirements-zerodha.txt`
2. Create a Kite Connect app at developers.kite.trade and put its key and
   secret in `.env`:
   ```text
   DATA_SOURCE=zerodha
   ZERODHA_API_KEY=...
   ZERODHA_API_SECRET=...
   ```
3. Each morning run `python scripts/zerodha_login.py`. It prints the Kite
   login URL; after you log in, paste the `request_token` from the redirect
   URL. The script exchanges it for today's access token and stores it in
   `runtime/zerodha_session.json` and `.env`. The same login is available in
   the terminal's *System* panel (**Open Kite login** → paste token →
   **Create session**).
4. `python scripts/zerodha_check.py` verifies the profile, instrument
   resolution, option quotes and 15 s of ticks; then `python -m terminal`.

What flows from Zerodha: index / futures ticks over KiteTicker (full mode)
and option quotes (LTP, OI, volume, bid/ask) for the strikes in the active
chain, resolved from the daily instrument dump. Live orders use
`BROKER=zerodha` behind the same triple interlock. `DATA_SOURCE` and `BROKER`
must name the same provider; one live session is shared by feed, quotes and
orders.

## Connecting your Angel One account (third broker option)

1. `python -m pip install -r requirements-angel.txt`
2. Create an app at smartapi.angelbroking.com (Trading API) and enable TOTP
   in the Angel One app. Put these in `.env`:
   ```text
   DATA_SOURCE=angel
   ANGEL_API_KEY=...
   ANGEL_CLIENT_CODE=...
   ANGEL_PIN=...          # trading PIN
   ANGEL_TOTP_SECRET=...
   ```
3. `python scripts/angel_check.py` logs in (client code + PIN + TOTP), loads
   the public instrument master, resolves indices / MCX futures, fetches
   sample option quotes and streams ticks for 15 s. Then `python -m terminal`.

What flows from Angel One: index / futures ticks over SmartWebSocketV2
(SNAP_QUOTE mode) and option quotes (LTP, OI, volume, bid/ask) via
`getMarketData` in batches of 50 tokens for the strikes in the active chain.
Live orders use `BROKER=angel` (product CARRYFORWARD) behind the same triple
interlock. No daily browser login is needed; the session re-authenticates with
TOTP automatically.

## Running

```bash
scripts/start_terminal.sh          # Linux / macOS (creates .venv, installs, starts)
scripts\start_terminal.bat         # Windows
docker compose up --build          # container on :8600 with a persistent /data volume
python -m pytest -q                # test-suite
```

Interactive API docs: `http://127.0.0.1:8600/api/docs`.

## Testing the safety controls

The test suite drives the whole terminal through a scripted feed (`tests/fakefeed.py`)
and a fake live broker: stop-loss layers, retries, halts, kill switch, reconciliation
and end-of-day square-off are all exercised without a market. Run `python -m pytest -q`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design, data
flow and extension points. The previous V27.5 codebase is preserved in
`legacy/` for reference.

## Disclaimer

Trading options involves substantial risk. This software is decision support
and automation tooling; it is not investment advice. Test thoroughly in PAPER
mode before enabling LIVE.
