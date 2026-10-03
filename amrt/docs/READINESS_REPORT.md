# Readiness report — AI Market Risk Terminal 1.0.0

**Final status: `NOT READY` for live trading.** Live broker integration is `BLOCKED` from verification in the build
environment (broker APIs and NSE are not reachable from it). PAPER operation on replay or SIMULATED data is
`TESTED` end to end. Live order flow is disabled by configuration (`PAPER_ONLY`) and must stay disabled until the
owner completes the activation review in [OPERATIONS.md](OPERATIONS.md) §6 on a machine with broker access.

Status vocabulary used below: IMPLEMENTED, TESTED, VERIFIED, BLOCKED, NOT RUN.

## 1. Evidence (all files in `amrt/evidence/`)

| Check | Command | Result |
|---|---|---|
| Full test suite with real PostgreSQL 16 + Redis 7 | `AMRT_TEST_PG_URL=… AMRT_TEST_REDIS_URL=… python -m pytest tests --junitxml=evidence/junit.xml --cov=amrt` | **116 passed, 0 failed, 0 skipped** (`junit.xml`, `pytest_full.txt`) |
| Coverage | same | **79 %** of 7,624 statements (`coverage.xml`); kernel 91 %, gateway 78 %, pipeline 98 %, supervisor 94 %, automatic mode 91 %, identity 95 % |
| Tests by marker | `pytest -m <marker> --co` | unit 50 · integration 4 · contract 5 · security 5 · quant 17 · e2e 7 · chaos 9 · recovery 3 · replay 3 · acceptance 91 (`test_counts_by_marker.txt`) |
| Lint | `ruff check amrt tests` | clean (`ruff.txt`) |
| Dashboard typecheck / build | `npx tsc --noEmit`; `npx next build` | exit 0 / exit 0, 15 static routes (`frontend_typecheck.txt`, `frontend_build.txt`) |
| Rollback rehearsal (SQLite) | `deploy/scripts/backup.sh` → upgrade → `deploy/scripts/rollback.sh <ref> <backup>` | code and data returned to the previous release; post-upgrade data absent; audit chain intact; corrupt backup refused (`rollback_rehearsal.txt`) |
| Backup/restore (PostgreSQL) | `python -m amrt backup` (pg_dump) → `python -m amrt restore` (pg_restore) | restored, schema 2, chain intact (`pg_backup_restore.txt`) |
| Browser check of the dashboard | Playwright/Chromium against a running server with SIMULATED data | all pages render; KILL SWITCH button engages both switches and the audible CRITICAL alert arrives over the WebSocket |
| Real entry point | `python -m amrt` | preflight, one-time setup code, owner creation, PAPER start, dashboard served |

## 2. Phase reports

| Phase | Status | Notes |
|---|---|---|
| 1 Requirements, threat model, contracts, inventory | IMPLEMENTED | [REQUIREMENTS_AND_THREAT_MODEL.md](REQUIREMENTS_AND_THREAT_MODEL.md) |
| 2 Foundation (auth, schemas, DB, logging, metrics, health, audit) | TESTED | SQLite + PostgreSQL; append-only + TRUNCATE-proof audit; hash chain tamper detection |
| 3 Market data, instrument master, freshness, persistence, replay | TESTED | live adapters BLOCKED from live verification |
| 4 Broker adapters (Kotak, Zerodha, Angel One, Upstox, Groww) | IMPLEMENTED + contract-TESTED; live **BLOCKED** | every SDK call binds to the installed official SDK signatures |
| 5 Agents, Master, schema validation, conflicts, quarantine, fallback | TESTED | failures, timeouts, quarantine, disagreement, tampered proposals, prompt injection |
| 6 Option chain, PCR, FII/DII, backtesting | TESTED | hand-computed numerics; no-look-ahead fills; walk-forward separation |
| 7 Risk Kernel, dual kill switch, modes, gateway, reconciliation | TESTED | forged/expired/mismatched decisions, unknown states, duplicates, isolation |
| 8 Reliability Control Plane, watchdog, self-healing, incidents | TESTED | bounded, audited, cannot delete incidents, no financial authority |
| 9 Dashboard on real backend | TESTED | Next.js static export; browser-checked |
| 10 Test matrix | TESTED | 116 tests (§1) |
| 11 Backup restoration, rollback rehearsal, readiness review, live-data verification | backup/rollback TESTED; Docker **NOT RUN** (no Docker daemon); live-data verification **BLOCKED** | |

## 3. Defects found by testing and fixed in this build

1. FII participant-OI parser lost the publication date when the CSV title line was not quoted (comma inside "Oct 03, 2026").
2. PostgreSQL TRUNCATE bypassed the row-level append-only triggers; incidents, recovery actions and decision packages
   could be deleted. Migration 2 protects them (no DELETE; no UPDATE for recovery actions and decisions; no TRUNCATE).
3. Restore copied the current SQLite file aside with a plain file copy, losing rows still in the WAL; it now uses the
   SQLite online-backup API.
