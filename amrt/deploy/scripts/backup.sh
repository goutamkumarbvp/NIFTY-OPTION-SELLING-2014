#!/usr/bin/env bash
# Take a backup now (SQLite online backup, or pg_dump for PostgreSQL). Output: $AMRT_RUNTIME_DIR/backups/
set -euo pipefail
cd "$(dirname "$0")/../.."
python -m amrt backup
