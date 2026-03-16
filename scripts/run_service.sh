#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

source .venv/bin/activate
export PYTHONPATH="$ROOT_DIR/src:${PYTHONPATH:-}"
exec python -m telegram_codex_controller.main
