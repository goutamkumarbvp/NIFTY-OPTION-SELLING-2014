#!/usr/bin/env bash
# Start the terminal (paper / simulated by default). Usage: scripts/start_terminal.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
exec python -m terminal
