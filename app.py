import json
import os
import random
import re
import shutil
import sqlite3
import ssl
import threading
import tempfile
import time
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright

try:
    import certifi
except Exception:
    certifi = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'data.db'
STATIC_DIR = BASE_DIR / 'static'
TEMPLATE_PATH = BASE_DIR / 'templates' / 'index.html'
POLL_INTERVAL_SECONDS = 300
DEFAULT_POLL_INTERVAL_MINUTES = 5
MIN_POLL_INTERVAL_MINUTES = 5
MAX_WATCHERS = 20
POLL_JITTER_SECONDS = 10
ROOM_SNIPPET_RADIUS = 1200
ROOM_PREVIEW_LIMIT = 12
PRICE_HISTORY_LIMIT = 30
DEFAULT_CHROME_USER_DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'Google' / 'Chrome'
DEFAULT_CHROME_PROFILE = 'Default'
APP_SESSION_PROFILE_ROOT = BASE_DIR / 'session_profiles'
APP_BUILD_VERSION = '2026-04-10-agoda-only-v1'
APP_SESSION_LOGIN_LOCK = threading.Lock()
APP_SESSION_LOGIN_THREADS: Dict[str, threading.Thread] = {}
APP_SESSION_LOGIN_STOP_EVENTS: Dict[str, threading.Event] = {}
APP_SESSION_LOGIN_PROCESSES: Dict[str, subprocess.Popen] = {}
APP_SESSION_DEBUG_PORTS: Dict[str, int] = {'agoda': 9333}
FIXED_SOURCE_TYPE = 'agoda'
APP_PORT = 8767
APP_SESSION_LOGIN_STATES: Dict[str, Dict[str, Any]] = {
    key: {
        'running': False,
        'target_url': '',
        'last_error': '',
        'last_started_at': None,
        'last_completed_at': None,
        'window_opened': False,
    }
    for key in ['agoda']
}

CREATE_WATCHERS_SQL = '''
CREATE TABLE IF NOT EXISTS watchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hotel_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    target_url TEXT NOT NULL,
    room_type_keyword TEXT NOT NULL DEFAULT '',
    room_type_meta TEXT NOT NULL DEFAULT '',
    price_pattern TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'CNY',
    notify_type TEXT NOT NULL DEFAULT 'feishu',
    notify_target TEXT NOT NULL,
    threshold_price REAL,
    min_expected_price REAL,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 5,
    request_headers TEXT NOT NULL DEFAULT '{}',
    use_local_chrome_profile INTEGER NOT NULL DEFAULT 0,
    chrome_profile_name TEXT NOT NULL DEFAULT 'Default',
    use_app_session_profile INTEGER NOT NULL DEFAULT 0,
    use_browser INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_price REAL,
    last_checked_at TEXT,
    last_notified_price REAL,
    last_price_note TEXT,
    all_time_low_price REAL,
    all_time_low_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
'''

CREATE_HISTORY_SQL = '''
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watcher_id INTEGER NOT NULL,
    price REAL NOT NULL,
    checked_at TEXT NOT NULL
);
'''

DEFAULT_PATTERNS = {
    'agoda': [
        r'"displayPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"grossPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"formattedDisplayPrice"\s*:\s*"?[A-Z¥￥$\s]*(\d+(?:\.\d+)?)"?',
        r'(?:CNY|RMB|¥|￥)\s*([0-9][0-9,]{2,}(?:\.\d+)?)',
        r'[¥￥]\s*(\d+(?:\.\d+)?)',
    ],
}

SOURCE_LABELS = {
    'agoda': 'Agoda',
}

SOURCE_TIPS = {
    'agoda': '建议直接复制 Agoda 上已选好日期和人数的房型页面链接。',
}

