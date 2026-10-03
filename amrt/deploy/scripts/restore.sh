#!/usr/bin/env bash
# Restore a backup. Stop the application first. The current database is copied aside before anything is replaced,
# and the restored database must pass schema + audit hash-chain verification or nothing is changed.
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 <backup file>"; exit 2; }
cd "$(dirname "$0")/../.."
python -m amrt restore "$1"
python -m amrt verify
