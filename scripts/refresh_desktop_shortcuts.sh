#!/bin/zsh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DESKTOP_DIR="$HOME/Desktop"

mkdir -p "$DESKTOP_DIR"
cp -f "$APP_DIR/launch_agoda_latest.command" "$DESKTOP_DIR/Launch Agoda Hotel Alert.command"
cp -f "$APP_DIR/stop_launchd.command" "$DESKTOP_DIR/Stop Agoda Hotel Alert.command"
chmod +x "$DESKTOP_DIR/Launch Agoda Hotel Alert.command" "$DESKTOP_DIR/Stop Agoda Hotel Alert.command" 2>/dev/null || true
