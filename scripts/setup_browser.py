#!/usr/bin/env python3
"""First-time browser login helper.

Opens a visible Chromium window so you can log in to your Google account.
The session (cookies + local storage) is saved to .browser_profile/ and
reused by subsequent headless runs of fetch_activity.py.

Usage:
    .venv/bin/python3 scripts/setup_browser.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / '.browser_profile'
ACTIVITY_URL = 'https://myactivity.google.com/myactivity?pli=1'


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('ERROR: playwright is not installed.', file=sys.stderr)
        print('Run:  ./scripts/setup.sh', file=sys.stderr)
        sys.exit(1)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    print('=== Browser Login Setup ===')
    print(f'Profile directory: {PROFILE_DIR}')
    print()
    print('Opening Chromium...')
    print('  → Log in to your Google account if prompted.')
    print('  → Navigate to Google My Activity and verify activity is visible.')
    print('  → Press Enter in this terminal when done.')
    print()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={'width': 1280, 'height': 900},
            args=['--disable-blink-features=AutomationControlled'],
            ignore_default_args=['--enable-automation'],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(ACTIVITY_URL, wait_until='domcontentloaded', timeout=30000)

        try:
            input('Press Enter to close the browser and save your session... ')
        except (EOFError, KeyboardInterrupt):
            pass

        context.close()

    print()
    print('✅ Session saved to .browser_profile/')
    print()
    print('You can now run:')
    print('    ./run_daily.sh')
    print('or for a quick test:')
    print('    .venv/bin/python3 scripts/fetch_activity.py --headless')


if __name__ == '__main__':
    main()
