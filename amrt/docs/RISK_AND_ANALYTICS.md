# Risk Kernel, protective paths and analytics methodology

## 1. Risk Kernel

Every order — owner, automation or protective — becomes an immutable `OrderIntent` that is persisted, evaluated by the
kernel against a context built from live state, and signed (HMAC) together with the intent hash. The gateway rejects a
decision that is unsigned, signed by another key, for a different intent, expired (5 s), or not approved. A kernel error
yields a rejection (fail closed). The full rule list is in [RISK_KERNEL_RULES.md](RISK_KERNEL_RULES.md).

**New risk vs reduce-only.** Rules that protect against *adding* exposure (kill switch, freeze, recovery lock, policy,
window, size, exposure, margin, loss, margin-risk, naked short, critical services, clock drift) apply to new risk only.
Reduce-only orders are checked for authorization, size against the position minus in-flight exits (RK-016), unknown
orders on the same instrument, rate (2× allowance) and price sanity — so exits are never blocked by the very conditions
that require them.

**Data rules.** Live accounts need `LIVE DATA VERIFIED` (authenticated live source, fresh, drift inside tolerance) for the
instrument and underlying; paper accounts accept live, replay, or — only in a PAPER_ONLY deployment with
`AMRT_SIMULATED_MARKET=true` — SIMULATED quotes. Clock drift that cannot be measured blocks live new risk (RK-026).

## 2. Loss thresholds and denominators

| Trigger | Default | Action |
|---|---|---|
| Level 1 absolute | ₹2,000 net loss today (after charges) | EMERGENCY LEVEL1: freeze new risk, alert |
| Level 2 absolute | ₹4,000 | EMERGENCY LEVEL2: freeze, SEV1 incident, pre-authorized protective action (FLATTEN_ALL or ALERT_ONLY) |
| Level 1 margin-risk | 1 % | as level 1 |
| Level 2 margin-risk | 2 % | as level 2 |

`margin-risk % = loss ÷ denominator × 100`, where the denominator is the policy's choice: **SOD_ACCOUNT_FUNDS** (total
account funds captured from the broker at the first reconciliation of the IST day; paper: paper capital) or
**MARGIN_USED** (current margin utilised). If the denominator is unavailable the margin-risk rule fails closed for new
risk. These are **triggers, not guaranteed maximum losses**: gaps, slippage, halts and API failures can exceed them.

## 3. Path A and Path B

* **Path A — Portfolio Risk Monitor** (every second): P&L per account; P&L that cannot be computed (any mark missing)
  freezes new risk; level 1/2 as above; per-leg stop (short leg premium +50 % by default); profit ratchet that only
  tightens. Protective flatten buys back shorts first, is bounded (≤ 5 attempts / 5 min, 10 s spacing) and reports the
  residual exposure. A live position while in PAPER mode raises an alert rather than sending a live order from PAPER.
* **Path B — Independent Safety Monitor** (every second): database unreachable → kill switch B; stale data on held
  instruments; broker unavailable with positions; heartbeat loss of gateway / event store / Path A / reconciler /
  database; clock drift; ORDER STATE UNKNOWN older than 30 s → freeze; Master Agent unhealthy → AI actions suspended (no
  other agent is promoted).
* **Watchdog**: thread + external process; engages switch B when the event loop stalls or a protective loop goes silent
  while positions are open, or (external) when the application heartbeat file goes stale.

## 4. Option-chain analytics (`analytics/option_chain.py`, version chain/1.0)

* ATM strike = listed strike nearest spot (tie → lower). Window = ATM ± 10 listed strikes.
* **Total OI PCR** = Σ PE OI ÷ Σ CE OI over the window (also reported for the full chain).
* **Change-in-OI PCR** = Σ PE ΔOI ÷ Σ CE ΔOI. Denominator 0 → undefined (never a number). Negative denominator →
  value reported with the interpretation "CE net unwinding — not comparable with the usual reading".
* Max and second-highest OI per side; ties → closer to ATM, then lower strike.
* Buildup / unwinding = largest positive / most negative ΔOI across both sides.
* Look-back windows 1 / 5 / 15 / 60 min compare with the snapshot closest to the window start (tolerance max(30 s, 20 %));
  no snapshot → INSUFFICIENT HISTORY (nothing interpolated).
* Coverage < 80 % of window strikes with OI → INCOMPLETE; < 50 % → DATA UNAVAILABLE (ratios None).
* Support/resistance zones are labelled **inference**: OI does not reveal whether contracts were bought or sold and does
  not predict direction.

## 5. FII / DII (`analytics/fii_dii.py`, version fiidii/1.0)

Sources: NSE participant-wise open interest CSV and NSE FII/DII cash-market activity. Client types must sum to the TOTAL
row (±0.5) and cash rows must satisfy net = buy − sell (±0.05) or the file is rejected. Re-imports with different content
become a new revision. Aggregations (D/W/M/Y/5Y) report completeness against expected weekdays; < 90 % is INCOMPLETE.
**No participant profit or loss is computed or estimated.**

## 6. Backtesting (`quant/backtest.py`, version backtest/1.0)

* Input: chain snapshots recorded by this system; the result inherits the input label. SIMULATED input produces a
  "SIMULATED BACKTEST — not evidence" and can never become validation for the Strategy Agent.
* Entry at the first snapshot inside 09:20 + 10 min (configurable); exit at the first snapshot ≥ 15:05, else the day's
  last snapshot (flagged). Decisions use data ≤ t; fills at the **next** snapshot at the touch (buy ask / sell bid) plus
  slippage; a missing quote at fill time means no trade. Charges from the dated schedule (STT 0.0625 % → 0.1 % on option
  sells from 2024-10-01).
* Metrics: net P&L, win rate, profit factor, max drawdown, Sharpe/Sortino (daily, √252), 5 % CVaR, worst/best day.
* Walk-forward: chronological folds, parameters chosen only in-sample; out-of-sample hit rate. Sensitivity (costs ×1.5,
  slippage ×2, entry +5 min, fill delay 2) and Black-Scholes stress (spot ±2/±4 %, IV +50 %).
* Validation (used by the Strategy Agent's confidence) requires non-simulated data, walk-forward OK and ≥ 60
  out-of-sample days. Past results do not predict future results.

## 7. Decision package aggregation (Master)

1. A critical agent failed, timed out, was quarantined or had no data → **INSUFFICIENT DATA**; a LIVE account also needs
   LIVE DATA VERIFIED.
2. Critical verification failure → **REJECTED**.
3. Risk-reducing proposals skip stance gating.
4. New risk: any BLOCK stance → **REJECTED** (a roll degrades to its exit legs); CAUTION ≥ SUPPORTIVE among context
   agents → **NO ACTION** with the disagreement recorded.
5. Non-binding kernel pre-check of every leg in execution order (hedges first) → failure → **REJECTED**.
6. Otherwise **REQUIRES APPROVAL**. Confidence = mean context-agent confidence × agreement × data quality — a heuristic,
   not a probability.

The optional narrative (Claude API, off by default) receives only the slimmed package as a data block, never agent
outputs that quote external text; prohibited claims ("guaranteed profit", "risk-free", "disable the kill switch") are
suppressed and the narrative can never change a status.
