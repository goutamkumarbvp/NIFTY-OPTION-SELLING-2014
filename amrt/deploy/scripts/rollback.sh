#!/usr/bin/env bash
# Roll back a release: code to a previous git ref, data to the backup taken before the upgrade.
#   deploy/scripts/rollback.sh <git-ref> <pre-upgrade backup file>
# Steps: verify inputs → stop (operator) → checkout code → restore data → verify → start in PAPER mode (always).
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <git-ref> <backup file>"; exit 2; }
REF="$1"; BACKUP="$2"
cd "$(dirname "$0")/../.."
git rev-parse --verify "$REF^{commit}" >/dev/null
[ -f "$BACKUP" ] || { echo "backup not found: $BACKUP"; exit 2; }
echo "[rollback] make sure the application is stopped (docker compose stop app  /  Ctrl+C)"
CURRENT=$(git rev-parse HEAD)
git checkout --quiet "$REF"
python -m amrt restore "$BACKUP" || { echo "[rollback] restore failed — returning code to $CURRENT"; git checkout --quiet "$CURRENT"; exit 1; }
python -m amrt verify
echo "[rollback] code at $(git rev-parse --short HEAD), data restored from $BACKUP. Start the app: it always starts in PAPER mode."
