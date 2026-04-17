#!/bin/zsh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.zhema.hotel-price-alert-agoda"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$APP_DIR/logs"
START_SCRIPT="$APP_DIR/scripts/start.sh"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR" || {
  echo '创建 LaunchAgents 或日志目录失败'
  exit 1
}

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${START_SCRIPT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${APP_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/stderr.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" || exit 1
launchctl enable "gui/$(id -u)/${LABEL}" || exit 1
launchctl kickstart -k "gui/$(id -u)/${LABEL}" || exit 1

echo "Installed and started: ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Logs: ${LOG_DIR}"