4. API passed the policy kind in lowercase, sending a risk policy down the automation-policy branch.
5. Marketable hedge prices were rounded to the tick back inside the spread (BUY rounded down); they now round away.
6. The chain agent read coinciding max-CE/max-PE OI strikes as a crossed range; it now reports concentration.
7. One reliability test asserted a deletion failure vacuously (it raised its own AssertionError) — rewritten; it then
   exposed defect 2.

## 4. Mandatory acceptance tests (master prompt §20)

The index `tests/acceptance/test_acceptance_index.py` fails if any referenced test disappears. All referenced tests pass.

| ID | Requirement | Status | Proving tests (abridged) |
|---|---|---|---|
| AT-01 | Specialist agents cannot place live orders | TESTED | AST import isolation; agent principals refused by gateway; permission matrix invariants |
| AT-02 | Master cannot bypass the Risk Kernel | TESTED | Master principal refused by gateway; packages never AUTHORIZED; paper auto-approve goes through 4 kernel decisions |
| AT-03 | Kernel rejection cannot be overridden | TESTED | flipped verdict, second kernel key, decision for another intent, expired decision, no decision |
| AT-04 | Reliability plane cannot modify financial authority | TESTED | supervisor refused on releases, mode, policy, unquarantine, kill; non-allowlisted actions rejected |
| AT-05 | Paper cannot access live APIs or credentials | TESTED | schema, gateway routing, venue ticket checks, only gateway holds credentials, no live venue in PAPER_ONLY, paper venue imports no broker code |
| AT-06 | Mode crossover / unauthorized live activation blocked | TESTED | PAPER_ONLY refuses MANUAL; startup always PAPER; role, step-up, phrase, readiness and exposure guards |
| AT-07 | Agent failures and disagreements → documented safe outcomes | TESTED | crash/timeout/quarantine → INSUFFICIENT DATA; caution majority → NO ACTION; tampered legs → REJECTED; injection quarantined; Master failure suspends AI |
| AT-08 | Unknown order states stay unresolved until reconciled | TESTED | timeout → UNKNOWN; resolved only by broker reads; NOT_FOUND needs ≥ 2 reads + grace; Path B freezes after 30 s |
| AT-09 | No duplicates after ambiguous timeouts | TESTED | same idempotency key → existing order (SQLite and 5 concurrent retries on PostgreSQL); RK-013/RK-014 |
| AT-10 | Kill switches effective during agent and supervisor failures | TESTED | all agents quarantined + Master/supervisor down → RK-001; switch B alone stops the gateway; DB loss engages B; watchdog writes B |
| AT-11 | Self-healing bounded, auditable, idempotent, cannot suppress incidents | TESTED | ≤ 3 attempts then quarantine; one incident ESCALATED; unique recovery records; deletes blocked |
| AT-12 | Critical failures freeze new risk | TESTED | heartbeat loss, stale data, P&L unavailable, broker read failure, critical component failure → freeze/recovery lock |
| AT-13 | Recovery requires reconciliation and reauthorization | TESTED | recovery lock survives component recovery; release needs owner + step-up + passing verification |
| AT-14 | PCR, OI, FII/DII, P&L, backtest numerics | TESTED | hand-computed values incl. zero/negative denominators, tie-breaks, charges by date, flips, missing marks |
| AT-15 | Disconnected states show DATA UNAVAILABLE | TESTED | API with no source: chain/PCR/FII show DATA UNAVAILABLE; stale quotes raise; missing marks → P&L None |
| AT-16 | Security, redaction, backup restoration, rollback | TESTED | redaction (logs, events, config), tamper detection, roles/CSRF/step-up/headers, static hygiene, restore + rollback |

## 5. Security findings

* No credentials in the repository; `.env` is git-ignored and excluded from release zips; tests never read a real `.env`.
* Credentials previously pasted into chat during this project should be **regenerated** (Kotak consumer key, TOTP secret).
* Residual risks: single-owner deployment (no four-eyes approval); signing keys are per-process (restart invalidates
  in-flight tickets — intended); the dashboard must sit behind HTTPS before leaving the loopback interface.

## 6. Blockers and dependencies (why the status is NOT READY)

1. **Live broker verification BLOCKED** — no broker API or NSE endpoint is reachable from the build environment; no
   broker feature is VERIFIED.
2. **Docker NOT RUN** — no Docker daemon in the build environment; the Compose file was validated syntactically only.
3. Lot sizes, charges and expiry calendars are defaults with effective dates and must be confirmed against current
   exchange circulars and your broker's tariff.
4. No validated backtest: only SIMULATED data was available here, which by design never counts as evidence.
5. The owner's live-activation review (OPERATIONS.md §6) has not been performed.

## 7. Next action

Run the activation review on your machine with read-only broker data first (`AMRT_DATA_BROKER=kotak`, PAPER_ONLY),
record a few weeks of chain snapshots, run the backtest/walk-forward on that real data, and only then consider
enabling live order flow.
