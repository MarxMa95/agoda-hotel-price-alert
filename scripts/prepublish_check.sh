#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
cd "$APP_DIR"
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || { echo 'No usable python3 interpreter was found.'; exit 1; }
"$PYTHON_BIN" - <<'PY'
import re, subprocess, sys
from pathlib import Path
repo = Path.cwd()
inside_git = subprocess.run(['git','rev-parse','--is-inside-work-tree'], cwd=repo, capture_output=True).returncode == 0
tracked = []
if inside_git:
    tracked = [x for x in subprocess.run(['git','ls-files','-z'], cwd=repo, capture_output=True, check=True).stdout.decode('utf-8','ignore').split('\0') if x]
else:
    tracked = [p.relative_to(repo).as_posix() for p in repo.rglob('*') if p.is_file() and '.git/' not in p.as_posix()]
print('==> Starting pre-publish check')
sensitive = [p for p in tracked if p == 'data.db' or p.startswith(('logs/','session_profiles/','debug_screens/'))]
print('1/3 Runtime data check')
if sensitive:
    print('Found runtime data that should not be published:')
    [print(f'  - {x}') for x in sensitive[:50]]
else:
    print('  OK')
print('2/3 .gitignore check')
gitignore = (repo / '.gitignore').read_text(encoding='utf-8') if (repo / '.gitignore').exists() else ''
missing = [x for x in ['data.db','logs/','session_profiles/','debug_screens/'] if x not in gitignore]
if missing:
    [print(f'  - missing: {x}') for x in missing]
else:
    print('  OK')
print('3/3 Webhook scan')
patterns = [
    re.compile(r'https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]{20,}'),
    re.compile(r'https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9_-]{16,}'),
    re.compile(r'https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}'),
]
hits = []
for rel in tracked:
    if rel == 'data.db' or rel.startswith(('logs/','session_profiles/','debug_screens/')):
        continue
    text = (repo / rel).read_text(encoding='utf-8', errors='ignore')
    for pattern in patterns:
        for m in pattern.finditer(text):
            hits.append((rel, m.group(0)))
if hits:
    for rel, value in hits[:20]:
        print(f'  - {rel}: {value[:48]}...')
else:
    print('  OK')
if sensitive or missing or hits:
    print('Result: failed')
    sys.exit(1)
print('Result: passed')
PY
