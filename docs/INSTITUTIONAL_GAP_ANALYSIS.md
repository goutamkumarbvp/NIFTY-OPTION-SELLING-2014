# Institutional gap analysis — NIFTY AI Options Terminal

A deep review of the terminal against what a proprietary desk / institutional
options-trading platform is expected to have. Each gap is marked **FIXED** (built
and tested in this release), **PARTIAL** (present but lighter than a bank-grade
implementation) or **OUT OF SCOPE** (needs infrastructure, vendors or approvals
that software alone cannot supply).

## 1. Market data

| Gap | Institutional norm | Status |
|---|---|---|
| No validation of inbound prices | Every print passes a quality gate: non-positive, outlier (held until a second update confirms), crossed quotes, stale timestamps; per-symbol reject statistics | **FIXED** — `terminal/market/quality.py`, wired into `_on_tick` / `_on_option_quote`; Guardian raises `DATA_QUALITY` when the global or any per-symbol reject rate is unacceptable |
| No raw data retention | Every tick is journaled for replay / post-mortem / research | **FIXED** — daily gzip journal `runtime/ticks/YYYY-MM-DD.jsonl.gz`, `iter_day()` reader, `/api/system/data-quality` |
| Single feed, no redundancy | Primary + secondary feed with automatic fail-over | **PARTIAL** — one provider at a time (Kotak / Zerodha / Angel) with reconnect, re-login and Guardian escalation; a second live feed would need a second data contract |
| Tick resolution unknown | Consume every update; measure and display inter-tick interval | **FIXED** (previous release) |
| No clock discipline | NTP-disciplined host clock, exchange timestamp comparison | **OUT OF SCOPE** — host/OS responsibility; stale-timestamp counter is exposed |

## 2. Pricing and risk models

| Gap | Institutional norm | Status |
|---|---|---|
| No scenario stress | Spot × vol grid re-pricing of the whole book every cycle, with a limit | **FIXED** — `terminal/risk/portfolio.py`: 9 spot × 4 vol scenarios, worst loss / scenario in the risk snapshot, `STRESS_LOSS_LIMIT` breach turns risk RED and blocks entries (exits always allowed) |
| Delta / vega only | Delta, gamma, vega, theta limits | **FIXED** — gamma limit added (`portfolio_gamma_limit`) |
| Underlyings treated independently | Beta-weighted cluster exposure (index cluster, energy, metals) with limits | **FIXED** — `CLUSTERS` with betas, `max_cluster_delta_notional`, `CLUSTER_DELTA_*` breaches |
| Expiry-day gamma risk | No new short options on a contract expiring today near the close | **FIXED** — `expiry_gamma_cutoff_minutes`, `EXPIRY_DAY_CUTOFF` pre-trade reason, `EXPIRY_DAY_POSITIONS` warning |
| Liquidity concentration | Our size versus contract open interest | **FIXED** — `max_oi_participation_pct` (pre-trade and monitor) |
| Flat Black-Scholes IV | Vol surface (SABR / SVI) with skew and term structure | **PARTIAL** — IV is solved per strike from live prices (implied smile); no parametric surface |
| Margin estimate | Exchange SPAN + exposure from the clearing file | **PARTIAL** — SPAN-like estimate pre-trade, broker-reported margin overrides it when live |

## 3. Execution and order controls (SEBI algo-trading style)

