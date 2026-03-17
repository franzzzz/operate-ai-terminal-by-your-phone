#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

LABEL="${TGC_LAUNCHD_LABEL:-com.example.telegram-codex-controller}"
PLIST_PATH="${TGC_LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/${LABEL}.plist}"

./scripts/install.sh
./scripts/doctor.sh

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/.state"

if [[ "$(uname -s)" == "Darwin" ]]; then
  cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${ROOT_DIR}/scripts/run_service.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${ROOT_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${ROOT_DIR}/logs/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${ROOT_DIR}/logs/stderr.log</string>
</dict>
</plist>
EOF

  if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$(id -u)/${LABEL}"
  else
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
  fi

  cat <<EOF

Bootstrap complete.

LaunchAgent:
  ${PLIST_PATH}

Next steps in Telegram:
  1. Open a direct chat with the bot and send /start
  2. In your target supergroup, send /forum_on
  3. Send /forum_bootstrap

EOF
else
  cat <<'EOF'

Bootstrap created local dependencies and config, but did not install a background service.
Use docs/systemd.service.example on Linux or run the controller manually.

EOF
fi
