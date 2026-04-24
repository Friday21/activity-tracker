#!/usr/bin/env python3
"""Fetch Google My Activity data using Playwright.

Usage:
    .venv/bin/python3 scripts/fetch_activity.py              # headed (visible browser)
    .venv/bin/python3 scripts/fetch_activity.py --headless   # headless (scheduled runs)

First-time login:
    .venv/bin/python3 scripts/setup_browser.py
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

from playwright.sync_api import sync_playwright, Page

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / 'inputs' / 'activity'
PROFILE_DIR = ROOT / '.browser_profile'
ACTIVITY_URL = 'https://myactivity.google.com/myactivity?pli=1'
MAX_FETCH_DAYS = 5

TIME_RE = re.compile(r'^(\d{1,2}:\d{2})(?:\s*[•·].*)?$')
ACTION_RE = re.compile(r'^(访问了|搜索了|观看了|查看了|播放了)\s+(.+)$')
DATE_HEADER_RE = re.compile(r'^(\d{1,2})月(\d{1,2})日')

IGNORE_LINES = {
    '某些活动可能尚未显示', '详细信息', '删除', '按日期和产品过滤',
    '我的活动记录', '我的 Google 活动记录', 'Google 会保护您的隐私和安全。',
}


# ── URL helpers ───────────────────────────────────────────────────────────────

def unwrap_google_url(href: str) -> str:
    try:
        u = urlparse(href)
        if u.netloc.endswith('google.com') and u.path == '/url':
            q = parse_qs(u.query).get('q')
            if q:
                return unquote(q[0])
    except Exception:
        pass
    return href


def infer_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ''


def sanitize_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        safe_query = ''
        if parsed.netloc.endswith('youtube.com') and parsed.path == '/watch':
            v = parse_qs(parsed.query).get('v', [''])[0]
            safe_query = f'v={v}' if v else ''
        elif parsed.netloc.endswith('google.com') and parsed.path == '/search':
            q = parse_qs(parsed.query).get('q', [''])[0]
            safe_query = f'q={q}' if q else ''
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', safe_query, ''))
    except Exception:
        return url


# ── Date helpers ──────────────────────────────────────────────────────────────

def parse_day_header(line: str, now: datetime) -> date | None:
    """Return the date for a day-header line, or None if not a header."""
    stripped = line.strip()
    if stripped == '今天':
        return now.date()
    if stripped == '昨天':
        return (now - timedelta(days=1)).date()
    m = DATE_HEADER_RE.match(stripped)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        candidate = date(year, month, day)
        if candidate > now.date():
            candidate = date(year - 1, month, day)
        return candidate
    return None



def get_visible_oldest_date(text: str, now: datetime) -> date | None:
    """Return the oldest date header visible in the page text."""
    oldest: date | None = None
    for line in text.splitlines():
        d = parse_day_header(line, now)
        if d is not None:
            if oldest is None or d < oldest:
                oldest = d
    return oldest


# ── Browser helpers ───────────────────────────────────────────────────────────

def ensure_activity_page(page: Page) -> None:
    """Navigate to Google My Activity if not already there."""
    if 'myactivity.google.com' not in page.url:
        page.goto(ACTIVITY_URL, wait_until='domcontentloaded', timeout=30_000)
        time.sleep(2)


def check_logged_in(page: Page) -> bool:
    """Return True if the user appears to be logged in to Google."""
    url = page.url
    return 'accounts.google.com' not in url and 'signin' not in url.lower()


def get_page_text(page: Page) -> str:
    return page.evaluate('() => document.body.innerText')


def get_page_links(page: Page) -> list[dict]:
    js = '''() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
      text: (a.innerText || '').trim(),
      href: a.href
    })).filter(x => x.text || x.href)'''
    return page.evaluate(js)


def scroll_until_cutoff(
    page: Page,
    max_days: int = MAX_FETCH_DAYS,
    pause_seconds: float = 1.5,
) -> None:
    """Scroll down until max_days of data is visible."""
    now = datetime.now().astimezone()
    cutoff_date = (now - timedelta(days=max_days)).date()

    for _ in range(300):
        text = get_page_text(page)
        oldest_visible = get_visible_oldest_date(text, now)

        if oldest_visible is not None and oldest_visible <= cutoff_date:
            break

        page.evaluate('window.scrollBy(0, window.innerHeight)')
        time.sleep(pause_seconds)


# ── Entry parsing ─────────────────────────────────────────────────────────────

def parse_entries(text: str, links: list[dict], now: datetime) -> list[dict]:
    """Parse all activity entries from the page, across all visible days."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Build title→URL lookup from page links
    link_pairs: list[tuple[str, str]] = []
    for link in links:
        txt = (link.get('text') or '').strip()
        href = unwrap_google_url(link.get('href') or '')
        if txt and href:
            link_pairs.append((txt, href))

    cutoff_date = (now - timedelta(days=MAX_FETCH_DAYS)).date()
    current_date: date | None = None
    entries: list[dict] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # ── Day header ─────────────────────────────────────────────────────
        d = parse_day_header(line, now)
        if d is not None:
            if d <= cutoff_date:
                break                     # stop: past max fetch window
            current_date = d
            i += 1
            continue

        # ── Skip noise ─────────────────────────────────────────────────────
        if line in IGNORE_LINES:
            i += 1
            continue

        # ── Need 3 lines + a known date ────────────────────────────────────
        if current_date is None or i + 2 >= len(lines):
            i += 1
            continue

        source = line
        action_line = lines[i + 1]
        time_line = lines[i + 2]

        m_action = ACTION_RE.match(action_line)
        m_time = TIME_RE.match(time_line)

        if not m_action or not m_time:
            i += 1
            continue

        action = m_action.group(1)
        title = m_action.group(2).strip()
        hh, mm = m_time.group(1).split(':')

        visited_dt = datetime(
            year=current_date.year,
            month=current_date.month,
            day=current_date.day,
            hour=int(hh),
            minute=int(mm),
            second=0,
            microsecond=0,
            tzinfo=now.tzinfo,
        )

        # Find best URL match
        href = ''
        for txt, candidate in link_pairs:
            if txt == title or title in txt or txt in title:
                href = candidate
                break
        url = sanitize_url(href) if href else ''
        domain = infer_domain(url) or source

        entries.append({
            'source': 'google-activity',
            'activity_source': source,
            'action': action,
            'title': title,
            'url': url,
            'domain_hint': domain,
            'visited_at': visited_dt.isoformat(),
            'captured_at': now.isoformat(),
        })
        i += 3

    return entries


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Fetch Google My Activity via Playwright')
    p.add_argument(
        '--headless', action='store_true',
        help='Headless mode (no visible browser). Requires prior login via setup_browser.py',
    )
    return p.parse_args()


