#!/bin/zsh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
./scripts/refresh_desktop_shortcuts.sh
read '?按回车键关闭...'
