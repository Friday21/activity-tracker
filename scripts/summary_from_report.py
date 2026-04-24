#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SECTION_HEADERS = {
    'By category (visit count):',
    'By category (estimated time):',
    'By source:',
}

STOP_HEADERS = {
    'Top domains (visit count):',
    'Top domains (estimated time):',
    'Top URLs (visit count):',
    'Top URLs (estimated time):',
    'Sample visits:',
}


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: summary_from_report.py <report.txt>', file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f'File not found: {path}', file=sys.stderr)
        sys.exit(1)

    lines = path.read_text(encoding='utf-8').splitlines()
    keep: list[str] = []
    in_section = False

    for line in lines:
        stripped = line.rstrip()

        # Always include report header lines
        if (
            stripped.startswith('Browser activity report - ')
            or stripped.startswith('Total unique visits:')
            or stripped.startswith('Total estimated browsing time:')
            or stripped.startswith('Input mode:')
        ):
            keep.append(stripped)
            continue

        # Stop collecting once we reach detail sections
        if stripped in STOP_HEADERS:
            break

        # Start collecting section (category / source)
        if stripped in SECTION_HEADERS:
            keep.append('')
            keep.append(stripped)
            in_section = True
            continue

        # Collect list items within an active section
        if in_section and stripped.startswith('- '):
            keep.append(stripped)
            continue

        # A non-list, non-empty line resets section state
        if stripped and not stripped.startswith('- '):
            in_section = False

    print('\n'.join(keep).strip() + '\n')


if __name__ == '__main__':
    main()
