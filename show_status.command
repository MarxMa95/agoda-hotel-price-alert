#!/bin/zsh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
./scripts/status_launchd.sh
printf '\n网页地址: http://127.0.0.1:8767\n'
read '?按回车键关闭...'
