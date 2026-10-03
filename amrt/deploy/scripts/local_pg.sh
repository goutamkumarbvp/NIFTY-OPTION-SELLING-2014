#!/usr/bin/env bash
# Start a throw-away PostgreSQL cluster for integration tests:  source deploy/scripts/local_pg.sh [dir] [port]
# Prints and exports AMRT_TEST_PG_URL. Requires PostgreSQL server binaries (initdb, pg_ctl).
set -euo pipefail
DEFAULT_DIR="${TMPDIR:-/tmp}/amrt-pg"; [ "$(id -u)" = "0" ] && DEFAULT_DIR="/var/lib/postgresql/amrt-test"
DIR="${1:-$DEFAULT_DIR}"
PORT="${2:-55432}"
BIN="$(dirname "$(ls /usr/lib/postgresql/*/bin/pg_ctl 2>/dev/null | sort | tail -1)")"
[ -x "$BIN/pg_ctl" ] || { echo "pg_ctl not found"; return 1 2>/dev/null || exit 1; }
RUN=""
if [ "$(id -u)" = "0" ]; then RUN="runuser -u postgres --"; fi   # PostgreSQL refuses to run as root
if [ ! -f "$DIR/PG_VERSION" ]; then
  mkdir -p "$DIR"; chmod 700 "$DIR"; [ -n "$RUN" ] && chown postgres "$DIR"
  $RUN "$BIN/initdb" -D "$DIR" -U amrt --auth=trust -E UTF8 --locale=C.UTF-8 >/dev/null
fi
$RUN "$BIN/pg_ctl" -D "$DIR" -o "-p $PORT -k $DIR -c listen_addresses=127.0.0.1" -l "$DIR/server.log" status >/dev/null 2>&1 || \
  $RUN "$BIN/pg_ctl" -D "$DIR" -o "-p $PORT -k $DIR -c listen_addresses=127.0.0.1" -l "$DIR/server.log" -w start >/dev/null
export AMRT_TEST_PG_URL="postgresql+psycopg://amrt@127.0.0.1:$PORT/postgres"
echo "$AMRT_TEST_PG_URL"
