#!/bin/zsh
set -euo pipefail

LABEL="com.zhema.hotel-price-alert-agoda"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "Removed: ${LABEL}"