def main() -> None:
    import sys

    args = parse_args()

    if not PROFILE_DIR.exists():
        print(
            'ERROR: Browser profile not found. Run first:\n'
            '    .venv/bin/python3 scripts/setup_browser.py',
            file=sys.stderr,
        )
        sys.exit(1)

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
            viewport={'width': 1280, 'height': 900},
            args=['--disable-blink-features=AutomationControlled'],
            ignore_default_args=['--enable-automation'],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            ensure_activity_page(page)

            if not check_logged_in(page):
                print(
                    'ERROR: Not logged in to Google.\n'
                    'Run:  .venv/bin/python3 scripts/setup_browser.py',
                    file=sys.stderr,
                )
                sys.exit(1)

            scroll_until_cutoff(page)
            text = get_page_text(page)
            links = get_page_links(page)
            entries = parse_entries(text, links, now)
        finally:
            context.close()

    stamp = now.strftime('%Y-%m-%dT%H-%M-%S%z')
    out = INPUT_DIR / f'capture-{stamp}.jsonl'
    written = 0
    with out.open('w', encoding='utf-8') as f:
        for item in entries:
            if not item.get('title'):
                continue
            if not item.get('url'):
                item['url'] = (
                    f'https://activity.local/{item["activity_source"]}/{item["title"]}'
                )
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
            written += 1

    print(json.dumps({'output': str(out), 'count': written}, ensure_ascii=False))


if __name__ == '__main__':
    main()
