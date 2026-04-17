#!/bin/zsh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
python3 ./scripts/doctor.py
read '?按回车键关闭...'
