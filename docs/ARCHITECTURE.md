# Architecture

```
┌────────────────────────── Web Dashboard (terminal/ui, no build step) ──────────────────────────┐
│ Overview │ Option Chain │ AI Council │ Strategies │ Approvals │ Orders & Positions │ Risk │ … │
└───────────────────────────────▲──────────────────────────────────────▲───────────────────────┘
                     REST (FastAPI)                            WebSocket /ws (1 s snapshots + events)
┌───────────────────────────────┴──────────────────────────────────────┴───────────────────────┐
│ terminal/api  ─ auth (roles) ─ request validation ─ routing                                   │
└───────────────────────────────▲──────────────────────────────────────────────────────────────┘
┌───────────────────────────────┴───────────── Terminal (terminal/app.py, composition root) ─────┐
│ Market feed ─▶ MarketDataProcessor ─▶ OptionChainBuilder ─▶ quotes                             │
│      │              (candles, indicators, PCR/OI history, VIX rank, σ-move)                   │
│      ▼                                                                                          │
│ Council (agents/orchestrator) ─── stage 1: Analyst · Volatility · Flow · Event · Sentinel ·     │
│      │                                       RiskGuardian · Reviewer  (parallel)                │
│      │                          stage 2: StrategySelector · ExecutionTactician                  │
│      │                          weighted consensus + vetoes → ENTER / PROPOSE / HOLD / VETO /   │
│      ▼                                                             AVOID / PROTECT              │
│ StrategyEngine  (plans → runs, MTM, SL / target / trailing / square-off, adjustments)           │
│      ▼                                                                                          │
│ OrderManager ── RiskManager.pre_trade ── mode gate (MANUAL approval) ── Broker ── Positions     │
│      │                                                                                          │
│ RiskManager.evaluate (1 s) → level GREEN/AMBER/RED/HALTED → halt() flattens + closes gate       │
│ AlertEngine (dashboard / Telegram / e-mail) · AuditLog (hash chain) · Database (SQLite)         │
│ HealthMonitor · feed supervisor (reconnect)                                                     │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Packages

| Package | Responsibility |
|---|---|
| `terminal/config.py` | Pydantic settings from `.env`; safest defaults; `live_allowed` interlock |
| `terminal/core` | Domain models, async event bus, hash-chained audit log, IST market clock & expiry calendar |
| `terminal/market` | Universe (NSE/BSE/MCX), feeds (simulator, Kotak Neo), Black-Scholes pricing, option-chain builder with persistent OI state, tick processor & indicators |
| `terminal/strategy` | Strategy library (legs, payoff, margin estimate), engine (runs, exits, adjustments), scheduler |
| `terminal/risk` | Pre-trade gates, portfolio monitor, halt / kill switch / safety gate |
| `terminal/execution` | Order manager (all gates, approvals), position manager (fills → P&L, greeks), brokers (paper, Kotak, Zerodha) |
| `terminal/agents` | Agent base + MarketContext, 9 specialists, Council orchestrator, optional LLM narrator |
| `terminal/analytics` | Performance statistics, fast backtester |
| `terminal/notifications` | Alert engine and channels |
| `terminal/monitoring` | Health monitor |
| `terminal/storage` | SQLite repository |
| `terminal/api` | FastAPI app, auth |
| `terminal/ui` | Static dashboard (HTML/CSS/ES modules + canvas charts) |

## Order path (identical in both modes)

1. `OrderManager.submit(order)` is the **only** way anything reaches a broker
   (UI, agents, strategy engine, sentinel, kill switch).
2. `RiskManager.pre_trade` — kill switch, safety gate, halted state, market
   enabled, lots per order, open lots (total & per market), open positions,
   naked-short policy, session/entry window, margin utilisation, daily loss,
   LIVE interlock, feed freshness. Reducing / protective orders skip the
   entry-only checks so exits are always possible.
3. Mode gate — in MANUAL, agent-originated *entries* become
   `PENDING_APPROVAL`. Council plans are approved at plan level (Approvals
   panel), which re-prices legs at approval time.
4. Broker fills (paper: bid/ask crossing + slippage + STT/txn/GST/SEBI/stamp
   charges) → `PositionManager.apply_fill` → trade book → audit → event bus →
   dashboard.

## Council decision rule

Each enabled agent returns `score ∈ [-1, 1]`, `confidence ∈ [0, 1]` and an
optional `veto`. Consensus is the confidence- and weight-weighted mean score.
`ENTER`/`PROPOSE` needs `consensus ≥ AUTO_MIN_CONSENSUS`, `confidence ≥
AUTO_MIN_CONFIDENCE`, no veto, an open entry window, no active run on the
underlying and a passing plan-level risk check. `Sentinel` in `PROTECT`
stance exits active runs (AUTO, or MANUAL with `PROTECT_IN_MANUAL`).
`PostTradeReviewer` maintains strategy × regime win-rates that feed back into
`StrategySelector` scoring (learning loop persisted in `agent_memory`).

## Extending

- **New strategy**: add a `StrategySpec` and a branch in `build_legs` in
  `terminal/strategy/library.py`; the engine, risk, UI and backtester pick it up.
- **New agent**: subclass `Agent` in `terminal/agents/specialists.py`, return an
  `Assessment`, register it in `Council.agents`.
- **New broker / feed**: implement `Broker` (`execution/brokers/base.py`) or
  `MarketFeed` (`market/feed.py`); wire in `Terminal._make_broker/_make_feed`.
- **Real option quotes**: call `OptionChainBuilder.apply_broker_quotes()` with
  `{symbol: {ltp, iv, oi, volume, oi_change}}` and they override the model.

## Persistence

`runtime/terminal.sqlite3` (orders, fills, trades, runs, plans, council
decisions, alerts, settings, agent memory, logs) and `runtime/audit.jsonl`
(hash-chained; corruption fails closed at startup).
