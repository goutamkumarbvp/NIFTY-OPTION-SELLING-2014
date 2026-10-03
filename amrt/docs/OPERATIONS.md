# Setup, build, test, deploy, backup, recovery, rollback

## 1. Run locally (Windows, macOS, Linux)

Requirements: Python 3.11+. Node.js is **not** needed — the dashboard build (`frontend/out`) is included.

```bat
cd NIFTY-OPTION-SELLING-2014\amrt
python -m venv .venv
.venv\Scripts\activate            (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
pip install -r requirements-brokers.txt      (only if you will connect a broker)
copy .env.example .env             (macOS/Linux: cp .env.example .env) — then edit .env
python -m amrt preflight
python -m amrt
```

Open http://127.0.0.1:8700. On first start the console prints a **one-time owner setup code**; enter it in the dashboard
with a username and a password (≥ 10 characters). The system always starts in **PAPER MODE — NO REAL ORDERS**.

* Demo without a broker: set `AMRT_SIMULATED_MARKET=true` (everything is labelled SIMULATED).
* Read-only live market data: set `AMRT_DATA_BROKER=kotak` and the `NEO_*` values. Orders stay disabled.
* Database: SQLite under `runtime/` by default; set `AMRT_DATABASE_URL=postgresql+psycopg://…` for PostgreSQL.

## 2. Docker Compose (PostgreSQL + Redis + app + external watchdog)

```bash
cp amrt/.env.example amrt/.env   # set AMRT_PG_PASSWORD at least
docker compose -f amrt/deploy/docker-compose.yml --env-file amrt/.env up -d --build
```

The dashboard is bound to 127.0.0.1:8700; put an HTTPS reverse proxy in front and set `AMRT_COOKIE_SECURE=true` before
exposing it. (Docker could not be run in the build environment — see READINESS_REPORT.md.)

## 3. Tests

```bash
cd amrt
pip install -r requirements-dev.txt -r requirements-brokers.txt
python -m pytest tests -q                          # PostgreSQL/Redis tests skip unless configured
bash deploy/scripts/local_pg.sh                    # optional throw-away PostgreSQL on :55432
AMRT_TEST_PG_URL=postgresql+psycopg://amrt@127.0.0.1:55432/postgres AMRT_TEST_REDIS_URL=redis://127.0.0.1:6379/0 python -m pytest tests -q
python -m pytest tests -m acceptance -q           # AT-01 … AT-16
ruff check amrt tests
cd frontend && npm ci && npx tsc --noEmit && npx next build   # dashboard
```

## 4. Backup, restore, verify

```bash
python -m amrt backup            # SQLite online backup (gzip) or pg_dump; newest AMRT_BACKUP_KEEP kept; also hourly while running
python -m amrt verify            # schema version + audit hash chain
bash deploy/scripts/restore.sh runtime/backups/amrt-<stamp>.sqlite3.gz   # app stopped; current DB copied aside first
```

A restore refuses a backup that fails verification and leaves the live database untouched.

## 5. Rollback a release

```bash
python -m amrt backup                                   # before every upgrade
bash deploy/scripts/rollback.sh <previous-git-ref> <pre-upgrade backup>
```

The script checks out the previous code, restores the data, verifies, and the app restarts in PAPER mode. Rehearsal
evidence: `evidence/rollback_rehearsal.txt` (SQLite) and `evidence/pg_backup_restore.txt` (PostgreSQL).

## 6. Live-trading activation review (separate, owner-run)

Live order flow needs **both** `AMRT_ENVIRONMENT=LIVE_CAPABLE` and `AMRT_LIVE_ORDERS_ENABLED=true`. Do not set them
until every item below is done and recorded:

1. Regenerate broker API keys/TOTP secrets that were ever shared outside `.env`.
2. Read-only verification on your machine: data broker login, chain and quotes for each underlying show
   `LIVE DATA VERIFIED`, clock drift measured, lot sizes and expiries match the exchange circular.
3. Reconciliation on the live account with no positions: orders/positions/funds read, start-of-day funds captured.
4. Owner creates and activates a risk policy for the live account (thresholds, denominator, lots, windows).
5. Mark the configuration known-good; run `Risk & Mode → Run recovery verification` — all checks pass.
6. One-lot manual order outside market hours is refused (entry window); during hours a one-lot hedge round-trip in
   MANUAL mode, then reconcile and compare with the broker's order book.
7. Drill: engage the kill switch, confirm new risk is refused and an exit still works; drill Manual Quick Exit.
8. Only then consider an automation policy — small lots, short validity, NO_ACTION fallback.

## 7. Security operations

* Credentials live only in the git-ignored `.env`; `scripts/build_release.py` excludes it from release zips.
* `python -m amrt preflight` shows the redacted effective configuration; Settings → configuration integrity shows drift.
* Audit: Audit page → Verify chain; any failure freezes new risk automatically (integrity loop).
