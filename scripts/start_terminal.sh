#!/usr/bin/env bash
# Start the terminal (live broker data, paper fills by default). Usage: scripts/start_terminal.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || { cp .env.paper .env; echo "Created .env from .env.paper — add your broker credentials (NEO_*) and run again"; exit 1; }
exec python -m terminal
