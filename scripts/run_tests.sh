#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
pip install -q -r requirements-dev.txt
python -m pytest -q
