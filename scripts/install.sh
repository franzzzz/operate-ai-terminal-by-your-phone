#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

echo "[1/5] Creating local directories..."
mkdir -p logs .state

echo "[2/5] Creating Python virtual environment if needed..."
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

echo "[3/5] Installing Python dependencies..."
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[4/5] Installing sidecar dependencies..."
if [[ -f "$ROOT_DIR/sidecar/package.json" ]]; then
  (cd "$ROOT_DIR/sidecar" && npm install)
fi

echo "[5/5] Preparing local config..."
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
else
  echo ".env already exists, leaving it untouched"
fi

cat <<'EOF'

Install complete.

Next steps:
  1. Edit .env and set TELEGRAM_BOT_TOKEN and AUTHORIZED_USER_IDS
  2. Run ./scripts/doctor.sh
  3. Run ./scripts/bootstrap.sh

EOF
