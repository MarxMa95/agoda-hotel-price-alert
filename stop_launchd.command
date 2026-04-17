#!/bin/zsh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
./scripts/uninstall_launchd.sh
read '?按回车键关闭...'
