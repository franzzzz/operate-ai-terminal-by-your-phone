#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${TGC_LAUNCHD_LABEL:-com.linfwang.telegram-codex-controller}"
PLIST_PATH="${TGC_LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
DOMAIN="gui/$(id -u)"

mkdir -p "${ROOT_DIR}/logs"

if [ ! -f "${PLIST_PATH}" ]; then
  echo "LaunchAgent plist not found: ${PLIST_PATH}" >&2
  echo "Copy docs/launchd.plist.example into ~/Library/LaunchAgents first, or override TGC_LAUNCHD_PLIST." >&2
  exit 1
fi

if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  echo "Restarting ${LABEL}..."
  launchctl kickstart -k "${DOMAIN}/${LABEL}"
else
  echo "Loading ${LABEL} from ${PLIST_PATH}..."
  launchctl bootstrap "${DOMAIN}" "${PLIST_PATH}"
fi

sleep 2
launchctl print "${DOMAIN}/${LABEL}" | sed -n '1,80p'
