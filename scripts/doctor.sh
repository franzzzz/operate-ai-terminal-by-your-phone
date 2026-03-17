#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

failures=0

check_cmd() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    echo "[ok] command found: $name"
  else
    echo "[missing] command not found: $name"
    failures=$((failures + 1))
  fi
}

echo "Checking local environment..."
check_cmd python3
check_cmd node
check_cmd tmux

if [[ -f .env ]]; then
  echo "[ok] .env exists"
else
  echo "[missing] .env does not exist"
  failures=$((failures + 1))
fi

if [[ -d .venv ]]; then
  echo "[ok] .venv exists"
else
  echo "[missing] .venv does not exist"
  failures=$((failures + 1))
fi

if [[ -f sidecar/package.json ]]; then
  echo "[ok] sidecar/package.json exists"
fi

if [[ -f .env ]]; then
  if grep -q '^TELEGRAM_BOT_TOKEN=replace_me$' .env; then
    echo "[warning] TELEGRAM_BOT_TOKEN still uses the placeholder value"
    failures=$((failures + 1))
  fi
  if grep -q '^AUTHORIZED_USER_IDS=123456789$' .env; then
    echo "[warning] AUTHORIZED_USER_IDS still uses the placeholder value"
    failures=$((failures + 1))
  fi
fi

if [[ "$failures" -gt 0 ]]; then
  echo
  echo "Doctor found ${failures} issue(s). Fix them before relying on the controller."
  exit 1
fi

echo
echo "Doctor checks passed."
