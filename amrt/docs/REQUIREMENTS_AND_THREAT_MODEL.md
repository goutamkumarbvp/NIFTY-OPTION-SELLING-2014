# Phase 1 — Requirements, threat model, safety contracts, data and API inventory

## 1. Requirements (summary)

* Human-governed: AI advises; the owner or a pre-approved automation policy authorizes; the deterministic kernel
  validates every order; only the gateway can reach a broker.
* Three modes (PAPER / MANUAL / AUTOMATIC) with technical isolation and no automatic transition into a live mode.
* Fail safe: missing data, uncertain broker state or unverifiable safety → NO ACTION, FREEZE NEW RISK, REQUIRES HUMAN
  APPROVAL or BLOCKED; displays say DATA UNAVAILABLE, ORDER STATE UNKNOWN, NOT READY rather than inventing values.
* Everything auditable and replayable; incidents are never suppressed.
* No profit promise and no guaranteed maximum loss anywhere in the product.

## 2. Safety contracts (each is enforced in code and covered by tests — see READINESS_REPORT.md §4)

| # | Contract | Enforcement |
|---|---|---|
| S1 | No agent can place, modify or cancel orders | capability matrix; agents import no execution/broker code (AST test) |
| S2 | No order reaches a venue without a kernel-signed approval for that exact intent | gateway signature + intent-hash check; venue ticket check |
| S3 | A kernel rejection is final | signature covers the verdict; no override API |
| S4 | Paper can never reach live | schema (PAPER ↔ broker "paper"), gateway routing, venue ticket mode check, no live venue in PAPER_ONLY |
| S5 | Ambiguity is never resolved by assumption | timeout → ORDER STATE UNKNOWN; only reconciliation resolves |
| S6 | No duplicate submission | UNIQUE idempotency key; client tag; RK-013/RK-014; no automatic retry |
| S7 | Protective exits always possible | reduce-only bypasses new-risk rules incl. kill switches |
| S8 | Latched safety state is released only by the owner after verification | capability matrix + server-side `verify_recovery()` |
| S9 | Self-healing is bounded and auditable | allowlist, backoff, recovery_actions table (no UPDATE/DELETE), incidents (no DELETE) |
| S10 | Startup is always PAPER | mode controller ignores the stored mode |

## 3. Threat model (STRIDE)

| Threat | Example | Mitigation |
|---|---|---|
| Spoofing | forged risk decision / ticket; stolen session | HMAC with process-local keys; Verifier objects cannot sign; HttpOnly SameSite=Strict cookie, CSRF token, step-up, TOTP, lockout |
| Tampering | editing the audit trail; altering an approved intent | append-only triggers (UPDATE/DELETE/TRUNCATE), hash chain verified periodically and before releases; intent hash bound into decision and ticket |
| Repudiation | "I never approved that" | every approval, step-up, mode change and denial is an event with actor, time and correlation id |
| Information disclosure | broker keys in logs, API or zip | credentials only in the git-ignored `.env`, only the gateway principal holds them, redaction on logs/events/config views, release zip excludes `.env` |
| Denial of service | broker / data outage, DB loss, wedged loop | freshness rules, Path B, watchdog (thread + external process), kill switch B independent of DB |
| Elevation of privilege | agent or supervisor acting as owner; prompt injection via news | capability checks on every privileged call; untrusted text sanitized, flagged and passed as data only; LLM output cannot change status |

## 4. Data-source inventory

| Data | Source | Label | Status in this build |
|---|---|---|---|
| Option chains, quotes | broker read APIs (Kotak Neo first; Zerodha, Angel, Upstox, Groww) | LIVE (UNVERIFIED) → LIVE DATA VERIFIED when authenticated, fresh and drift-checked | implemented; **live access BLOCKED from the build environment** |
| Historical replay | chain snapshots recorded by this system (JSONL) | HISTORICAL REPLAY | implemented, tested |
| Demo market | deterministic simulator (opt-in, PAPER_ONLY only) | SIMULATED | implemented, tested |
| FII/DII | NSE participant-wise OI CSV, NSE FII/DII cash activity — imported by the owner | OFFICIAL END-OF-DAY | parser + import tested; NSE website not reachable from the build environment |
| Events calendar | owner-supplied JSON file | — | nothing fetched or invented |
| News | none configured | — | the News agent reports "unscheduled news risk unknown" |
| Lot sizes | defaults with effective dates (NIFTY 75 → 65 from 2025-12-30, etc.), overridden by broker masters | — | **verify against exchange circulars before live use** |
| Charges | dated schedule (STT change 2024-10-01) | — | verify brokerage and exchange fees for your plan |

## 5. Official API verification status

See [BROKER_CAPABILITY_MATRIX.md](BROKER_CAPABILITY_MATRIX.md). Every SDK call the adapters make is bound against the
installed official SDK signatures (`tests/contract`). No broker endpoint could be exercised live from the build
environment (network policy), so no broker feature is VERIFIED; live verification is part of the activation review in
[OPERATIONS.md](OPERATIONS.md) §6.