INDEX_HTML = TEMPLATE_PATH.read_text(encoding='utf-8') if TEMPLATE_PATH.exists() else ''
BROWSER_LOCK = threading.Lock()
PLAYWRIGHT_CACHE_DIR = Path.home() / 'Library' / 'Caches' / 'ms-playwright'
SYSTEM_BROWSER_CANDIDATES = [
    Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
    Path('/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
    Path('/Applications/Chromium.app/Contents/MacOS/Chromium'),
    Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'),
]


def _iter_browser_candidates(playwright: Any, prefer_system: bool = False) -> List[Path]:
    reported = Path(playwright.chromium.executable_path)
    cache_candidates: List[Path] = []
    if reported.exists():
        cache_candidates.append(reported)

    candidate_patterns = [
        'chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium',
        'chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium',
        'chromium-*/chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
        'chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
    ]
    for pattern in candidate_patterns:
        cache_candidates.extend(sorted(PLAYWRIGHT_CACHE_DIR.glob(pattern)))

    candidates = list(SYSTEM_BROWSER_CANDIDATES) + cache_candidates if prefer_system else cache_candidates + list(SYSTEM_BROWSER_CANDIDATES)
    seen = set()
    ordered: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if candidate.exists() and key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


def resolve_chromium_executable(playwright: Any, prefer_system: bool = False) -> str:
    candidates = _iter_browser_candidates(playwright, prefer_system=prefer_system)
    if candidates:
        return str(candidates[0])
    raise FileNotFoundError(f'未找到可用的 Chromium 或系统 Chrome，可执行目录：{PLAYWRIGHT_CACHE_DIR}')


def cleanup_persistent_profile_locks(profile_dir: Path) -> None:
    lock_names = [
        'SingletonLock',
        'SingletonCookie',
        'SingletonSocket',
        'DevToolsActivePort',
    ]
    for name in lock_names:
        target = profile_dir / name
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
        except Exception:
            pass


def utc_now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def parse_utc_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        normalized = str(value).replace(' UTC', '')
        return datetime.strptime(normalized, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def watcher_next_run_display(watcher: 'Watcher') -> Optional[str]:
    interval_seconds = max(60, int(watcher.poll_interval_minutes or DEFAULT_POLL_INTERVAL_MINUTES) * 60)
    last_checked = parse_utc_timestamp(watcher.last_checked_at)
    if last_checked is None:
        return None
    next_dt = last_checked.timestamp() + interval_seconds
    return datetime.fromtimestamp(next_dt).strftime('%Y-%m-%d %H:%M:%S UTC')


def build_ssl_context() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def http_open(request: urllib.request.Request, timeout: int = 15):
    context = build_ssl_context()
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, name: str, ddl: str) -> None:
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(watchers)').fetchall()}
    if name not in columns:
        conn.execute(f'ALTER TABLE watchers ADD COLUMN {ddl}')


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(CREATE_WATCHERS_SQL)
        conn.execute(CREATE_HISTORY_SQL)
        ensure_column(conn, 'room_type_keyword', "room_type_keyword TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, 'room_type_meta', "room_type_meta TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, 'price_pattern', "price_pattern TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, 'currency', "currency TEXT NOT NULL DEFAULT 'CNY'")
        ensure_column(conn, 'notify_type', "notify_type TEXT NOT NULL DEFAULT 'feishu'")
        ensure_column(conn, 'min_expected_price', 'min_expected_price REAL')
        ensure_column(conn, 'poll_interval_minutes', 'poll_interval_minutes INTEGER NOT NULL DEFAULT 5')
        ensure_column(conn, 'request_headers', "request_headers TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, 'use_local_chrome_profile', 'use_local_chrome_profile INTEGER NOT NULL DEFAULT 0')
        ensure_column(conn, 'chrome_profile_name', "chrome_profile_name TEXT NOT NULL DEFAULT 'Default'")
        ensure_column(conn, 'use_app_session_profile', 'use_app_session_profile INTEGER NOT NULL DEFAULT 0')
        ensure_column(conn, 'use_browser', 'use_browser INTEGER NOT NULL DEFAULT 1')
        ensure_column(conn, 'last_error', 'last_error TEXT')
        ensure_column(conn, 'last_price_note', 'last_price_note TEXT')
        ensure_column(conn, 'all_time_low_price', 'all_time_low_price REAL')
        ensure_column(conn, 'all_time_low_at', 'all_time_low_at TEXT')
        conn.execute(
            '''
            UPDATE watchers
            SET all_time_low_price = COALESCE(all_time_low_price, last_price),
                all_time_low_at = COALESCE(all_time_low_at, last_checked_at)
            WHERE all_time_low_price IS NULL AND last_price IS NOT NULL
            '''
        )
        conn.execute(
            '''
            UPDATE watchers
            SET all_time_low_price = (
                SELECT MIN(price) FROM price_history WHERE watcher_id = watchers.id
            )
            WHERE EXISTS (SELECT 1 FROM price_history WHERE watcher_id = watchers.id)
              AND (all_time_low_price IS NULL OR all_time_low_price > (
                SELECT MIN(price) FROM price_history WHERE watcher_id = watchers.id
              ))
            '''
        )
        conn.execute(
            '''
            UPDATE watchers
            SET all_time_low_at = (
                SELECT checked_at
                FROM price_history
                WHERE watcher_id = watchers.id
                  AND price = (
                    SELECT MIN(price) FROM price_history WHERE watcher_id = watchers.id
                  )
                ORDER BY id ASC
                LIMIT 1
            )
            WHERE EXISTS (SELECT 1 FROM price_history WHERE watcher_id = watchers.id)
              AND all_time_low_price IS NOT NULL
            '''
        )
        conn.commit()


@dataclass
class Watcher:
    id: int
    name: str
    hotel_name: str
    source_type: str
    target_url: str
    room_type_keyword: str
    room_type_meta: str
    price_pattern: str
    currency: str
    notify_type: str
    notify_target: str
    threshold_price: Optional[float]
    min_expected_price: Optional[float]
    poll_interval_minutes: int
    request_headers: str
    use_local_chrome_profile: int
    chrome_profile_name: str
    use_app_session_profile: int
    use_browser: int
    last_error: Optional[str]
    is_active: int
    last_price: Optional[float]
    last_checked_at: Optional[str]
    last_notified_price: Optional[float]
    last_price_note: Optional[str]
    all_time_low_price: Optional[float]
    all_time_low_at: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'Watcher':
        data = dict(row)
        data.setdefault('room_type_keyword', '')
        data.setdefault('room_type_meta', '')
        data.setdefault('price_pattern', '')
        data.setdefault('currency', 'CNY')
        data.setdefault('notify_type', 'feishu')
        data.setdefault('min_expected_price', None)
        data.setdefault('poll_interval_minutes', DEFAULT_POLL_INTERVAL_MINUTES)
        data.setdefault('request_headers', '{}')
        data.setdefault('use_local_chrome_profile', 0)
        data.setdefault('chrome_profile_name', DEFAULT_CHROME_PROFILE)
        data.setdefault('use_app_session_profile', 0)
        data.setdefault('use_browser', 1)
        data.setdefault('last_error', None)
        data.setdefault('last_price_note', None)
        data.setdefault('all_time_low_price', None)
        data.setdefault('all_time_low_at', None)
        return cls(**data)

    def parsed_headers(self) -> Dict[str, str]:
        try:
            parsed = json.loads(self.request_headers or '{}')
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def meta_tags(self) -> List[str]:
        if not self.room_type_meta.strip():
            return []
        return [item for item in self.room_type_meta.split(' | ') if item]


def list_watchers() -> List[Watcher]:
    with db_connection() as conn:
        rows = conn.execute('SELECT * FROM watchers WHERE source_type = ? ORDER BY id DESC', (FIXED_SOURCE_TYPE,)).fetchall()
    return [Watcher.from_row(row) for row in rows]


def find_watcher(watcher_id: int) -> Optional[Watcher]:
    for watcher in list_watchers():
        if watcher.id == watcher_id:
            return watcher
    return None


def list_history(watcher_id: int, limit: int = PRICE_HISTORY_LIMIT) -> List[Dict[str, Any]]:
    with db_connection() as conn:
        rows = conn.execute(
            'SELECT price, checked_at FROM price_history WHERE watcher_id = ? ORDER BY id DESC LIMIT ?',
            (watcher_id, limit),
        ).fetchall()
    items = [{'price': row['price'], 'checked_at': row['checked_at']} for row in reversed(rows)]
    return items


def append_price_history(watcher_id: int, price: float, checked_at: str) -> None:
    with db_connection() as conn:
        conn.execute(
            'INSERT INTO price_history (watcher_id, price, checked_at) VALUES (?, ?, ?)',
            (watcher_id, price, checked_at),
        )
        conn.execute(
            '''
            DELETE FROM price_history
            WHERE watcher_id = ? AND id NOT IN (
                SELECT id FROM price_history WHERE watcher_id = ? ORDER BY id DESC LIMIT ?
            )
            ''',
            (watcher_id, watcher_id, PRICE_HISTORY_LIMIT),
        )
        conn.commit()


def source_default_headers(source_type: str) -> Dict[str, str]:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 HotelPriceAlert/1.0 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'no-cache',
    }
    if source_type == 'agoda':
        headers['Referer'] = 'https://www.agoda.com/'
    return headers


def normalize_headers(raw_headers: Any, source_type: str) -> Dict[str, str]:
    headers = source_default_headers(source_type)
    if isinstance(raw_headers, str) and raw_headers.strip():
        parsed = json.loads(raw_headers)
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                headers[str(key)] = str(value)
    elif isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            headers[str(key)] = str(value)
    return headers


def merge_cookie_into_headers(headers: Dict[str, str], cookie_text: str) -> Dict[str, str]:
    cookie = re.sub(r'\s+', ' ', cookie_text).strip()
    if not cookie:
        return headers
    merged = dict(headers)
    merged['Cookie'] = cookie
    return merged



def is_allowed_agoda_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or '').lower()
    except Exception:
        return False
    return bool(host) and (host == 'agoda.com' or host.endswith('.agoda.com'))


def ensure_public_safe_target_url(url: str) -> str:
    normalized = normalize_target_url(url, 'agoda')
    if not is_allowed_agoda_url(normalized):
        raise ValueError('Open-source edition only supports Agoda hotel URLs on agoda.com.')
    return normalized


def clamp_public_poll_interval(value: Any) -> int:
    minutes = int(value or DEFAULT_POLL_INTERVAL_MINUTES)
    return max(MIN_POLL_INTERVAL_MINUTES, minutes)


def normalize_target_url(url: str, source_type: str, preferred_currency: str = 'CNY') -> str:
    if source_type != 'agoda':
        return url
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    currency = (params.get('currencyCode') or params.get('priceCur') or params.get('selectedcurrency') or params.get('currency') or preferred_currency).upper()
    params['currencyCode'] = currency
    params['priceCur'] = currency
    params['selectedcurrency'] = currency
    params['currency'] = currency
    if 'locale' not in params and '/zh-cn/' in parsed.path:
        params['locale'] = 'zh-cn'
    new_query = urllib.parse.urlencode(params)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


AGODA_CURRENCY_TEXTS = {
    'CNY': ['CNY', '人民币', '￥', '¥', 'Chinese Yuan', 'China Yuan', 'Chinese Yuan Renminbi'],
    'SGD': ['SGD', '新加坡元', 'Singapore Dollar'],
}


def read_agoda_display_currency(page: Any) -> str:
    try:
        body_text = page.locator('body').inner_text(timeout=4000)
    except Exception:
        body_text = ''
    lines = [re.sub(r'\s+', ' ', line).strip() for line in str(body_text).splitlines() if line.strip()]
    for index, line in enumerate(lines[:60]):
        normalized = line.strip().upper()
        if re.fullmatch(r'[A-Z]{3}', normalized):
            next_line = lines[index + 1] if index + 1 < len(lines) else ''
            if '币种' in next_line or 'currency' in next_line.lower():
                return normalized
    joined = ' | '.join(lines[:30])
    match = re.search(r'\b([A-Z]{3})\b\s*(?:\||｜)?\s*(?:选择您的币种|Select your currency)', joined, re.I)
    if match:
        return match.group(1).upper()
    try:
        value = page.evaluate(r"""
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll('button, a, div, span'));
  for (const node of nodes) {
    const text = norm(node.innerText || node.textContent || '');
    if (!text) continue;
    if (!/选择您的币种|select your currency/i.test(text)) continue;
    const parent = node.parentElement;
    const pool = [node, parent, parent && parent.previousElementSibling, node.previousElementSibling].filter(Boolean);
    for (const item of pool) {
      const raw = norm(item.innerText || item.textContent || '');
      const m = raw.match(/\b([A-Z]{3})\b/);
      if (m) return m[1];
    }
  }
  return '';
}
""")
        if isinstance(value, str) and re.fullmatch(r'[A-Z]{3}', value.strip().upper()):
            return value.strip().upper()
    except Exception:
        pass
    return ''


def agoda_click_text_candidates(page: Any, texts: List[str]) -> bool:
    for raw_text in texts:
        target = raw_text.strip()
        if not target:
            continue
        selectors = [
            f'text={target}',
            f'button:has-text("{target}")',
            f'a:has-text("{target}")',
            f'div:has-text("{target}")',
            f'span:has-text("{target}")',
            f'li:has-text("{target}")',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 6)
                for index in range(count):
                    item = locator.nth(index)
                    if item.is_visible(timeout=500):
                        item.click(timeout=1200)
                        return True
            except Exception:
                continue
        try:
            clicked = page.evaluate(r"""
(target) => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width >= 6 && rect.height >= 6;
  };
  const nodes = Array.from(document.querySelectorAll('button, a, div, span, li'));
  for (const node of nodes) {
    const text = norm(node.innerText || node.textContent || '');
    if (!text || !visible(node)) continue;
    if (text === target || text.startsWith(target + ' ') || text.includes(target)) {
      node.click();
      return true;
    }
  }
  return false;
}
""", target)
            if clicked:
                return True
        except Exception:
            pass
    return False


def agoda_open_currency_picker(page: Any) -> bool:
    try:
        clicked = page.evaluate(r"""
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width >= 8 && rect.height >= 8;
  };
  const nodes = Array.from(document.querySelectorAll('button, a, div, span'));
  const ranked = [];
  for (const node of nodes) {
    const text = norm(node.innerText || node.textContent || '');
    if (!text || !visible(node)) continue;
    const rect = node.getBoundingClientRect();
    if (rect.top > 180 || rect.left < 700) continue;
    let score = 0;
    if (/选择您的币种|select your currency/i.test(text)) score += 120;
    if (/\bRMB\b|\bCNY\b|\bSGD\b|人民币|新加坡元/i.test(text)) score += 80;
    if (score <= 0) continue;
    ranked.push({ node, score, top: rect.top, left: rect.left });
  }
  ranked.sort((a, b) => b.score - a.score || a.top - b.top || b.left - a.left);
  const hit = ranked[0];
  if (!hit) return false;
  hit.node.click();
  return true;
}
""")
        return bool(clicked)
    except Exception:
        return False


def agoda_jump_to_room_section(page: Any) -> bool:
    try:
        page.evaluate('window.scrollTo(0, 700)')
        page.wait_for_timeout(700)
    except Exception:
        pass
    try:
        clicked = page.evaluate(r"""
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width >= 8 && rect.height >= 8;
  };
  const navHints = ['简介', '客房', '设施服务', '住客评分', '位置', '政策'];
  const badHints = ['名大人', '间客房', '搜搜看', '选择您的币种', '登录', '注册', '输入住宿名'];
  const nodes = Array.from(document.querySelectorAll('button, a, div, span'));
  const ranked = [];
  for (const node of nodes) {
    const text = norm(node.innerText || node.textContent || '');
    if (!text || !visible(node)) continue;
    if (!(text === '客房' || text === 'Rooms')) continue;
    const rect = node.getBoundingClientRect();
    if (rect.top < 180 || rect.top > 820) continue;
    if (rect.left < 80 || rect.left > 900) continue;
    let container = node;
    let parentText = text;
    for (let i = 0; container && i < 4; i += 1, container = container.parentElement) {
      const maybe = norm(container.innerText || container.textContent || '');
      if (maybe && maybe.length < 200) parentText = maybe;
      if (navHints.filter(h => maybe.includes(h)).length >= 4) {
        parentText = maybe;
        break;
      }
    }
    const navHitCount = navHints.filter(h => parentText.includes(h)).length;
    if (navHitCount < 4) continue;
    if (badHints.some(h => parentText.includes(h))) continue;
    let score = 200 + navHitCount * 20;
    score -= Math.abs(rect.top - 360);
    ranked.push({ node, score, top: rect.top, left: rect.left, parentText });
  }
  ranked.sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);
  const hit = ranked[0];
  if (!hit) return false;
  hit.node.click();
  return true;
}
""")
        return bool(clicked)
    except Exception:
        return False


def ensure_agoda_currency(page: Any, preferred_currency: str = 'CNY') -> Dict[str, Any]:
    target = (preferred_currency or 'CNY').upper()
    info: Dict[str, Any] = {
        'target_currency': target,
        'before_currency': read_agoda_display_currency(page),
        'after_currency': '',
        'switched': False,
        'attempted': False,
    }
    if info['before_currency'] == target:
        info['after_currency'] = info['before_currency']
        return info

    target_candidates = AGODA_CURRENCY_TEXTS.get(target, [target])

    for _ in range(3):
        info['attempted'] = True
        opened = agoda_open_currency_picker(page)
        if not opened:
            try:
                page.mouse.click(980, 80)
                page.wait_for_timeout(500)
            except Exception:
                pass
        try:
            page.wait_for_timeout(800)
            for selector in ['input[type="search"]', 'input[placeholder*="币"]', 'input[placeholder*="curr"]', 'input[aria-label*="curr"]']:
                locator = page.locator(selector)
                if locator.count() and locator.first.is_visible(timeout=400):
                    locator.first.fill(target, timeout=1000)
                    page.wait_for_timeout(500)
                    break
        except Exception:
            pass
        if agoda_click_text_candidates(page, target_candidates):
            try:
                page.wait_for_load_state('networkidle', timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1800)
        after = read_agoda_display_currency(page)
        info['after_currency'] = after
        if after == target:
            info['switched'] = True
            return info
    if not info['after_currency']:
        info['after_currency'] = read_agoda_display_currency(page)
    return info




def agoda_keyword_option_count(body_text: str, room_keyword: str) -> int:
    if not body_text or not room_keyword.strip():
        return 0
    best = 0
    for variant in [room_keyword.strip()] + keyword_variants(room_keyword)[:8]:
        if len(variant.strip()) < 2:
            continue
        for match in re.finditer(re.escape(variant), body_text, re.I):
            start = max(0, match.start() - 20)
            end = min(len(body_text), match.end() + 2200)
            snippet = body_text[start:end]
            if agoda_snippet_is_review_like(snippet) and not agoda_snippet_has_room_price_context(snippet):
                continue
            count = len(re.findall(r'含税单价|客房x1|申请预订|马上预订', snippet))
            if count > best:
                best = count
    return best


def agoda_snippet_is_review_like(snippet: str) -> bool:
    if not snippet:
        return False
    review_markers = [
        '点评日期', '显示原文', '回复日期', '您觉得此点评是否有用', '通过生成式', '自动翻译',
        '来自中国', '来自土耳其', '来自', '入住了', '惊艳了', '还不错', '极好！',
    ]
    hits = sum(1 for marker in review_markers if marker in snippet)
    return hits >= 2


def agoda_snippet_has_room_price_context(snippet: str) -> bool:
    if not snippet:
        return False
    room_markers = [
        '含税单价', '含税/费', '含税费', '客房x1', '申请预订', '马上预订并付款',
        '免费取消', '不可取消', '早餐', '到店付', '在线付', '预付', '选项',
    ]
    hits = sum(1 for marker in room_markers if marker in snippet)
    return hits >= 2 or ('含税单价' in snippet and ('客房x1' in snippet or '申请预订' in snippet or '马上预订' in snippet))


def agoda_keyword_has_price_context(body_text: str, room_keyword: str) -> bool:
    if not body_text or not room_keyword.strip():
        return False
    for variant in [room_keyword.strip()] + keyword_variants(room_keyword)[:8]:
        if len(variant.strip()) < 2:
            continue
        for match in re.finditer(re.escape(variant), body_text, re.I):
            start = max(0, match.start() - 120)
            end = min(len(body_text), match.end() + 1600)
            snippet = body_text[start:end]
            if agoda_snippet_is_review_like(snippet) and not agoda_snippet_has_room_price_context(snippet):
                continue
            if agoda_snippet_has_room_price_context(snippet):
                return True
    return False


def agoda_focus_room_keyword(page: Any, room_keyword: str) -> Dict[str, Any]:
    keyword = room_keyword.strip()
    info: Dict[str, Any] = {
        'room_keyword': keyword,
        'attempted': False,
        'clicked_text': '',
        'focused': False,
        'had_price_context_before': False,
        'had_price_context_after': False,
        'option_count_before': 0,
        'option_count_after': 0,
        'scroll_steps': 0,
    }
    if not keyword:
        return info

    try:
        body_text = page.locator('body').inner_text(timeout=5000)
    except Exception:
        body_text = ''
    info['had_price_context_before'] = agoda_keyword_has_price_context(body_text, keyword)
    info['option_count_before'] = agoda_keyword_option_count(body_text, keyword)
    if info['had_price_context_before']:
        info['focused'] = True
        info['had_price_context_after'] = info['had_price_context_before']
        info['option_count_after'] = info['option_count_before']
        return info

    try:
        agoda_jump_to_room_section(page)
        info['attempted'] = True
        info['clicked_text'] = '客房'
        page.wait_for_timeout(2500)
    except Exception:
        pass

    for step in range(10):
        try:
            body_text = page.locator('body').inner_text(timeout=5000)
        except Exception:
            body_text = ''
        info['had_price_context_after'] = agoda_keyword_has_price_context(body_text, keyword)
        info['option_count_after'] = agoda_keyword_option_count(body_text, keyword)
        if info['had_price_context_after']:
            info['focused'] = True
            info['scroll_steps'] = step
            return info
        try:
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(1200)
        except Exception:
            break

    try:
        body_text = page.locator('body').inner_text(timeout=5000)
    except Exception:
        body_text = ''
    info['had_price_context_after'] = agoda_keyword_has_price_context(body_text, keyword)
    info['option_count_after'] = agoda_keyword_option_count(body_text, keyword)
    info['focused'] = info['had_price_context_after']
    return info


def create_watcher(payload: Dict[str, Any]) -> int:
    now = utc_now()
    source_type = payload['source_type'].strip()
    payload['target_url'] = ensure_public_safe_target_url(str(payload.get('target_url', '')).strip())
    normalized_headers = normalize_headers(payload.get('request_headers', '{}'), source_type)
    normalized_headers = merge_cookie_into_headers(normalized_headers, str(payload.get('cookie', '')))
    request_headers = json.dumps(normalized_headers, ensure_ascii=False)
    with db_connection() as conn:
        cursor = conn.execute(
            '''
            INSERT INTO watchers (
                name, hotel_name, source_type, target_url, room_type_keyword,
                room_type_meta, price_pattern, currency, notify_type, notify_target, threshold_price, min_expected_price, poll_interval_minutes,
                request_headers, use_local_chrome_profile, chrome_profile_name, use_app_session_profile, use_browser, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                payload['name'].strip(),
                payload['hotel_name'].strip(),
                source_type,
                payload['target_url'].strip(),
                payload.get('room_type_keyword', '').strip(),
                payload.get('room_type_meta', '').strip(),
                payload.get('price_pattern', '').strip(),
                payload.get('currency', 'CNY').strip() or 'CNY',
                payload.get('notify_type', 'feishu').strip() or 'feishu',
                payload['notify_target'].strip(),
                payload.get('threshold_price'),
                payload.get('min_expected_price'),
                clamp_public_poll_interval(payload.get('poll_interval_minutes', DEFAULT_POLL_INTERVAL_MINUTES)),
                request_headers,
                1 if bool(payload.get('use_local_chrome_profile')) else 0,
                str(payload.get('chrome_profile_name') or DEFAULT_CHROME_PROFILE).strip() or DEFAULT_CHROME_PROFILE,
                1 if bool(payload.get('use_app_session_profile')) else 0,
                1 if bool(payload.get('use_browser', True)) else 0,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)




def update_watcher(watcher_id: int, payload: Dict[str, Any]) -> None:
    now = utc_now()
    source_type = payload['source_type'].strip()
    payload['target_url'] = ensure_public_safe_target_url(str(payload.get('target_url', '')).strip())
    normalized_headers = normalize_headers(payload.get('request_headers', '{}'), source_type)
    normalized_headers = merge_cookie_into_headers(normalized_headers, str(payload.get('cookie', '')))
    request_headers = json.dumps(normalized_headers, ensure_ascii=False)
    with db_connection() as conn:
        conn.execute(
            '''
            UPDATE watchers
            SET name = ?, hotel_name = ?, source_type = ?, target_url = ?, room_type_keyword = ?,
                room_type_meta = ?, price_pattern = ?, currency = ?, notify_type = ?, notify_target = ?,
                threshold_price = ?, min_expected_price = ?, poll_interval_minutes = ?, request_headers = ?,
                use_local_chrome_profile = ?, chrome_profile_name = ?, use_app_session_profile = ?, use_browser = ?, updated_at = ?
            WHERE id = ?
            ''',
            (
                payload['name'].strip(),
                payload['hotel_name'].strip(),
                source_type,
                payload['target_url'].strip(),
                payload.get('room_type_keyword', '').strip(),
                payload.get('room_type_meta', '').strip(),
                payload.get('price_pattern', '').strip(),
                payload.get('currency', 'CNY').strip() or 'CNY',
                payload.get('notify_type', 'feishu').strip() or 'feishu',
                payload['notify_target'].strip(),
                payload.get('threshold_price'),
                payload.get('min_expected_price'),
                clamp_public_poll_interval(payload.get('poll_interval_minutes', DEFAULT_POLL_INTERVAL_MINUTES)),
                request_headers,
                1 if bool(payload.get('use_local_chrome_profile')) else 0,
                str(payload.get('chrome_profile_name') or DEFAULT_CHROME_PROFILE).strip() or DEFAULT_CHROME_PROFILE,
                1 if bool(payload.get('use_app_session_profile')) else 0,
                1 if bool(payload.get('use_browser', True)) else 0,
                now,
                watcher_id,
            ),
        )
        conn.commit()
def set_watcher_active(watcher_id: int, is_active: int) -> None:
    with db_connection() as conn:
        conn.execute('UPDATE watchers SET is_active = ?, updated_at = ? WHERE id = ?', (is_active, utc_now(), watcher_id))
        conn.commit()


def delete_watcher(watcher_id: int) -> None:
    with db_connection() as conn:
        conn.execute('DELETE FROM price_history WHERE watcher_id = ?', (watcher_id,))
        conn.execute('DELETE FROM watchers WHERE id = ?', (watcher_id,))
        conn.commit()


def update_check_result(watcher_id: int, price: Optional[float], should_notify: bool, error: Optional[str] = None, price_note: Optional[str] = None) -> None:
    now = utc_now()
    with db_connection() as conn:
        current = conn.execute('SELECT all_time_low_price, all_time_low_at FROM watchers WHERE id = ?', (watcher_id,)).fetchone()
        next_low_price = current['all_time_low_price'] if current else None
        next_low_at = current['all_time_low_at'] if current else None
        if price is not None and error is None and (next_low_price is None or float(price) < float(next_low_price)):
            next_low_price = price
            next_low_at = now
        if should_notify and price is not None:
            conn.execute(
                'UPDATE watchers SET last_price = ?, last_checked_at = ?, last_notified_price = ?, last_error = ?, last_price_note = ?, all_time_low_price = ?, all_time_low_at = ?, updated_at = ? WHERE id = ?',
                (price, now, price, error, price_note, next_low_price, next_low_at, now, watcher_id),
            )
        else:
            conn.execute(
                'UPDATE watchers SET last_price = ?, last_checked_at = ?, last_error = ?, last_price_note = ?, all_time_low_price = ?, all_time_low_at = ?, updated_at = ? WHERE id = ?',
                (price, now, error, price_note, next_low_price, next_low_at, now, watcher_id),
            )
        conn.commit()
    if price is not None and error is None:
        append_price_history(watcher_id, price, now)


def fetch_text(url: str, headers: Dict[str, str]) -> str:
    request = urllib.request.Request(url, headers=headers)
    with http_open(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or 'utf-8'
        return response.read().decode(charset, errors='ignore')


def extract_legacy_room_blocks(page: Any) -> List[Dict[str, Any]]:
    script = r"""
() => {
  const nodes = Array.from(document.querySelectorAll('*'));
  const roomHint = /(房|床|套房|双早|大床|双床|Deluxe|King|Twin|Suite|Room|Villa|泳池|水疗|Spa|Pool)/i;
  const todayPriceHint = /(今日价格|选择房间|起订|预订)/i;
  const taxHint = /(含税\/?费|含税费|税费均|含税\/费\s*均)/i;
  const breakfastHint = /(含早|双早|单早|早餐|Breakfast)/i;
  const cancelHint = /(免费取消|不可取消|不可退款|免费退|No refund|Free cancellation)/i;
  const payHint = /(到店付|在线付|预付|Pay at property|Prepay)/i;
  const badRoomNameHint = /(photo gallery|照片|查看所有|Go to main content|房型摘要|联系客服|我的订单|房间详情 房型摘要)/i;
  const badPriceContextHint = /(张照片|平方米|m²|平米|楼层|邮编|地址|Batok Bay|Wi-Fi|可住人数|1张|2张|3张|4张)/i;
  const currencyPriceRegex = /[¥￥]\s*([0-9][0-9,]{2,}(?:\.\d{1,2})?)/g;

  const pickText = (node) => {
    if (!node) return '';
    const preferred = [
      node.innerText || '',
      node.getAttribute?.('aria-label') || '',
      node.getAttribute?.('title') || '',
      node.getAttribute?.('data-title') || '',
      node.getAttribute?.('alt') || '',
      node.textContent || '',
    ];
    for (const value of preferred) {
      const text = String(value || '').replace(/\u00a0/g, ' ').trim();
      if (text) return text;
    }
    return '';
  };

  const normalizeValue = (raw) => Number(String(raw || '').replace(/,/g, '').replace(/[^\d.]/g, ''));

  const currencyPricesWithPos = (text) => {
    const items = [];
    for (const match of String(text || '').matchAll(currencyPriceRegex)) {
      const value = normalizeValue(match[1]);
      if (value >= 500 && value <= 100000) {
        items.push({ value, pos: match.index || 0 });
      }
    }
    return items;
  };

  const nearestFollowingPrice = (text, hintRegex, maxDistance = 80) => {
    const hints = Array.from(String(text || '').matchAll(new RegExp(hintRegex.source, 'ig')));
    const prices = currencyPricesWithPos(text);
    const results = [];
    for (const hint of hints) {
      const hintPos = hint.index || 0;
      const candidate = prices.find((item) => item.pos >= hintPos && item.pos - hintPos <= maxDistance);
      if (candidate) {
        const near = String(text || '').slice(Math.max(0, hintPos - 12), Math.min(String(text || '').length, candidate.pos + 24));
        if (!badPriceContextHint.test(near)) results.push(candidate.value);
      }
    }
    return results;
  };

  const collectPriceCandidates = (text) => {
    const sourceText = String(text || '');
    const candidates = [];

    for (const value of nearestFollowingPrice(sourceText, taxHint, 100)) {
      candidates.push({ value, score: 100, source: 'tax_included' });
    }
    for (const value of nearestFollowingPrice(sourceText, todayPriceHint, 80)) {
      candidates.push({ value, score: 80, source: 'today_price' });
    }

    const prices = currencyPricesWithPos(sourceText);
    for (const item of prices) {
      const near = sourceText.slice(Math.max(0, item.pos - 24), Math.min(sourceText.length, item.pos + 24));
      let score = 10;
      if (taxHint.test(near)) score += 30;
      if (todayPriceHint.test(near)) score += 18;
      if (badPriceContextHint.test(near)) score -= 40;
      candidates.push({ value: item.value, score, source: 'currency' });
    }

    candidates.sort((a, b) => (b.score - a.score) || (a.value - b.value));
    return candidates;
  };

  const findRoomName = (text) => {
    const lines = String(text || '').split(/\n+/).map(s => s.trim()).filter(Boolean);
    for (const line of lines) {
      if (line.length > 80) continue;
      if (badRoomNameHint.test(line)) continue;
      if (roomHint.test(line)) return line;
    }
    return '';
  };

  const seen = new Set();
  const results = [];
  const pushResult = (roomName, blockText, tags) => {
    if (!roomName || badRoomNameHint.test(roomName)) return;
    const candidates = collectPriceCandidates(blockText).filter((item) => item.value >= 500);
    if (!candidates.length) return;
    const best = candidates[0];
    const key = `${roomName}__${best.value}`;
    if (seen.has(key)) return;
    seen.add(key);
    results.push({
      room_name: roomName,
      price: best.value,
      tags,
      raw_text: String(blockText || '').slice(0, 420) + ` [price_source=${best.source}; candidates=${candidates.slice(0, 4).map(item => item.value).join('/')}]`,
    });
  };

  for (const node of nodes) {
    const selfText = pickText(node);
    if (!selfText) continue;
    if (!roomHint.test(selfText) && !todayPriceHint.test(selfText) && !taxHint.test(selfText)) continue;

    let container = node;
    let bestText = selfText;
    let depth = 0;
    while (container && depth < 6) {
      const currentText = pickText(container);
      if (currentText && currentText.length <= 1800) bestText = currentText;
      if (roomHint.test(bestText) && (todayPriceHint.test(bestText) || taxHint.test(bestText))) break;
      container = container.parentElement;
      depth += 1;
    }

    const roomName = findRoomName(bestText) || findRoomName(selfText);
    if (!roomName) continue;

    const tags = [];
    const breakfast = bestText.match(breakfastHint);
    const cancel = bestText.match(cancelHint);
    const pay = bestText.match(payHint);
    if (breakfast) tags.push(breakfast[1]);
    if (cancel) tags.push(cancel[1]);
    if (pay) tags.push(pay[1]);

    pushResult(roomName, bestText, tags);
  }

  return results.slice(0, 30);
}
"""
    try:
        items = page.evaluate(script)
        return items if isinstance(items, list) else []
    except Exception:
        return []

def extract_agoda_room_blocks(page: Any, room_keyword: str = '') -> List[Dict[str, Any]]:
    def parse_display_price(value: Any) -> Optional[float]:
        text = str(value or '').strip()
        if not text:
            return None
        normalized = re.sub(r'[^\d.]', '', text.replace(',', ''))
        if not normalized:
            return None
        try:
            amount = float(normalized)
        except Exception:
            return None
        return amount if 500 <= amount <= 100000 else None

    def extract_from_property_page_params() -> List[Dict[str, Any]]:
        try:
            payload = page.evaluate(r"""
() => {
  const root = window.propertyPageParams?.roomGridData?.masterRooms;
  if (!Array.isArray(root)) return [];
  return root.map((room, roomIndex) => ({
    roomIndex,
    name: room?.name || '',
    recommendedRoomName: room?.recommendedRoomName || '',
    cheapestPrice: Number(room?.cheapestPrice || 0),
    rooms: Array.isArray(room?.rooms) ? room.rooms.map((offer, offerIndex) => ({
      offerIndex,
      name: offer?.name || '',
      isBreakfastIncluded: !!offer?.isBreakfastIncluded,
      isFreeCancellation: !!offer?.isFreeCancellation,
      displayPrice: Number(offer?.pricing?.displayPrice || 0),
      perNightInclusive: Number(offer?.pricing?.displaySummary?.perRoomPerNight?.displayTotal?.allInclusive || 0),
      perNightExclusive: Number(offer?.pricing?.displaySummary?.perRoomPerNight?.displayTotal?.exclusive || 0),
      perNightAfterCashback: Number(offer?.pricing?.displaySummary?.perRoomPerNight?.displayAfterCashback?.allInclusive || 0),
      totalInclusive: Number(offer?.pricing?.displaySummary?.perBook?.displayTotal?.allInclusive || 0),
      benefitTitles: Array.isArray(offer?.benefits) ? offer.benefits.map(item => item?.title).filter(Boolean) : [],
      formattedAgodaPrice: offer?.pricePopupViewModel?.formattedAgodaPrice || '',
      formattedChargePriceAmount: offer?.pricePopupViewModel?.formattedChargePriceAmount || '',
      formattedTaxesAndFeesAmount: offer?.pricePopupViewModel?.formattedTaxesAndFeesAmount || '',
    })) : [],
  }));
}
""")
        except Exception:
            return []

        keyword_variants_lower = [item.lower() for item in keyword_variants(room_keyword)] if room_keyword.strip() else []
        entries: List[Dict[str, Any]] = []
        for room in payload or []:
            room_name = str(room.get('name') or room.get('recommendedRoomName') or '').strip()
            room_haystack = ' '.join(filter(None, [room_name, str(room.get('recommendedRoomName') or '')])).lower()
            target_match = bool(keyword_variants_lower) and any(variant in room_haystack for variant in keyword_variants_lower)
            offers = room.get('rooms') or []
            for offer in offers:
                raw_price = float(offer.get('perNightInclusive') or offer.get('displayPrice') or 0)
                display_price = parse_display_price(offer.get('formattedAgodaPrice'))
                display_charge_price = parse_display_price(offer.get('formattedChargePriceAmount'))
                precise_price = float(display_price or display_charge_price or raw_price or 0)
                price = float(int(precise_price))
                if not (500 <= price <= 100000):
                    continue
                tags: List[str] = []
                if bool(offer.get('isBreakfastIncluded')):
                    tags.append('早餐')
                if bool(offer.get('isFreeCancellation')):
                    tags.append('免费取消')
                for benefit in offer.get('benefitTitles') or []:
                    benefit_text = str(benefit).strip()
                    if benefit_text and benefit_text not in tags:
                        tags.append(benefit_text)
                raw_text = ' | '.join(filter(None, [
                    room_name,
                    f"offer={offer.get('offerIndex')}",
                    f"display_price={price:.0f}",
                    f"precise_price={precise_price:.2f}",
                    f"exclusive={float(offer.get('perNightExclusive') or 0):.2f}",
                    f"after_cashback={float(offer.get('perNightAfterCashback') or 0):.2f}",
                    f"total={float(offer.get('totalInclusive') or 0):.2f}",
                    ','.join(tags),
                    f"ui_price={str(offer.get('formattedAgodaPrice') or '')}",
                    f"ui_charge={str(offer.get('formattedChargePriceAmount') or '')}",
                ]))
                entries.append({
                    'room_name': room_name or str(offer.get('name') or room_keyword).strip(),
                    'price': price,
                    'tags': tags,
                    'raw_text': f'{raw_text} [price_source=propertyPageParams]',
                    'target_match': target_match,
                })
        entries.sort(key=lambda item: (0 if item.get('target_match') else 1, float(item.get('price') or 0)))
        return entries[:60]

    def normalize_text(value: str) -> str:
        return re.sub(r'\s+', ' ', str(value or '').replace(' ', ' ')).strip()

    def split_lines(value: str) -> List[str]:
        return [normalize_text(line) for line in re.split(r'\n+', str(value or '')) if normalize_text(line)]

    def extract_prices_near_tax(lines: List[str]) -> List[float]:
        prices: List[float] = []
        for index, line in enumerate(lines):
            if not re.search(r'含税单价|含税/费|含税费|税费', line, re.I):
                continue
            for offset in [0, 1, 2, 3, 4]:
                candidate_line = lines[index - offset] if index - offset >= 0 else ''
                for match in re.finditer(r'[¥￥]\s*([0-9][0-9,]{2,}(?:\.\d{1,2})?)', candidate_line):
                    value = float(match.group(1).replace(',', ''))
                    if 500 <= value <= 100000:
                        prices.append(value)
                for match in re.finditer(r'(^|\b)([0-9][0-9,]{2,}(?:\.\d{1,2})?)(\b|$)', candidate_line):
                    value = float(match.group(2).replace(',', ''))
                    if 500 <= value <= 100000:
                        prices.append(value)
        seen: set = set()
        result: List[float] = []
        for value in sorted(prices):
            key = round(value, 2)
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    def collect_tags(block: str) -> List[str]:
        tags: List[str] = []
        for pattern in [r'(含早|双早|单早|早餐|Breakfast|含优质早餐)', r'(免费取消|不可取消|不可退款|免费退|No refund|Free cancellation)', r'(到店付|在线付|预付|Pay at property|Prepay|立即预订|马上预订并付款)']:
            match = re.search(pattern, block, re.I)
            if match:
                tags.append(match.group(1))
        return tags

    def find_room_name(lines: List[str], keyword: str) -> str:
        keyword_lower = keyword.lower().strip()
        for line in lines:
            if keyword_lower and keyword_lower in line.lower():
                return line
        for line in lines:
            if len(line) > 120:
                continue
            if re.search(r'一起订更划算|中文客服|选择您的语言|搜搜看|主页|设施服务|旅行推荐|交通|简介|住客评分|点评语言|住客类型', line, re.I):
                continue
            if re.search(r'房|床|套房|别墅|Villa|Suite|King|Twin|Deluxe|Pool|Spa|泳池|水疗', line, re.I):
                return line
        return keyword.strip()

    try:
        structured_entries = extract_from_property_page_params()
        if structured_entries:
            return structured_entries
        body_text = page.locator('body').inner_text(timeout=8000)
    except Exception:
        return []

    body_text = str(body_text or '')
    if not body_text.strip():
        return []

    if room_keyword.strip():
        keyword = room_keyword.strip()
        lower_text = body_text.lower()
        lower_keyword = keyword.lower()
        blocks: List[Tuple[int, str, int]] = []
        offset = 0
        while True:
            index = lower_text.find(lower_keyword, offset)
            if index < 0:
                break
            prev_marker = body_text.rfind('更多客房图片和详情', 0, index)
            start = prev_marker if prev_marker >= 0 else max(0, index - 500)
            next_room = body_text.find('更多客房图片和详情', index + len(keyword))
            next_faq = body_text.find('常见问题', index)
            candidates = [pos for pos in [next_room, next_faq] if pos >= 0]
            end = min(candidates) if candidates else min(len(body_text), index + 3200)
            block = body_text[start:end]
            score = 0
            if agoda_snippet_is_review_like(block) and not agoda_snippet_has_room_price_context(block):
                score -= 400
            if re.search(r'含税单价|含税/费|含税费|税费', block, re.I):
                score += 100
            if re.search(r'客房x\d+|预订|申请预订|马上预订|立即预订|选项\s*\d+', block, re.I):
                score += 80
            if '详情' in block:
                score += 40
            if re.search(r'点评日期|显示原文|回复日期|通过生成式', block) and not re.search(r'含税单价', block):
                score -= 200
            blocks.append((score, block, index))
            offset = index + len(keyword)

        entries: List[Dict[str, Any]] = []
        for score, block, _ in sorted(blocks, key=lambda item: (-item[0], item[2])):
            if score < 150:
                continue
            lines = split_lines(block)
            room_name = find_room_name(lines, keyword)
            room_index = next((i for i, line in enumerate(lines) if keyword.lower() in line.lower()), 0)
            scoped_lines = lines[room_index: room_index + 90]
            scoped_block = '\n'.join(scoped_lines)
            prices = extract_prices_near_tax(scoped_lines)
            if not prices:
                continue
            tags = collect_tags(scoped_block)
            normalized_block = normalize_text(scoped_block)[:2600]
            for price in prices:
                entries.append({
                    'room_name': room_name,
                    'price': price,
                    'tags': tags,
                    'raw_text': f"{normalized_block} [price_source=body_keyword_block; candidates={'/'.join(f'{p:.0f}' for p in prices)}; score={score}]",
                })
            if entries:
                dedup: List[Dict[str, Any]] = []
                seen = set()
                for item in entries:
                    key = (item['room_name'], round(float(item['price']), 2))
                    if key in seen:
                        continue
                    seen.add(key)
                    dedup.append(item)
                dedup.sort(key=lambda item: float(item['price']))
                return dedup[:20]

    return []


def extract_room_blocks_for_source(page: Any, source_type: str, room_keyword: str = '') -> List[Dict[str, Any]]:
    if source_type == 'agoda':
        return extract_agoda_room_blocks(page, room_keyword)
    return []


def detect_page_signals(source_type: str, final_url: str, title: str, lower_preview: str, room_block_count: int) -> Dict[str, Any]:
    risk_like = any(token in lower_preview for token in ['验证', 'captcha', '风控', '访问异常', '稍后再试'])
    booking_like = False
    if source_type == 'agoda':
        agoda_login_tokens = ['继续使用google', '继续使用 google', '电子邮箱地址', '邮箱地址', '手机号码', '输入验证码', '登录 agoda', 'sign in', 'continue with google']
        host_login_like = any(token in final_url for token in ['agoda.com/login', 'auth.agoda.com'])
        preview_login_hits = sum(1 for token in agoda_login_tokens if token in lower_preview)
        hotel_page_tokens = ['选择您的币种', '搜搜看', '一起订更划算', '客房', '主页']
        looks_like_hotel_page = any(token in lower_preview for token in hotel_page_tokens)
        header_guest_like = ('登录' in lower_preview and '注册' in lower_preview)
        login_like = host_login_like or header_guest_like or (preview_login_hits >= 3 and not looks_like_hotel_page)
    else:
        login_like = any(token in lower_preview for token in ['登录', 'login', '手机号', '验证', '验证码'])
    return {
        'login_like': login_like,
        'risk_like': risk_like,
        'booking_like': booking_like,
        'empty_room_like': room_block_count == 0,
    }


def encode_room_blocks(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ''
    return '\n'.join([
        f"ROOM_BLOCK||{item.get('room_name', '')}||{item.get('price', '')}||{' | '.join(item.get('tags', []))}||{item.get('raw_text', '')}"
        for item in items
    ])


def encode_page_debug(debug_info: Dict[str, Any]) -> str:
    if not debug_info:
        return ''
    payload = json.dumps(debug_info, ensure_ascii=False)
    return f"PAGE_DEBUG||{payload}"


def parse_room_blocks(text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith('ROOM_BLOCK||'):
            continue
        parts = line.split('||', 4)
        if len(parts) < 5:
            continue
        room_name = parts[1].strip()
        price_text = parts[2].strip()
        tags_text = parts[3].strip()
        raw_text = parts[4].strip()
        try:
            price = float(price_text.replace(',', ''))
        except ValueError:
            continue
        items.append({'room_name': room_name, 'price': price, 'tags': [tag for tag in tags_text.split(' | ') if tag], 'raw_text': raw_text})
    return items


def parse_page_debug(text: str) -> Dict[str, Any]:
    for line in text.splitlines():
        if not line.startswith('PAGE_DEBUG||'):
            continue
        raw = line.split('||', 1)[1]
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def room_like_lines(text: str) -> List[str]:
    text = search_text_only(text)
    lines = [normalize_room_name(line) for line in text.splitlines()]
    lines = [line for line in lines if line and not looks_garbled(line)]
    room_hint = re.compile(r'(房|床|套房|双早|大床|双床|Deluxe|King|Twin|Suite|Room|Villa|泳池|水疗|Spa|Pool)', re.I)
    results = []
    seen = set()
    for index, line in enumerate(lines):
        if not room_hint.search(line):
            continue
        block = ' '.join(lines[max(0, index - 1): index + 3])
        if block in seen:
            continue
        seen.add(block)
        results.append(block)
        if len(results) >= 20:
            break
    return results


def room_candidate_summaries(text: str, limit: int = 8) -> List[str]:
    block_items = parse_room_blocks(text)
    search_text = search_text_only(text)
    summaries: List[str] = []
    if block_items:
        for item in block_items[:limit]:
            room_name = str(item.get('room_name', '')).strip()
            price = item.get('price')
            tags = item.get('tags', []) or []
            tag_text = f" [{' / '.join(tags)}]" if tags else ''
            if room_name:
                summaries.append(f"{room_name} ¥{price}{tag_text}")
        if summaries:
            return summaries

    for line in room_like_lines(text)[:limit]:
        summaries.append(line[:100])
    return summaries


def raw_html_keyword_snippets(text: str, room_keyword: str, limit: int = 4) -> List[str]:
    marker = '\n\n<!--RAW_HTML-->\n'
    if marker not in text:
        return []
    raw_html = text.split(marker, 1)[1]
    snippets: List[str] = []
    seen = set()
    for variant in keyword_variants(room_keyword)[:6]:
        try:
            pattern = re.escape(variant)
            match = re.search(pattern, raw_html, re.I)
        except re.error:
            match = None
        if not match:
            continue
        start = max(0, match.start() - 500)
        end = min(len(raw_html), match.end() + 1800)
        snippet = raw_html[start:end]
        snippet = re.sub(r'\s+', ' ', snippet).strip()
        if snippet and snippet not in seen:
            seen.add(snippet)
            snippets.append(snippet[:2200])
        if len(snippets) >= limit:
            break
    return snippets


def build_room_debug_payload(text: str, room_keyword: str, watcher: Optional[Watcher] = None) -> Dict[str, Any]:
    page_debug = parse_page_debug(text)
    matched_blocks: List[Dict[str, Any]] = []
    if watcher is not None:
        try:
            matched_blocks = matched_room_blocks(text, watcher)[:10]
        except Exception:
            matched_blocks = []
    snippets: List[str] = []
    for variant in keyword_variants(room_keyword)[:6]:
        search_text = search_text_only(text)
        try:
            match = re.search(re.escape(variant), search_text, re.I)
        except re.error:
            match = None
        if not match:
            continue
        start = max(0, match.start() - 220)
        end = min(len(search_text), match.end() + 380)
        snippet = search_text[start:end].replace('\n', ' ')
        snippet = re.sub(r'\s+', ' ', snippet).strip()
        if snippet and snippet not in snippets:
            snippets.append(snippet[:600])
        if len(snippets) >= 4:
            break
    return {
        'room_keyword': room_keyword,
        'keyword_variants': keyword_variants(room_keyword),
        'room_candidates': room_candidate_summaries(text, 10),
        'room_like_lines': room_like_lines(text)[:10],
        'matched_room_blocks': matched_blocks,
        'keyword_snippets': snippets,
        'raw_html_snippets': raw_html_keyword_snippets(text, room_keyword),
        'page_debug': page_debug,
    }


def expand_dynamic_sections(page: Any, source_type: str) -> None:
    selectors: List[str] = []
    if source_type == 'agoda':
        selectors = [
            'text=选择房型', 'text=查看房型', 'text=房型', 'text=查看全部客房', 'text=显示全部客房', 'text=更多房型', 'text=查看更多', 'text=Show all rooms',
            'button:has-text("选择房型")', 'button:has-text("查看房型")', '[href*="room"]', '[data-element-name*="room"]', '[class*=room] button', '[class*=Room] button',
        ]
    try:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 4)
                for index in range(count):
                    item = locator.nth(index)
                    if item.is_visible(timeout=600):
                        item.click(timeout=800)
                        page.wait_for_timeout(700)
            except Exception:
                continue
        wheel_rounds = 10 if source_type == 'agoda' else 6
        for _ in range(wheel_rounds):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(900 if source_type == 'agoda' else 700)
    except Exception:
        pass

def prepare_local_chrome_profile(profile_name: str) -> tuple[Path, str]:
    source_root = DEFAULT_CHROME_USER_DATA_DIR
    source_profile = source_root / profile_name
    if not source_root.exists():
        raise FileNotFoundError(f'未找到本机 Chrome 用户目录：{source_root}')
    if not source_profile.exists():
        raise FileNotFoundError(f'未找到本机 Chrome Profile：{source_profile}')

    temp_root = Path(tempfile.mkdtemp(prefix='hotel-alert-chrome-'))
    local_state = source_root / 'Local State'
    if local_state.exists():
        shutil.copy2(local_state, temp_root / 'Local State')
    shutil.copytree(source_profile, temp_root / profile_name, dirs_exist_ok=True)
    return temp_root, profile_name


def session_login_label(source_type: str) -> str:
    return 'Agoda'


def session_default_target_url(source_type: str) -> str:
    if source_type == 'agoda':
        return 'https://www.agoda.com/'
    return 'https://www.agoda.com/'


def resolve_login_browser_executable() -> str:
    for candidate in SYSTEM_BROWSER_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError('没有找到可用于登录的系统浏览器。请先安装 Google Chrome，再重试登录。')


def ensure_app_session_profile_dir(source_type: str) -> Path:
    profile_dir = APP_SESSION_PROFILE_ROOT / source_type
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _set_app_session_login_state(source_type: str, **kwargs: Any) -> None:
    with APP_SESSION_LOGIN_LOCK:
        state = APP_SESSION_LOGIN_STATES.setdefault(source_type, {
            'running': False,
            'target_url': '',
            'last_error': '',
            'last_started_at': None,
            'last_completed_at': None,
            'window_opened': False,
        })
        state.update(kwargs)


def _app_session_login_worker(source_type: str, target_url: str, stop_event: threading.Event) -> None:
    _set_app_session_login_state(source_type,
        running=True,
        target_url=target_url,
        last_error='',
        last_started_at=utc_now(),
        window_opened=False,
    )
    process = None
    try:
        executable_path = resolve_login_browser_executable()
        profile_dir = ensure_app_session_profile_dir(source_type)
        cleanup_persistent_profile_locks(profile_dir)
        command = [
            executable_path,
            f'--user-data-dir={profile_dir}',
            '--profile-directory=Default',
            '--no-first-run',
            '--no-default-browser-check',
        ]
        debug_port = int(APP_SESSION_DEBUG_PORTS.get(source_type) or 0)
        if debug_port > 0:
            command.append(f'--remote-debugging-port={debug_port}')
        command.append(target_url)
        process = subprocess.Popen(command)
        with APP_SESSION_LOGIN_LOCK:
            APP_SESSION_LOGIN_PROCESSES[source_type] = process
        _set_app_session_login_state(source_type, window_opened=True)
        while not stop_event.is_set():
            if process.poll() is not None:
                break
            time.sleep(0.8)
    except Exception as exc:
        _set_app_session_login_state(source_type, last_error=str(exc))
    finally:
        still_running = bool(process and process.poll() is None)
        with APP_SESSION_LOGIN_LOCK:
            APP_SESSION_LOGIN_PROCESSES.pop(source_type, None)
        _set_app_session_login_state(source_type, running=False, window_opened=still_running, last_completed_at=utc_now())


def launch_login_and_save_session(source_type: str, target_url: str) -> Dict[str, Any]:
    profile_dir = ensure_app_session_profile_dir(source_type)
    label = session_login_label(source_type)
    with APP_SESSION_LOGIN_LOCK:
        thread = APP_SESSION_LOGIN_THREADS.get(source_type)
        if thread and thread.is_alive():
            return {
                'ok': True,
                'already_running': True,
                'message': f'{label} 登录窗口已经打开了。请在浏览器里完成登录，然后点击“我已登录完成”。',
                'profile_dir': str(profile_dir),
            }
        stop_event = threading.Event()
        thread = threading.Thread(target=_app_session_login_worker, args=(source_type, target_url, stop_event), daemon=True)
        APP_SESSION_LOGIN_STOP_EVENTS[source_type] = stop_event
        APP_SESSION_LOGIN_THREADS[source_type] = thread
        thread.start()

    for _ in range(20):
        time.sleep(0.15)
        state = app_session_profile_status(source_type)
        if state.get('last_error'):
            raise RuntimeError(state['last_error'])
        if state.get('login_running') and state.get('window_opened'):
            break

    return {
        'ok': True,
        'message': f'已打开 {label} 登录窗口。请在弹出的浏览器里完成登录，完成后回到这里点击“我已登录完成”。',
        'profile_dir': str(profile_dir),
    }


def finish_login_and_save_session(source_type: str) -> Dict[str, Any]:
    label = session_login_label(source_type)
    with APP_SESSION_LOGIN_LOCK:
        stop_event = APP_SESSION_LOGIN_STOP_EVENTS.get(source_type)
        thread = APP_SESSION_LOGIN_THREADS.get(source_type)
        process = APP_SESSION_LOGIN_PROCESSES.get(source_type)
    if stop_event:
        stop_event.set()
    if thread and thread.is_alive():
        thread.join(timeout=8)
    state = app_session_profile_status(source_type)
    return {
        'ok': True,
        'message': f'{label} 登录态已保存。现在可以重新点“自动识别房型”或“开始监控”。后续抓取会复用当前这个专用浏览器窗口；你可以最小化或切到后台，但不要关闭它。',
        'profile_dir': str(ensure_app_session_profile_dir(source_type)),
        'status': state,
    }


def app_session_profile_status(source_type: str = 'agoda') -> Dict[str, Any]:
    profile_dir = APP_SESSION_PROFILE_ROOT / source_type
    with APP_SESSION_LOGIN_LOCK:
        thread = APP_SESSION_LOGIN_THREADS.get(source_type)
        running = bool(thread and thread.is_alive())
        state = dict(APP_SESSION_LOGIN_STATES.get(source_type) or {})
    return {
        'exists': profile_dir.exists(),
        'profile_dir': str(profile_dir),
        'login_running': running,
        'window_opened': bool(state.get('window_opened')),
        'debug_port': int(APP_SESSION_DEBUG_PORTS.get(source_type) or 0),
        'target_url': state.get('target_url') or '',
        'last_error': state.get('last_error') or '',
        'last_started_at': state.get('last_started_at'),
        'last_completed_at': state.get('last_completed_at'),
        'source_type': source_type,
        'source_label': session_login_label(source_type),
    }


def recent_target_url(source_type: str) -> str:
    for watcher in list_watchers():
        if watcher.source_type == source_type and watcher.target_url.strip():
            return watcher.target_url.strip()
    return session_default_target_url(source_type)


def startup_session_check(source_type: str = 'agoda') -> Dict[str, Any]:
    status = app_session_profile_status(source_type)
    if status.get('login_running'):
        return {
            'ok': False,
            'needs_login': True,
            'message': f'{session_login_label(source_type)} 专用登录窗口还开着，请先完成登录后再继续。',
            'status': status,
        }
    if not status.get('exists'):
        return {
            'ok': False,
            'needs_login': True,
            'message': f'还没有{session_login_label(source_type)}专用登录态，请先在页面里登录并保存会话。',
            'status': status,
            'target_url': recent_target_url(source_type),
        }

    target_url = recent_target_url(source_type)
    headers = source_default_headers(source_type)
    try:
        result = verify_app_session(target_url, headers, source_type)
        return {
            'ok': bool(result.get('ok')),
            'needs_login': not bool(result.get('ok')),
            'message': f'{session_login_label(source_type)}专用登录态验证通过' if result.get('ok') else f'{session_login_label(source_type)}专用登录态已失效，请重新登录。',
            'target_url': target_url,
            'page_debug': result.get('page_debug'),
            'status': status,
        }
    except Exception as exc:
        payload = {
            'ok': False,
            'needs_login': True,
            'message': f'{session_login_label(source_type)}专用登录态验证失败：{exc}',
            'target_url': target_url,
            'status': status,
        }
        debug_payload = getattr(exc, 'debug_payload', None)
        if debug_payload:
            payload['debug'] = debug_payload
        return payload


def startup_all_session_checks() -> Dict[str, Any]:
    items = {}
    all_ok = True
    messages = []
    for source_type in [FIXED_SOURCE_TYPE]:
        result = startup_session_check(source_type)
        items[source_type] = result
        if result.get('ok'):
            messages.append(f"{session_login_label(source_type)}：可用")
        else:
            all_ok = False
            messages.append(f"{session_login_label(source_type)}：需要重新登录或重新验证")
    return {
        'ok': all_ok,
        'items': items,
        'message': '；'.join(messages),
    }


def verify_app_session(target_url: str, headers: Dict[str, str], source_type: str) -> Dict[str, Any]:
    text = browser_fetch_with_app_session(target_url, headers, source_type, '')
    page_debug = parse_page_debug(text)
    signals = page_debug.get('signals') or {}
    final_url = str(page_debug.get('final_url') or '')
    login_hosts = {
        'agoda': ['agoda.com/login', 'www.agoda.com/login', 'auth.agoda.com'],
    }
    host_hit = any(token in final_url for token in login_hosts.get(source_type, []))
    ok = not signals.get('login_like') and not host_hit
    label = session_login_label(source_type)
    return {
        'ok': ok,
        'message': '专用登录态可用。你可以最小化窗口，但不要关闭专用浏览器。' if ok else f'专用登录态仍然落到了{label}登录页，请重新登录后再试',
        'page_debug': page_debug,
    }


def _open_app_session_page(url: str, headers: Dict[str, str], source_type: str, room_keyword: str = '', focus_room: bool = True) -> Dict[str, Any]:
    if source_type == 'agoda':
        raise RuntimeError('Agoda 现在只允许复用当前已打开的专用登录窗口，已禁止后台再新开浏览器实例。请先点击“登录 Agoda”，并保持该专用窗口不要关闭。')
    with APP_SESSION_LOGIN_LOCK:
        thread = APP_SESSION_LOGIN_THREADS.get(source_type)
        if thread and thread.is_alive():
            raise RuntimeError(f'{session_login_label(source_type)}登录窗口还开着。请先点击“我已登录完成”，保存登录态后再识别房型。')
    profile_dir = ensure_app_session_profile_dir(source_type)
    playwright = sync_playwright().start()
    executable_path = resolve_chromium_executable(playwright, prefer_system=True)
    cleanup_persistent_profile_locks(profile_dir)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        executable_path=executable_path,
        headless=False if source_type == 'agoda' else True,
        locale='zh-CN',
        user_agent=headers.get('User-Agent'),
        extra_http_headers={k: v for k, v in headers.items() if k.lower() not in {'user-agent', 'cookie'}},
        args=['--profile-directory=Default', '--no-first-run'],
        viewport={'width': 1440, 'height': 2200},
    )
    page = context.new_page()
    fetch_url = normalize_target_url(url, source_type)
    page.goto(fetch_url, wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_load_state('networkidle', timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    currency_debug = {}
    room_focus_debug = {}
    if source_type == 'agoda':
        # Agoda 拆分版：切币仅属于 Agoda 链路。
        currency_debug = ensure_agoda_currency(page, 'CNY')
        if focus_room and room_keyword.strip():
            room_focus_debug = agoda_focus_room_keyword(page, room_keyword)
    if focus_room:
        expand_dynamic_sections(page, source_type)
    return {
        'playwright': playwright,
        'context': context,
        'page': page,
        'profile_dir': profile_dir,
        'fetch_url': fetch_url,
        'currency_debug': currency_debug,
        'room_focus_debug': room_focus_debug,
    }



def browser_fetch_via_existing_agoda_window(url: str, headers: Dict[str, str], room_keyword: str = '') -> Optional[str]:
    debug_port = int(APP_SESSION_DEBUG_PORTS.get('agoda') or 0)
    if debug_port <= 0:
        return None
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{debug_port}')
        except Exception:
            return None
        page = None
        context = browser.contexts[0] if browser.contexts else None
        if context and context.pages:
            page = context.pages[0]
        if page is None:
            return None
        fetch_url = normalize_target_url(url, 'agoda')
        try:
            page.goto(fetch_url, wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_load_state('networkidle', timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            currency_debug = ensure_agoda_currency(page, 'CNY')
            room_focus_debug = {}
            if room_keyword.strip():
                room_focus_debug = agoda_focus_room_keyword(page, room_keyword)
            expand_dynamic_sections(page, 'agoda')
            room_blocks = extract_room_blocks_for_source(page, 'agoda', room_keyword)
            page_title = ''
            try:
                page_title = page.title()
            except Exception:
                page_title = ''
            visible_text = page.locator('body').inner_text(timeout=5000)
            room_block_text = encode_room_blocks(room_blocks)
            visible_lines = [line.strip() for line in visible_text.splitlines() if line.strip()][:30]
            joined_preview = ' | '.join(visible_lines[:12])
            lower_preview = joined_preview.lower()
            page_debug = {
                'requested_url': fetch_url,
                'final_url': page.url,
                'title': page_title,
                'visible_line_count': len(visible_lines),
                'visible_lines_preview': visible_lines[:20],
                'room_block_count': len(room_blocks),
                'signals': detect_page_signals('agoda', page.url, page_title, lower_preview, len(room_blocks)),
                'app_session_mode': True,
                'profile_dir': str(ensure_app_session_profile_dir('agoda')),
                'currency_debug': currency_debug,
                'current_currency': (currency_debug.get('after_currency') or currency_debug.get('before_currency') or ''),
                'room_focus_debug': room_focus_debug,
                'agoda_live_window_mode': True,
                'debug_port': debug_port,
            }
            page_debug_text = encode_page_debug(page_debug)
            return page_debug_text + ('\n' if page_debug_text else '') + room_block_text + ('\n' if room_block_text else '') + visible_text + '\n\n<!--RAW_HTML-->\n' + page.content()
        finally:
            try:
                browser.close()
            except Exception:
                pass


def browser_capture_via_existing_agoda_window(url: str, headers: Dict[str, str], room_keyword: str = '', focus_room: bool = False) -> Optional[Dict[str, Any]]:
    debug_port = int(APP_SESSION_DEBUG_PORTS.get('agoda') or 0)
    if debug_port <= 0:
        return None
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{debug_port}')
        except Exception:
            return None
        page = None
        context = browser.contexts[0] if browser.contexts else None
        if context and context.pages:
            page = context.pages[0]
        if page is None:
            return None
        fetch_url = normalize_target_url(url, 'agoda')
        try:
            page.goto(fetch_url, wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_load_state('networkidle', timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            currency_debug = ensure_agoda_currency(page, 'CNY')
            room_focus_debug = {}
            if focus_room and room_keyword.strip():
                room_focus_debug = agoda_focus_room_keyword(page, room_keyword)
                expand_dynamic_sections(page, 'agoda')
            safe_source = 'agoda'
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            out_dir = BASE_DIR / 'debug_screens'
            out_dir.mkdir(parents=True, exist_ok=True)
            top_path = out_dir / f'{safe_source}-app-session-top-{ts}.png'
            full_path = out_dir / f'{safe_source}-app-session-full-{ts}.png'
            page.screenshot(path=str(top_path), full_page=False)
            page.screenshot(path=str(full_path), full_page=True)
            page_title = ''
            try:
                page_title = page.title()
            except Exception:
                page_title = ''
            visible_text = page.locator('body').inner_text(timeout=5000)
            visible_lines = [line.strip() for line in visible_text.splitlines() if line.strip()][:30]
            return {
                'top_path': str(top_path),
                'full_path': str(full_path),
                'title': page_title,
                'final_url': page.url,
                'visible_lines_preview': visible_lines[:20],
                'currency_debug': currency_debug,
                'room_focus_debug': room_focus_debug,
                'profile_dir': str(ensure_app_session_profile_dir('agoda')),
                'agoda_live_window_mode': True,
                'debug_port': debug_port,
            }
        finally:
            try:
                browser.close()
            except Exception:
                pass


def browser_capture_with_app_session(url: str, headers: Dict[str, str], source_type: str, room_keyword: str = '', focus_room: bool = False) -> Dict[str, Any]:
    if source_type == 'agoda':
        live_capture = browser_capture_via_existing_agoda_window(url, headers, room_keyword, focus_room=focus_room)
        if live_capture:
            return live_capture
        raise RuntimeError('没有连接到正在打开的 Agoda 专用窗口。请先点击“登录 Agoda”，并保持该专用窗口不要关闭，然后再试。')
    state = _open_app_session_page(url, headers, source_type, room_keyword, focus_room=focus_room)
    context = state['context']
    playwright = state['playwright']
    page = state['page']
    try:
        safe_source = re.sub(r'[^a-z0-9_-]+', '-', (source_type or 'generic').lower()).strip('-') or 'generic'
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        out_dir = BASE_DIR / 'debug_screens'
        out_dir.mkdir(parents=True, exist_ok=True)
        top_path = out_dir / f'{safe_source}-app-session-top-{ts}.png'
        full_path = out_dir / f'{safe_source}-app-session-full-{ts}.png'
        page.screenshot(path=str(top_path), full_page=False)
        page.screenshot(path=str(full_path), full_page=True)
        page_title = ''
        try:
            page_title = page.title()
        except Exception:
            page_title = ''
        visible_text = page.locator('body').inner_text(timeout=5000)
        visible_lines = [line.strip() for line in visible_text.splitlines() if line.strip()][:30]
        return {
            'top_path': str(top_path),
            'full_path': str(full_path),
            'title': page_title,
            'final_url': page.url,
            'visible_lines_preview': visible_lines[:20],
            'currency_debug': state.get('currency_debug') or {},
            'room_focus_debug': state.get('room_focus_debug') or {},
            'profile_dir': str(state.get('profile_dir')),
        }
    finally:
        context.close()
        playwright.stop()


def browser_fetch_with_app_session(url: str, headers: Dict[str, str], source_type: str, room_keyword: str = '') -> str:
    if source_type == 'agoda':
        live_text = browser_fetch_via_existing_agoda_window(url, headers, room_keyword)
        if live_text:
            return live_text
        raise RuntimeError('没有连接到正在打开的 Agoda 专用窗口。请先点击“登录 Agoda”，在该窗口里完成登录并保持窗口不要关闭，然后再试。')
    state = _open_app_session_page(url, headers, source_type, room_keyword, focus_room=True)
    context = state['context']
    playwright = state['playwright']
    page = state['page']
    profile_dir = state['profile_dir']
    fetch_url = state['fetch_url']
    currency_debug = state['currency_debug']
    room_focus_debug = state['room_focus_debug']
    try:
        room_blocks = extract_room_blocks_for_source(page, source_type, room_keyword)
        page_title = ''
        try:
            page_title = page.title()
        except Exception:
            page_title = ''
        visible_text = page.locator('body').inner_text(timeout=5000)
        room_block_text = encode_room_blocks(room_blocks)
        visible_lines = [line.strip() for line in visible_text.splitlines() if line.strip()][:30]
        joined_preview = ' | '.join(visible_lines[:12])
        lower_preview = joined_preview.lower()
        page_debug = {
            'requested_url': fetch_url,
            'final_url': page.url,
            'title': page_title,
            'visible_line_count': len(visible_lines),
            'visible_lines_preview': visible_lines[:20],
            'room_block_count': len(room_blocks),
            'signals': detect_page_signals(source_type, page.url, page_title, lower_preview, len(room_blocks)),
            'app_session_mode': True,
            'profile_dir': str(profile_dir),
            'currency_debug': currency_debug,
            'current_currency': (currency_debug.get('after_currency') or currency_debug.get('before_currency') or ''),
            'room_focus_debug': room_focus_debug,
        }
        page_debug_text = encode_page_debug(page_debug)
        content = page_debug_text + ('\n' if page_debug_text else '') + room_block_text + ('\n' if room_block_text else '') + visible_text + '\n\n<!--RAW_HTML-->\n' + page.content()
        return content
    finally:
        context.close()
        playwright.stop()


def discover_chrome_profiles() -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    root = DEFAULT_CHROME_USER_DATA_DIR
    if not root.exists():
        return items

    local_state_path = root / 'Local State'
    profile_names: Dict[str, str] = {}
    if local_state_path.exists():
        try:
            local_state = json.loads(local_state_path.read_text(encoding='utf-8'))
            info_cache = local_state.get('profile', {}).get('info_cache', {})
            if isinstance(info_cache, dict):
                for key, value in info_cache.items():
                    if isinstance(value, dict):
                        profile_names[key] = str(value.get('name') or key)
        except Exception:
            pass

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == 'System Profile' or child.name.startswith('Guest'):
            continue
        if child.name == 'Default' or child.name.startswith('Profile '):
            items.append({'dir_name': child.name, 'display_name': profile_names.get(child.name, child.name)})

    if not items and (root / 'Default').exists():
        items.append({'dir_name': 'Default', 'display_name': 'Default'})
    return items


def browser_fetch_with_local_profile(url: str, headers: Dict[str, str], source_type: str, profile_name: str, room_keyword: str = '') -> str:
    temp_root, copied_profile_name = prepare_local_chrome_profile(profile_name)
    try:
        with sync_playwright() as playwright:
            executable_path = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(temp_root),
                executable_path=executable_path,
                headless=True,
                locale='zh-CN',
                user_agent=headers.get('User-Agent'),
                extra_http_headers={k: v for k, v in headers.items() if k.lower() not in {'user-agent', 'cookie'}},
                args=[f'--profile-directory={copied_profile_name}', '--no-first-run'],
            )
            page = context.new_page()
            fetch_url = normalize_target_url(url, source_type)
            page.goto(fetch_url, wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_load_state('networkidle', timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            currency_debug = {}
            if source_type == 'agoda':
                # Agoda 拆分版：切币仅属于 Agoda 链路。
                currency_debug = ensure_agoda_currency(page, 'CNY')
            expand_dynamic_sections(page, source_type)
            room_blocks = extract_room_blocks_for_source(page, source_type, room_keyword)
            page_title = ''
            try:
                page_title = page.title()
            except Exception:
                page_title = ''
            visible_text = page.locator('body').inner_text(timeout=5000)
            room_block_text = encode_room_blocks(room_blocks)
            visible_lines = [line.strip() for line in visible_text.splitlines() if line.strip()][:30]
            joined_preview = ' | '.join(visible_lines[:12])
            lower_preview = joined_preview.lower()
            page_debug = {
                'requested_url': fetch_url,
                'final_url': page.url,
                'title': page_title,
                'visible_line_count': len(visible_lines),
                'visible_lines_preview': visible_lines[:20],
                'room_block_count': len(room_blocks),
                'signals': detect_page_signals(source_type, page.url, page_title, lower_preview, len(room_blocks)),
                'local_profile_mode': True,
                'chrome_profile_name': profile_name,
                'currency_debug': currency_debug,
                'current_currency': (currency_debug.get('after_currency') or currency_debug.get('before_currency') or ''),
            }
            page_debug_text = encode_page_debug(page_debug)
            content = page_debug_text + ('\n' if page_debug_text else '') + room_block_text + ('\n' if room_block_text else '') + visible_text + '\n\n<!--RAW_HTML-->\n' + page.content()
            context.close()
            return content
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return

    selectors = [
        'text=全部房型',
        'text=查看全部房型',
        'text=展开全部房型',
        'text=更多房型',
        'text=全部展开',
        '[class*=room] button',
        '[class*=Room] button',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 5)
            for index in range(count):
                item = locator.nth(index)
                if item.is_visible(timeout=800):
                    item.click(timeout=800)
                    page.wait_for_timeout(500)
        except Exception:
            continue

    try:
        for _ in range(4):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(700)
    except Exception:
        pass


def browser_fetch(url: str, headers: Dict[str, str], source_type: str = 'generic', room_keyword: str = '') -> str:
    with BROWSER_LOCK:
        with sync_playwright() as playwright:
            executable_path = resolve_chromium_executable(playwright)
            browser = playwright.chromium.launch(executable_path=executable_path, headless=True)
            context = browser.new_context(
                user_agent=headers.get('User-Agent'),
                locale='zh-CN',
                extra_http_headers={k: v for k, v in headers.items() if k.lower() not in {'user-agent'}},
            )
            page = context.new_page()
            fetch_url = normalize_target_url(url, source_type)
            page.goto(fetch_url, wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_load_state('networkidle', timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            currency_debug = {}
            if source_type == 'agoda':
                # Agoda 拆分版：切币仅属于 Agoda 链路。
                currency_debug = ensure_agoda_currency(page, 'CNY')
            expand_dynamic_sections(page, source_type)
            room_blocks = extract_room_blocks_for_source(page, source_type, room_keyword)
            page_title = ''
            try:
                page_title = page.title()
            except Exception:
                page_title = ''
            visible_text = page.locator('body').inner_text(timeout=5000)
            room_block_text = encode_room_blocks(room_blocks)
            visible_lines = [line.strip() for line in visible_text.splitlines() if line.strip()][:30]
            joined_preview = ' | '.join(visible_lines[:12])
            lower_preview = joined_preview.lower()
            page_debug = {
                'requested_url': fetch_url,
                'final_url': page.url,
                'title': page_title,
                'visible_line_count': len(visible_lines),
                'visible_lines_preview': visible_lines[:20],
                'room_block_count': len(room_blocks),
                'signals': detect_page_signals(source_type, page.url, page_title, lower_preview, len(room_blocks)),
                'currency_debug': currency_debug,
                'current_currency': (currency_debug.get('after_currency') or currency_debug.get('before_currency') or ''),
            }
            page_debug_text = encode_page_debug(page_debug)
            content = page_debug_text + ('\n' if page_debug_text else '') + room_block_text + ('\n' if room_block_text else '') + visible_text + '\n\n<!--RAW_HTML-->\n' + page.content()
            context.close()
            browser.close()
            return content


def candidate_patterns(watcher: Watcher) -> List[str]:
    patterns: List[str] = []
    if watcher.price_pattern.strip():
        patterns.append(watcher.price_pattern.strip())
    patterns.extend(DEFAULT_PATTERNS.get(watcher.source_type, DEFAULT_PATTERNS['generic']))
    return patterns


def plausible_price_from_text(text: str) -> Optional[float]:
    candidates = re.findall(r'[¥￥]\s*([0-9][0-9,]*(?:\.\d+)?)', text)
    numbers = [float(value.replace(',', '')) for value in candidates if 50 <= float(value.replace(',', '')) <= 50000]
    if not numbers:
        return None
    return min(numbers)


CURRENCY_ALIASES = {
    '¥': 'CNY', '￥': 'CNY', 'CNY': 'CNY', 'RMB': 'CNY',
    'S$': 'SGD', 'SGD': 'SGD',
    '$': 'USD', 'US$': 'USD', 'USD': 'USD',
}

CURRENCY_FALLBACK_RATES = {
    ('SGD', 'CNY'): 5.129,
    ('USD', 'CNY'): 7.20,
}


def extract_currency_price_candidates(text: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    pattern = re.compile(r'(S\$|US\$|SGD|USD|CNY|RMB|[¥￥$])\s*([0-9][0-9,]{2,}(?:\.\d{1,2})?)', re.I)
    for match in pattern.finditer(text):
        raw_currency = match.group(1).upper()
        currency = CURRENCY_ALIASES.get(match.group(1), CURRENCY_ALIASES.get(raw_currency, raw_currency))
        amount = float(match.group(2).replace(',', ''))
        if not (50 <= amount <= 100000):
            continue
        before = text[max(0, match.start() - 12): match.start()]
        after = text[match.end(): min(len(text), match.end() + 8)]
        near = text[max(0, match.start() - 20): min(len(text), match.end() + 20)]
        if '/月' in after or '每月' in after or '月付' in after:
            continue
        if '返现奖励' in before or 'cashback' in before.lower():
            continue
        results.append({'currency': currency, 'display_currency': match.group(1).upper(), 'amount': amount, 'raw': match.group(0), 'context': near, 'pos': match.start()})
    return results


def maybe_convert_price(amount: float, from_currency: str, to_currency: str) -> Optional[float]:
    if from_currency == to_currency:
        return amount
    rate = CURRENCY_FALLBACK_RATES.get((from_currency, to_currency))
    if rate is None:
        return None
    return round(amount * rate, 2)


def extract_price(text: str, patterns: List[str]) -> float:
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if not match:
            continue
        raw = match.group(1) if match.groups() else match.group(0)
        normalized = re.sub(r'[^\d.]', '', raw)
        if normalized:
            value = float(normalized)
            if 20 <= value <= 100000:
                return value
    fallback = plausible_price_from_text(text)
    if fallback is not None:
        return fallback
    raise ValueError('没抓到价格。通常是页面需要登录/Cookie，或平台页面结构变了。')


def matched_room_blocks(text: str, watcher: Watcher) -> List[Dict[str, Any]]:
    keyword = watcher.room_type_keyword.strip()
    if not keyword:
        return []
    block_items = parse_room_blocks(text)
    if not block_items:
        return []

    variants = [item.lower() for item in keyword_variants(keyword)]
    preferred_tags = [item.strip().lower() for item in watcher.meta_tags() if item.strip()]
    matched: List[Dict[str, Any]] = []
    for item in block_items:
        haystack = ' '.join([
            str(item.get('room_name', '')),
            str(item.get('raw_text', '')),
            ' '.join(item.get('tags', [])),
        ]).lower()
        if not any(variant in haystack for variant in variants):
            continue
        score = 0
        room_name = str(item.get('room_name', '')).lower()
        raw_text = str(item.get('raw_text', '')).lower()
        if keyword.lower() in room_name:
            score += 8
        if keyword.lower() in raw_text:
            score += 5
        for variant in variants[:8]:
            if variant in room_name:
                score += min(len(variant), 6)
        tags = [str(tag).strip().lower() for tag in item.get('tags', []) if str(tag).strip()]
        if preferred_tags:
            for preferred in preferred_tags:
                if preferred in tags or preferred in raw_text:
                    score += 6
        price = float(item.get('price') or 0)
        if bool(item.get('target_match')):
            score += 100
        min_expected = float(watcher.min_expected_price or 0)
        if min_expected and price < min_expected:
            score -= 100
        if price >= 500:
            score += 2
        elif price < 100:
            score -= 8
        enriched = dict(item)
        enriched['match_score'] = score
        matched.append(enriched)
    matched.sort(key=lambda item: (-int(item.get('match_score', 0)), float(item.get('price') or 0)))
    return matched


def keyword_variants(keyword: str) -> List[str]:
    variants: List[str] = []
    if keyword:
        variants.append(keyword)

    parts = [part for part in re.split(r'[\s/|（）()\-]+', keyword) if len(part) >= 2]
    variants.extend(parts)

    chinese = re.sub(r'[^一-鿿A-Za-z0-9]', '', keyword)
    if len(chinese) >= 4:
        for size in range(min(4, len(chinese)), 1, -1):
            for index in range(0, len(chinese) - size + 1):
                piece = chinese[index:index + size]
                if len(piece) >= 2:
                    variants.append(piece)

    seen = set()
    ordered = []
    for item in variants:
        item = item.strip()
        if len(item) >= 2 and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def room_scoped_texts(text: str, room_keyword: str, watcher: Optional[Watcher] = None) -> List[str]:
    keyword = room_keyword.strip()
    if not keyword:
        return [text]

    block_items = parse_room_blocks(text)
    if block_items:
        matched_blocks = []
        variants = [item.lower() for item in keyword_variants(keyword)]
        for item in block_items:
            haystack = ' '.join([item.get('room_name', ''), item.get('raw_text', ''), ' '.join(item.get('tags', []))]).lower()
            if any(variant.lower() in haystack for variant in variants):
                matched_blocks.append(item.get('raw_text', ''))
        if matched_blocks:
            return matched_blocks

    snippets: List[str] = []
    patterns = [re.escape(item) for item in keyword_variants(keyword)]
    search_text = search_text_only(text)

    seen_ranges = set()
    for pattern in patterns:
        for match in re.finditer(pattern, search_text, re.I):
            start = max(0, match.start() - ROOM_SNIPPET_RADIUS)
            end = min(len(search_text), match.end() + ROOM_SNIPPET_RADIUS)
            marker = (start, end)
            if marker in seen_ranges:
                continue
            snippet = search_text[start:end]
            if agoda_snippet_is_review_like(snippet) and not agoda_snippet_has_room_price_context(snippet):
                continue
            seen_ranges.add(marker)
            snippets.append(snippet)
            if len(snippets) >= 12:
                return snippets
    if snippets:
        return snippets

    suggestions = room_candidate_summaries(text, 6)
    variants = keyword_variants(keyword)
    variant_text = ' / '.join(variants[:8])
    page_debug = parse_page_debug(text)
    final_url = str(page_debug.get('final_url') or '')
    signals = page_debug.get('signals') or {}
    if signals.get('login_like'):
        error = ValueError(
            f'当前抓到的并不是 Agoda 酒店房型页，而是 Agoda 登录页：{final_url or "(未知链接)"}。'
            '请先点“登录 Agoda 并保存会话”，在弹出的浏览器里完成登录，再回来点“我已登录完成”，然后重新识别房型。'
        )
        setattr(error, 'debug_payload', build_room_debug_payload(text, keyword, watcher))
        raise error
    if suggestions:
        error = ValueError(
            f"没有找到房型关键词：{keyword}。已尝试匹配这些关键词片段：{variant_text}。"
            f"当前页面抓到的房型候选示例：{'；'.join(suggestions)}。"
            "建议：1）先点‘自动识别房型’；2）复制候选里的完整房型名再试；3）如果页面明明有但候选里没有，说明该房型可能还未加载出来。"
        )
        setattr(error, 'debug_payload', build_room_debug_payload(text, keyword, watcher))
        raise error
    error = ValueError(
        f"没有找到房型关键词：{keyword}。已尝试匹配这些关键词片段：{variant_text}。"
        "当前页面没有抓到任何明确的房型候选，建议先滚动页面、补 Cookie，或换成更具体的房型完整名称。"
    )
    setattr(error, 'debug_payload', build_room_debug_payload(text, keyword, watcher))
    raise error


def normalize_room_name(name: str) -> str:
    cleaned = re.sub(r'\s+', ' ', name).strip(' -:\n\t')
    cleaned = re.sub(r'[{}<>\[\]#@|]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def visible_text_only(text: str) -> str:
    marker = '\n\n<!--RAW_HTML-->\n'
    return text.split(marker, 1)[0] if marker in text else text


def search_text_only(text: str) -> str:
    visible = visible_text_only(text)
    lines = []
    for line in visible.splitlines():
        if line.startswith('PAGE_DEBUG||') or line.startswith('ROOM_BLOCK||'):
            continue
        lines.append(line)
    return '\n'.join(lines)

def looks_noisy_room_name(name: str) -> bool:
    cleaned = normalize_room_name(name)
    if not cleaned:
        return True
    if len(cleaned) > 80:
        return True
    bad_patterns = [
        r'^ROOM_BLOCK\\b',
        r'^选择房间\\b',
        r'^房间\\b$',
        r'^概览\\b',
        r'^展示额外\\d+个房型价格',
        r'photo gallery',
        r'Go to main content',
        r'房间详情',
        r'房型摘要',
        r'可住人数',
        r'^\\d+张',
        r'^\\d+\\s+\\d+张',
        r'热卖',
        r'仅剩\d+间',
        r'酒店',
        r'度假村',
        r'度假酒店',
        r'\(.*\)',
    ]
    return any(re.search(pattern, cleaned, re.I) for pattern in bad_patterns)


def looks_garbled(line: str) -> bool:
    if not line:
        return True
    if len(line) > 80:
        return True
    weird = sum(1 for ch in line if ord(ch) < 32 and ch not in '\t\n\r')
    if weird:
        return True
    symbol_count = sum(1 for ch in line if ch in '{}<>[]|=_#@/\\')
    if symbol_count >= max(4, len(line) // 3):
        return True
    if re.search(r'\b(function|return|const|var|undefined|null|true|false)\b', line, re.I):
        return True
    return False


def extract_room_candidates(text: str) -> List[Dict[str, Any]]:
    block_items = parse_room_blocks(text)
    candidates: List[Dict[str, Any]] = []
    seen = set()
    if block_items:
        room_hint = re.compile(r'(房|床|套房|别墅|Villa|Suite|Twin|King|Deluxe|泳池|水疗|Spa|Pool)', re.I)
        for item in block_items:
            room_name = normalize_room_name(str(item.get('room_name', '')))
            if looks_noisy_room_name(room_name):
                continue
            if not room_hint.search(room_name):
                continue
            price = float(item.get('price') or 0)
            if not (500 <= price <= 100000):
                continue
            tags = [normalize_room_name(str(tag)) for tag in (item.get('tags', []) or []) if normalize_room_name(str(tag))]
            key = (room_name.lower(), round(price, 2), tuple(tags))
            if key in seen:
                continue
            seen.add(key)
            candidates.append({'room_name': room_name, 'price': price, 'tags': tags})
        if candidates:
            candidates.sort(key=lambda item: item['price'])
            return candidates[:ROOM_PREVIEW_LIMIT]

    text = visible_text_only(text)
    text = re.sub(r'(?i)(房型总览|酒店介绍|住客点评|周边信息)', '\n', text)
    lines = [normalize_room_name(line) for line in text.splitlines()]
    lines = [line for line in lines if line and not looks_garbled(line) and not looks_noisy_room_name(line)]
    candidates = []
    room_hint = re.compile(r'(房|床|双早|大床|双床|套房|景|Deluxe|King|Twin|Suite|Room|Breakfast|Villa)', re.I)
    price_hint = re.compile(r'([¥￥]\s*[0-9][0-9,]*(?:\.\d+)?)|(\d{3,6}(?:\.\d{1,2})?)')
    breakfast_hint = re.compile(r'(含早|双早|单早|早餐|Breakfast|breakfast)', re.I)
    cancel_hint = re.compile(r'(免费取消|不可取消|不可退款|免费退|No refund|Free cancellation)', re.I)
    pay_hint = re.compile(r'(到店付|在线付|预付|Pay at property|Prepay)', re.I)
    for index, line in enumerate(lines):
        if not room_hint.search(line):
            continue
        window = lines[max(0, index - 1):index + 6]
        block = ' '.join(window)
        match = price_hint.search(block)
        if not match:
            continue
        value_text = match.group(1) or match.group(2)
        value = float(re.sub(r'[^\d.,]', '', value_text).replace(',', ''))
        if not (500 <= value <= 100000):
            continue
        room_name = line[:80]
        tags = []
        breakfast_match = breakfast_hint.search(block)
        cancel_match = cancel_hint.search(block)
        pay_match = pay_hint.search(block)
        if breakfast_match:
            tags.append(breakfast_match.group(1))
        if cancel_match:
            tags.append(cancel_match.group(1))
        if pay_match:
            tags.append(pay_match.group(1))
        key = (room_name.lower(), round(value, 2), tuple(tags))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({'room_name': room_name, 'price': value, 'tags': tags})
        if len(candidates) >= ROOM_PREVIEW_LIMIT:
            break
    candidates.sort(key=lambda item: item['price'])
    return candidates


def extract_price_for_watcher(text: str, watcher: Watcher) -> float:
    min_expected = float(watcher.min_expected_price or 0)
    page_debug = parse_page_debug(text)
    current_currency = str(page_debug.get('current_currency') or watcher.currency or '').upper()
    matched_blocks = matched_room_blocks(text, watcher)
    filtered_out_prices: List[float] = []
    if matched_blocks:
        for block in matched_blocks:
            price = float(block.get('price') or 0)
            if min_expected and price < min_expected:
                filtered_out_prices.append(price)
                continue
            if 20 <= price <= 100000:
                return price

    snippets = room_scoped_texts(text, watcher.room_type_keyword, watcher)
    patterns = candidate_patterns(watcher)
    last_error: Optional[Exception] = None
    for snippet in snippets:
        try:
            if watcher.source_type == 'agoda':
                keyword_zone = snippet
                variant_positions = []
                for variant in keyword_variants(watcher.room_type_keyword)[:6]:
                    match = re.search(re.escape(variant), snippet, re.I)
                    if match:
                        variant_positions.append(match.start())
                if variant_positions:
                    pivot = min(variant_positions)
                    start = max(0, pivot - 12)
                    end = min(len(snippet), pivot + 1200)
                    keyword_zone = snippet[start:end]
                currency_candidates = extract_currency_price_candidates(keyword_zone)
                if currency_candidates:
                    converted_candidates: List[Dict[str, Any]] = []
                    pivot_pos = pivot if variant_positions else 0
                    tax_line_pos = -1
                    for marker in ['含税单价', '含税', '含税/费', '含税费']:
                        found = keyword_zone.find(marker)
                        if found >= 0:
                            tax_line_pos = found
                            break

                    def display_currency_for(candidate: Dict[str, Any]) -> str:
                        display = str(candidate.get('display_currency') or candidate.get('currency') or '').upper()
                        if display in {'¥', '￥', '$'} and current_currency:
                            return current_currency
                        if display == 'CNY' and current_currency in {'RMB', 'CNY'}:
                            return current_currency
                        return display or str(candidate.get('currency') or watcher.currency)

                    chosen_raw_candidate: Optional[Dict[str, Any]] = None
                    if tax_line_pos >= 0:
                        raw_tax_candidates = [item for item in currency_candidates if 0 <= tax_line_pos - int(item.get('pos', 0)) <= 80]
                        if raw_tax_candidates:
                            raw_tax_candidates.sort(key=lambda item: (tax_line_pos - int(item.get('pos', 0)), float(item.get('amount', 0))))
                            chosen_raw_candidate = raw_tax_candidates[0]
                    if chosen_raw_candidate is None:
                        for candidate in currency_candidates:
                            converted = maybe_convert_price(float(candidate['amount']), str(candidate['currency']), watcher.currency)
                            if converted is not None:
                                pos = int(candidate.get('pos', 0))
                                converted_candidates.append({
                                    'value': converted,
                                    'distance': abs(pos - pivot_pos),
                                    'offset': pos - pivot_pos,
                                    'pos': pos,
                                    'raw_candidate': candidate,
                                })
                        if converted_candidates:
                            taxline_candidates = []
                            if tax_line_pos >= 0:
                                taxline_candidates = [item for item in converted_candidates if 0 <= tax_line_pos - item['pos'] <= 80]
                            chosen_converted = None
                            if taxline_candidates:
                                taxline_candidates.sort(key=lambda item: (tax_line_pos - item['pos'], item['value']))
                                chosen_converted = taxline_candidates[0]
                            else:
                                after_candidates = [item for item in converted_candidates if 0 <= item['offset'] <= 260]
                                before_candidates = [item for item in converted_candidates if -180 <= item['offset'] < 0]
                                if after_candidates:
                                    after_candidates.sort(key=lambda item: (abs(item['offset']), item['value']))
                                    chosen_converted = after_candidates[0]
                                elif before_candidates:
                                    before_candidates.sort(key=lambda item: (abs(item['offset']), item['value']))
                                    chosen_converted = before_candidates[0]
                                else:
                                    converted_candidates.sort(key=lambda item: (item['distance'], item['value']))
                                    chosen_converted = converted_candidates[0]
                            if chosen_converted is not None:
                                value = float(chosen_converted['value'])
                                raw_candidate = chosen_converted.get('raw_candidate') or {}
                                raw_amount = float(raw_candidate.get('amount') or value)
                                raw_currency = display_currency_for(raw_candidate) if raw_candidate else (current_currency or watcher.currency)
                                watcher._runtime_price_note = f'原始抓取价：{raw_currency} {raw_amount:.2f}'
                                if raw_currency != watcher.currency or abs(raw_amount - value) > 0.009:
                                    watcher._runtime_price_note += f'；当前展示价：{watcher.currency} {value:.2f}'
                                if min_expected and value < min_expected:
                                    filtered_out_prices.append(value)
                                    raise ValueError(f'抓到的价格 {value:.2f} 低于你设置的最低合理价格 {min_expected:.2f}')
                                return value
                    if chosen_raw_candidate is not None:
                        raw_amount = float(chosen_raw_candidate['amount'])
                        raw_currency = display_currency_for(chosen_raw_candidate)
                        value = maybe_convert_price(raw_amount, str(chosen_raw_candidate['currency']), watcher.currency)
                        if value is None:
                            value = raw_amount
                        watcher._runtime_price_note = f'原始抓取价：{raw_currency} {raw_amount:.2f}'
                        if raw_currency != watcher.currency or abs(raw_amount - value) > 0.009:
                            watcher._runtime_price_note += f'；当前展示价：{watcher.currency} {value:.2f}'
                        if min_expected and value < min_expected:
                            filtered_out_prices.append(value)
                            raise ValueError(f'抓到的价格 {value:.2f} 低于你设置的最低合理价格 {min_expected:.2f}')
                        return value
            value = extract_price(snippet, patterns)
            if min_expected and value < min_expected:
                filtered_out_prices.append(value)
                raise ValueError(f'抓到的价格 {value:.2f} 低于你设置的最低合理价格 {min_expected:.2f}')
            return value
        except Exception as exc:
            last_error = exc
    if watcher.room_type_keyword.strip():
        if filtered_out_prices:
            samples = ' / '.join(f'{value:.2f}' for value in filtered_out_prices[:6])
            error = ValueError(
                f'找到了房型关键词，但当前抓到的价格都低于你设置的最低合理价格：{watcher.room_type_keyword}。'
                f'最低合理价格为 {min_expected:.2f}，本次抓到的候选价格有：{samples}。'
                '你可以把“最低合理价格”调低一点，或者继续优化房型匹配。'
            )
            setattr(error, 'debug_payload', build_room_debug_payload(text, watcher.room_type_keyword, watcher))
            raise error from last_error
        error = ValueError(f'找到了房型关键词，但没抓到对应价格：{watcher.room_type_keyword}')
        setattr(error, 'debug_payload', build_room_debug_payload(text, watcher.room_type_keyword, watcher))
        raise error from last_error
    if last_error:
        raise last_error
    raise ValueError('没抓到价格')


def should_notify(watcher: Watcher, current_price: float) -> bool:
    threshold_hit = watcher.threshold_price is not None and current_price <= watcher.threshold_price
    drop_hit = watcher.last_price is not None and current_price < watcher.last_price
    first_hit = watcher.last_price is None and threshold_hit
    already_notified_same_price = watcher.last_notified_price is not None and current_price >= watcher.last_notified_price
    return (threshold_hit or drop_hit or first_hit) and not already_notified_same_price


def send_feishu_webhook(webhook_url: str, content: str) -> None:
    payload = json.dumps({'msg_type': 'text', 'content': {'text': content}}).encode('utf-8')
    request = urllib.request.Request(webhook_url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    with http_open(request, timeout=15) as response:
        response.read()


def send_wechat_webhook(webhook_url: str, content: str) -> None:
    payload = json.dumps({'msgtype': 'text', 'text': {'content': content}}).encode('utf-8')
    request = urllib.request.Request(webhook_url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    with http_open(request, timeout=15) as response:
        response.read()


def send_notification(watcher: Watcher, current_price: float, is_test: bool = False) -> None:
    lines = [
        '酒店价格提醒测试' if is_test else '酒店价格提醒',
        f'平台: {SOURCE_LABELS.get(watcher.source_type, watcher.source_type)}',
        f'监控任务: {watcher.name}',
        f'酒店: {watcher.hotel_name}',
    ]
    if watcher.room_type_keyword.strip():
        lines.append(f'房型: {watcher.room_type_keyword}')
    if watcher.room_type_meta.strip():
        lines.append(f'房型标签: {watcher.room_type_meta}')
    lines.append(f'当前价格: {watcher.currency} {current_price:.2f}')
    if watcher.last_price is not None and not is_test:
        lines.append(f'上次价格: {watcher.currency} {watcher.last_price:.2f}')
    if watcher.threshold_price is not None:
        lines.append(f'目标价格: {watcher.currency} {watcher.threshold_price:.2f}')
    if watcher.min_expected_price is not None:
        lines.append(f'最低合理价格: {watcher.currency} {watcher.min_expected_price:.2f}')
    lines.append(f'链接: {watcher.target_url}')
    content = '\n'.join(lines)
    if watcher.notify_type == 'wechat':
        send_wechat_webhook(watcher.notify_target, content)
    else:
        send_feishu_webhook(watcher.notify_target, content)


def check_watcher(watcher: Watcher) -> Dict[str, Any]:
    try:
        headers = watcher.parsed_headers() or source_default_headers(watcher.source_type)
        if watcher.use_browser and watcher.use_app_session_profile:
            text = browser_fetch_with_app_session(watcher.target_url, headers, watcher.source_type, watcher.room_type_keyword)
        elif watcher.use_browser and watcher.use_local_chrome_profile:
            text = browser_fetch_with_local_profile(watcher.target_url, headers, watcher.source_type, watcher.chrome_profile_name, watcher.room_type_keyword)
        else:
            text = browser_fetch(watcher.target_url, headers, watcher.source_type, watcher.room_type_keyword) if watcher.use_browser else fetch_text(watcher.target_url, headers)
        setattr(watcher, '_runtime_price_note', None)
        current_price = extract_price_for_watcher(text, watcher)
        notify = should_notify(watcher, current_price)
        if notify:
            send_notification(watcher, current_price)
        update_check_result(watcher.id, current_price, notify, None, getattr(watcher, '_runtime_price_note', None))
        return {'ok': True, 'price': current_price, 'notified': notify, 'price_note': getattr(watcher, '_runtime_price_note', None)}
    except Exception as exc:
        update_check_result(watcher.id, watcher.last_price, False, str(exc), getattr(watcher, '_runtime_price_note', None))
        payload = {'ok': False, 'error': str(exc)}
        if 'Invalid header value' in str(exc):
            payload['error'] = '请求头里有非法内容，通常是 Cookie 里带了换行或特殊字符。请重新粘贴一整串 Cookie。'
        debug_payload = getattr(exc, 'debug_payload', None)
        if debug_payload:
            payload['debug'] = debug_payload
        return payload


class Poller(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = threading.Event()
        self._next_run_at: Dict[int, float] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def _schedule_next(self, watcher: Watcher, base_time: Optional[float] = None) -> None:
        base = time.time() if base_time is None else base_time
        interval_seconds = max(60, int(watcher.poll_interval_minutes or DEFAULT_POLL_INTERVAL_MINUTES) * 60)
        jitter = random.randint(-POLL_JITTER_SECONDS, POLL_JITTER_SECONDS)
        self._next_run_at[watcher.id] = base + interval_seconds + jitter

    def _initial_due_at(self, watcher: Watcher) -> float:
        interval_seconds = max(60, int(watcher.poll_interval_minutes or DEFAULT_POLL_INTERVAL_MINUTES) * 60)
        last_checked = parse_utc_timestamp(watcher.last_checked_at)
        now_ts = time.time()
        if not last_checked:
            return now_ts + random.randint(0, POLL_JITTER_SECONDS)
        last_ts = last_checked.timestamp()
        due = last_ts + interval_seconds + random.randint(-POLL_JITTER_SECONDS, POLL_JITTER_SECONDS)
        return due if due > now_ts else now_ts + random.randint(0, POLL_JITTER_SECONDS)

    def run(self) -> None:
        while not self._stop_event.is_set():
            watchers = list_watchers()
            active_ids = {watcher.id for watcher in watchers if watcher.is_active}
            for watcher_id in list(self._next_run_at):
                if watcher_id not in active_ids:
                    self._next_run_at.pop(watcher_id, None)
            now_ts = time.time()
            for watcher in watchers:
                if not watcher.is_active:
                    continue
                due_at = self._next_run_at.get(watcher.id)
                if due_at is None:
                    due_at = self._initial_due_at(watcher)
                    self._next_run_at[watcher.id] = due_at
                if now_ts >= due_at:
                    check_watcher(watcher)
                    self._schedule_next(watcher, time.time())
            self._stop_event.wait(1)

class AppHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode('utf-8')
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = file_path.read_bytes()
        content_type = 'text/css; charset=utf-8' if file_path.suffix == '.css' else 'text/plain; charset=utf-8'
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/':
            self._send_html(INDEX_HTML)
            return
        if parsed.path == '/api/watchers':
            items = []
            for watcher in list_watchers():
                item = watcher.__dict__.copy()
                item['source_label'] = SOURCE_LABELS.get(watcher.source_type, watcher.source_type)
                item['tip'] = SOURCE_TIPS.get(watcher.source_type, '')
                item['room_type_tags'] = watcher.meta_tags()
                item['history'] = list_history(watcher.id, PRICE_HISTORY_LIMIT)
                item['next_check_at'] = watcher_next_run_display(watcher)
                items.append(item)
            self._send_json({'items': items, 'poll_interval_seconds': POLL_INTERVAL_SECONDS, 'build_version': APP_BUILD_VERSION})
            return
        if parsed.path == '/api/version':
            self._send_json({'build_version': APP_BUILD_VERSION})
            return
        if parsed.path == '/api/source-presets':
            self._send_json({
                'items': [
                    {
                        'key': key,
                        'label': SOURCE_LABELS[key],
                        'tip': SOURCE_TIPS[key],
                        'patterns': DEFAULT_PATTERNS[key],
                    }
                    for key in ['agoda']
                ]
            })
            return
        if parsed.path == '/api/chrome-profiles':
            self._send_json({'items': discover_chrome_profiles()})
            return
        if parsed.path == '/api/app-session-status':
            source_type = urllib.parse.parse_qs(parsed.query).get('source_type', [FIXED_SOURCE_TYPE])[0].strip() or FIXED_SOURCE_TYPE
            self._send_json(app_session_profile_status(source_type))
            return
        if parsed.path == '/api/startup-session-check':
            params = urllib.parse.parse_qs(parsed.query)
            source_type = params.get('source_type', [FIXED_SOURCE_TYPE])[0].strip()
            result = startup_session_check(source_type) if source_type else startup_all_session_checks()
            self._send_json(result, HTTPStatus.OK if result.get('ok') else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith('/static/'):
            self._send_static(STATIC_DIR / parsed.path.removeprefix('/static/'))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length) if length else b'{}'
        payload = json.loads(raw.decode('utf-8'))

        if parsed.path == '/api/watchers':
            required = ['name', 'hotel_name', 'source_type', 'target_url', 'notify_target']
            missing = [field for field in required if not str(payload.get(field, '')).strip()]
            if missing:
                self._send_json({'error': f'缺少字段: {", ".join(missing)}'}, HTTPStatus.BAD_REQUEST)
                return
            try:
                request_headers = payload.get('request_headers', '').strip()
                if request_headers:
                    json.loads(request_headers)
                threshold = payload.get('threshold_price')
                payload['threshold_price'] = None if threshold in ('', None) else float(threshold)
                min_expected = payload.get('min_expected_price')
                payload['min_expected_price'] = None if min_expected in ('', None) else float(min_expected)
                interval_minutes = payload.get('poll_interval_minutes')
                payload['poll_interval_minutes'] = clamp_public_poll_interval(interval_minutes or DEFAULT_POLL_INTERVAL_MINUTES)
                payload['notify_type'] = (payload.get('notify_type') or 'feishu').strip() or 'feishu'
                active_count = sum(1 for item in list_watchers() if item.is_active)
                if active_count >= MAX_WATCHERS:
                    self._send_json({'error': f'Open-source edition limits active watchers to {MAX_WATCHERS}.'}, HTTPStatus.BAD_REQUEST)
                    return
                payload['use_browser'] = True
                if str(payload.get('source_type', '')).strip() == 'agoda':
                    payload['use_app_session_profile'] = True
                    payload['use_local_chrome_profile'] = False
                    payload['chrome_profile_name'] = DEFAULT_CHROME_PROFILE
                watcher_id = create_watcher(payload)
                self._send_json({'ok': True, 'id': watcher_id}, HTTPStatus.CREATED)
            except json.JSONDecodeError:
                self._send_json({'error': '高级设置里的请求头必须是合法 JSON'}, HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == '/api/update-watcher':
            required = ['id', 'name', 'hotel_name', 'source_type', 'target_url', 'notify_target']
            missing = [field for field in required if field != 'id' and not str(payload.get(field, '')).strip()]
            if missing:
                self._send_json({'error': f'缺少字段: {", ".join(missing)}'}, HTTPStatus.BAD_REQUEST)
                return
            watcher = find_watcher(int(payload.get('id', 0)))
            if not watcher:
                self._send_json({'error': '监控任务不存在'}, HTTPStatus.NOT_FOUND)
                return
            try:
                request_headers = payload.get('request_headers', '').strip()
                if request_headers:
                    json.loads(request_headers)
                threshold = payload.get('threshold_price')
                payload['threshold_price'] = None if threshold in ('', None) else float(threshold)
                min_expected = payload.get('min_expected_price')
                payload['min_expected_price'] = None if min_expected in ('', None) else float(min_expected)
                interval_minutes = payload.get('poll_interval_minutes')
                payload['poll_interval_minutes'] = clamp_public_poll_interval(interval_minutes or DEFAULT_POLL_INTERVAL_MINUTES)
                payload['notify_type'] = (payload.get('notify_type') or 'feishu').strip() or 'feishu'
                payload['use_browser'] = True
                if str(payload.get('source_type', '')).strip() == 'agoda':
                    payload['use_app_session_profile'] = True
                    payload['use_local_chrome_profile'] = False
                    payload['chrome_profile_name'] = DEFAULT_CHROME_PROFILE
                update_watcher(int(payload['id']), payload)
                self._send_json({'ok': True, 'id': int(payload['id'])})
            except json.JSONDecodeError:
                self._send_json({'error': '高级设置里的请求头必须是合法 JSON'}, HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == '/api/check-now':
            watcher = find_watcher(int(payload['id']))
            if not watcher:
                self._send_json({'error': '监控任务不存在'}, HTTPStatus.NOT_FOUND)
                return
            result = check_watcher(watcher)
            self._send_json(result, HTTPStatus.OK if result.get('ok') else HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == '/api/preview-rooms':
            try:
                source_type = str(payload.get('source_type', 'generic')).strip() or 'generic'
                target_url = str(payload.get('target_url', '')).strip()
                if not target_url:
                    self._send_json({'error': '请先填写酒店链接'}, HTTPStatus.BAD_REQUEST)
                    return
                target_url = ensure_public_safe_target_url(target_url)
                request_headers = str(payload.get('request_headers', '')).strip()
                headers = normalize_headers(request_headers or '{}', source_type)
                headers = merge_cookie_into_headers(headers, str(payload.get('cookie', '')))
                if source_type == 'agoda' or bool(payload.get('use_app_session_profile')):
                    text = browser_fetch_with_app_session(target_url, headers, source_type, str(payload.get('room_type_keyword', '')).strip())
                elif bool(payload.get('use_local_chrome_profile')):
                    profile_name = str(payload.get('chrome_profile_name') or DEFAULT_CHROME_PROFILE).strip() or DEFAULT_CHROME_PROFILE
                    text = browser_fetch_with_local_profile(target_url, headers, source_type, profile_name, str(payload.get('room_type_keyword', '')).strip())
                else:
                    text = browser_fetch(target_url, headers, source_type, str(payload.get('room_type_keyword', '')).strip())
                items = extract_room_candidates(text)
                if not items:
                    self._send_json({'error': '暂时没有自动识别到房型，请直接手填房型关键词或补 Cookie'}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({'items': items})
            except json.JSONDecodeError:
                self._send_json({'error': '高级设置里的请求头必须是合法 JSON'}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == '/api/test-feishu':
            webhook = str(payload.get('notify_target', '')).strip()
            if not webhook:
                self._send_json({'error': '请先填写飞书 Webhook'}, HTTPStatus.BAD_REQUEST)
                return
            temp = Watcher(
                id=0, name='测试提醒', hotel_name='测试酒店', source_type='generic', target_url='https://example.com',
                room_type_keyword='豪华大床房', room_type_meta='含早 | 免费取消', price_pattern='', currency='CNY',
                notify_type='feishu', notify_target=webhook, threshold_price=999.0, min_expected_price=None,
                poll_interval_minutes=DEFAULT_POLL_INTERVAL_MINUTES,
                request_headers='{}', use_local_chrome_profile=0, chrome_profile_name=DEFAULT_CHROME_PROFILE,
                use_app_session_profile=0, use_browser=1, last_error=None, is_active=1, last_price=None,
                last_checked_at=None, last_notified_price=None, last_price_note=None, created_at=utc_now(), updated_at=utc_now(),
            )
            try:
                send_notification(temp, 888.0, is_test=True)
                self._send_json({'ok': True})
            except ssl.SSLCertVerificationError:
                self._send_json({'error': '本机 Python 证书校验失败，请使用最新版启动器重启服务后重试。'}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({'error': f'飞书测试发送失败: {exc}'}, HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == '/api/start-agoda-login':
            source_type = str(payload.get('source_type', 'agoda')).strip() or 'agoda'
            target_url = ensure_public_safe_target_url(str(payload.get('target_url', '')).strip() or session_default_target_url(source_type))
            result = launch_login_and_save_session(source_type, target_url)
            self._send_json(result)
            return

        if parsed.path == '/api/finish-agoda-login':
            source_type = str(payload.get('source_type', 'agoda')).strip() or 'agoda'
            result = finish_login_and_save_session(source_type)
            self._send_json(result)
            return

        if parsed.path == '/api/verify-app-session':
            try:
                source_type = str(payload.get('source_type', 'agoda')).strip() or 'agoda'
                target_url = str(payload.get('target_url', '')).strip()
                if not target_url:
                    self._send_json({'error': '请先填写酒店链接'}, HTTPStatus.BAD_REQUEST)
                    return
                target_url = ensure_public_safe_target_url(target_url)
                request_headers = str(payload.get('request_headers', '')).strip()
                headers = normalize_headers(request_headers or '{}', source_type)
                headers = merge_cookie_into_headers(headers, str(payload.get('cookie', '')))
                result = verify_app_session(target_url, headers, source_type)
                status = HTTPStatus.OK if result.get('ok') else HTTPStatus.BAD_REQUEST
                self._send_json(result, status)
            except json.JSONDecodeError:
                self._send_json({'error': '高级设置里的请求头必须是合法 JSON'}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                payload = {'error': str(exc)}
                debug_payload = getattr(exc, 'debug_payload', None)
                if debug_payload:
                    payload['debug'] = debug_payload
                self._send_json(payload, HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == '/api/debug-app-session-screenshot':
            try:
                source_type = str(payload.get('source_type', 'agoda')).strip() or 'agoda'
                target_url = str(payload.get('target_url', '')).strip()
                if not target_url:
                    self._send_json({'error': '请先填写酒店链接'}, HTTPStatus.BAD_REQUEST)
                    return
                target_url = ensure_public_safe_target_url(target_url)
                request_headers = str(payload.get('request_headers', '')).strip()
                headers = normalize_headers(request_headers or '{}', source_type)
                headers = merge_cookie_into_headers(headers, str(payload.get('cookie', '')))
                room_keyword = str(payload.get('room_type_keyword', '')).strip()
                focus_room = bool(payload.get('focus_room'))
                result = browser_capture_with_app_session(target_url, headers, source_type, room_keyword, focus_room=focus_room)
                self._send_json({'ok': True, **result})
            except json.JSONDecodeError:
                self._send_json({'error': '高级设置里的请求头必须是合法 JSON'}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == '/api/toggle':
            set_watcher_active(int(payload['id']), 1 if bool(payload.get('is_active')) else 0)
            self._send_json({'ok': True})
            return

        if parsed.path == '/api/delete':
            delete_watcher(int(payload['id']))
            self._send_json({'ok': True})
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    init_db()
    poller = Poller()
    poller.start()
    server = ThreadingHTTPServer(('127.0.0.1', APP_PORT), AppHandler)
    print('Hotel price alert running at http://127.0.0.1:8767')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        server.server_close()


if __name__ == '__main__':
    main()
