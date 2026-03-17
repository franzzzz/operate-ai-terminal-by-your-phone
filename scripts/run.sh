#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -r requirements.txt
if [[ -f "$ROOT_DIR/sidecar/package.json" ]]; then
  (cd "$ROOT_DIR/sidecar" && npm install)
fi
export PYTHONPATH="$ROOT_DIR/src:${PYTHONPATH:-}"
python -m pocket_operator.main
