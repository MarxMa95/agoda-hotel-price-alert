#!/bin/zsh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
DOCTOR_SCRIPT="$APP_DIR/scripts/doctor.py"
INSTALL_SCRIPT="$APP_DIR/scripts/install_launchd.sh"
UNINSTALL_SCRIPT="$APP_DIR/scripts/uninstall_launchd.sh"
STATUS_SCRIPT="$APP_DIR/scripts/status_launchd.sh"
REFRESH_SHORTCUTS_SCRIPT="$APP_DIR/scripts/refresh_desktop_shortcuts.sh"
APP_URL="http://127.0.0.1:8767"
HEALTH_URL="$APP_URL/api/watchers"
VERSION_URL="$APP_URL/api/version"
EXPECTED_BUILD=$($PYTHON_BIN - <<'PY_EXPECTED'
from pathlib import Path
import re
app_py = Path('app.py')
text = app_py.read_text(encoding='utf-8')
match = re.search(r"APP_BUILD_VERSION\s*=\s*['"]([^'"]+)['"]", text)
print(match.group(1) if match else '')
PY_EXPECTED
)
cd "$APP_DIR" || exit 1
wait_for_service() {
  local i
  for i in {1..25}; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}
"$PYTHON_BIN" "$DOCTOR_SCRIPT" || exit 1
"$REFRESH_SHORTCUTS_SCRIPT" >/dev/null 2>&1 || true
"$UNINSTALL_SCRIPT" >/dev/null 2>&1 || true
pkill -f "$APP_DIR/app.py" >/dev/null 2>&1 || true
pkill -f "$APP_DIR/scripts/start.sh" >/dev/null 2>&1 || true
sleep 1
"$INSTALL_SCRIPT" || exit 1
wait_for_service || exit 1
echo "网页地址：$APP_URL"
open "$APP_URL"
read '?按回车键关闭...'