| Gap | Institutional norm | Status |
|---|---|---|
| No price collar | Limit price within ±x% of LTP; market orders need a fresh quote | **FIXED** — `terminal/risk/pretrade.py` `PRICE_COLLAR` |
| No order value / notional caps | Max premium value and underlying notional per order, per-underlying gross notional | **FIXED** — `MAX_ORDER_VALUE`, `MAX_ORDER_NOTIONAL`, `MAX_UNDERLYING_NOTIONAL` |
| No gateway throttle | Orders per second / minute, working-order cap | **FIXED** — `THROTTLED_PER_SECOND/MINUTE` (exits get 3× headroom, never zero), `MAX_WORKING_ORDERS` |
| No order-to-trade ratio guard | OTR monitoring / rejection | **FIXED** — `ORDER_TO_TRADE_RATIO` |
| No idempotency | Duplicate order detection window | **FIXED** — `DUPLICATE_ORDER` (entries), exit duplicates already suppressed by the exit guard |
| Trades on model prices | Entries only on live broker quotes; spread and crossed-quote checks | **FIXED** — `NO_LIVE_QUOTE`, `ILLIQUID_SPREAD`, `CROSSED_QUOTE` |
| Fat-finger | Size sanity versus recent order sizes | **FIXED** — `FAT_FINGER_SIZE` |
| No transaction-cost analysis | Arrival price, implementation shortfall (bps), half-spread cost, submit→fill latency, by source / underlying / side | **FIXED** — `terminal/analytics/tca.py`, `/api/reports/tca`, Reports panel, persisted in the `tca` table |
| Smart order routing / slicing | TWAP / iceberg for large size | **OUT OF SCOPE** for retail lot sizes; limit-then-market chasing is handled by the exit guard's retry loop |

## 4. P&L and performance analytics

| Gap | Institutional norm | Status |
|---|---|---|
| No P&L attribution | Greek decomposition (Δ, ½Γ, Θ, V, residual) per day and per strategy | **FIXED** — `PnLAttribution`, `/api/reports/attribution`, Reports panel |
| Model-based backtester only | Replay of real recorded ticks | **PARTIAL** — real ticks are now journaled; the replay harness reads them (`dq.iter_day`) but the backtester still uses synthetic paths |
| Learned model not governed | Model versioning, drift monitoring, rollback | **FIXED** — versions snapshot on every training pass, `drift()`, `/api/model/versions`, `/api/model/rollback` |

## 5. Governance, compliance and security

| Gap | Institutional norm | Status |
|---|---|---|
| One admin can change anything | Four-eyes on control-plane changes | **FIXED** — `terminal/core/governance.py`: risk limits, switch to AUTO, gate open and Guardian policy are proposed by one admin and approved by a different one (on by default in LIVE, `FOUR_EYES_REQUIRED` elsewhere); Approvals → Governance panel |
| No configuration history | Old → new value, who, when | **FIXED** — `config_history` table, shown in `/api/governance` |
| Weak login protection | Lockout after repeated failures, session expiry | **FIXED** — lockout (`LOGIN_LOCKOUT_*`), `SESSION_TTL_HOURS`, audited `LOGIN_FAILED` / `LOGIN_LOCKED` |
| Audit trail | Tamper-evident, exportable | already hash-chained; now also copied with every backup |
| Secrets management | HSM / vault, rotation | **OUT OF SCOPE** — `.env` (mode 600) is the supported store; rotate broker keys regularly |
| Transport security | TLS, network segmentation | **OUT OF SCOPE** — bind to loopback or terminate TLS at a reverse proxy |

## 6. Reliability and operations

| Gap | Institutional norm | Status |
|---|---|---|
| No backups | Scheduled point-in-time backups with rotation | **FIXED** — `terminal/storage/backup.py` (SQLite online backup + audit copy every `BACKUP_INTERVAL_MINUTES` and at end of day, keep `BACKUP_KEEP`) |
| No latency SLOs | p50 / p95 / p99 per critical path with alerting | **FIXED** — `terminal/monitoring/latency.py` (tick→mark, quote→eval, submit→ack, submit→fill, council cycle), Guardian `LATENCY_SLO`, Prometheus `terminal_latency_p95_ms` |
| Self-healing | Watchdogs with human-in-the-loop remediation | **FIXED** (previous release) — Guardian agent; now also watches data quality, latency and backups |
| Hot standby / multi-region | Active-passive process pair with state hand-over | **OUT OF SCOPE** — single process by design; restart recovery + backups cover the crash case |
| Change management / CI | Lint + tests on every change | present — GitHub Actions; 83 tests |

## What remains genuinely institutional-only

* Exchange membership, co-location and direct market access.
* Redundant data vendors and a second broker for fail-over.
* Parametric volatility surface and full SPAN margin engine from the clearing files.
* Hardware security modules for credentials and a network security perimeter.

Everything else in the tables above is implemented in this release and covered by
`tests/test_institutional.py`.
