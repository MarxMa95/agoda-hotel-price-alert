#!/bin/zsh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
./scripts/prepublish_check.sh
read '?Press Enter to close...'
