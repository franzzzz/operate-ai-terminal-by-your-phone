#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="gui/$(id -u)"
LOG_PATH="${TGC_LOG_PATH:-$ROOT_DIR/logs/stderr.log}"
DEFAULT_LABEL="com.example.pocket-operator"
LEGACY_LABEL="com.linfwang.telegram-codex-controller"

if [[ -n "${TGC_LAUNCHD_LABEL:-}" ]]; then
  LABEL="${TGC_LAUNCHD_LABEL}"
elif launchctl print "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1; then
  LABEL="${LEGACY_LABEL}"
else
  LABEL="${DEFAULT_LABEL}"
fi

if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  launchctl print "${DOMAIN}/${LABEL}" | sed -n '1,120p'
else
  echo "Service '${LABEL}' is not loaded in ${DOMAIN}." >&2
  if [ -f "${LOG_PATH}" ]; then
    echo >&2
    echo "Recent stderr (last 20 lines):" >&2
    tail -n 20 "${LOG_PATH}" >&2 || true
  fi
  exit 1
fi

if [ -f "${LOG_PATH}" ]; then
  echo
  echo "Recent stderr (last 20 lines):"
  tail -n 20 "${LOG_PATH}" || true
fi
