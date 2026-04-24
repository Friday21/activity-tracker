#!/usr/bin/env python3
"""Build unified per-day activity JSON by merging browser visits and
macOS/iPhone screen time into outputs/daily/YYYY-MM-DD.json.

Schema:
{
  "date": "YYYY-MM-DD",
  "updated_at": "ISO8601",
  "items": [
    {
      "category": "工作",                       # topical classification
      "title": "example.com" | "小红书",        # domain for URLs, app name for apps
      "start": "2026-04-15T10:23:45+08:00",
      "duration_seconds": 697,
      "detail": "https://example.com/path" | "小红书",
      "source": "browser" | "mac" | "iphone"
    }
  ]
}

Dedup / update: items are keyed by (start, detail). On re-run, items with
the same key are overwritten by the latest values (later source wins).

Coverage per run:
  - browser  : today + yesterday (reads outputs/data/{day}.json)
  - screentime: whatever days are under outputs/screentime/ (fetcher keeps 7d)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"
BROWSER_DIR = ROOT / "outputs" / "data"
SCREENTIME_DIR = ROOT / "outputs" / "screentime"
DAILY_DIR = ROOT / "outputs" / "daily"


def _load_json(p: Path, default):
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _browser_items(day: str) -> list[dict]:
    data = _load_json(BROWSER_DIR / f"{day}.json", [])
    items: list[dict] = []
    for v in data:
        detail = v.get("normalized_url") or v.get("url") or ""
        title = v.get("domain") or ""
        if not title and detail:
            from urllib.parse import urlparse
            title = urlparse(detail).netloc or detail
        items.append({
            "category": v.get("category") or "其他",
            "title": title,
            "start": v.get("visited_at") or "",
            "duration_seconds": int(v.get("estimated_seconds") or 0),
            "detail": detail,
            "source": "browser",
        })
    return items


def _screentime_items(day: str) -> list[dict]:
    d = _load_json(SCREENTIME_DIR / f"{day}.json", {})
    raw = d.get("items", []) if isinstance(d, dict) else []
    items: list[dict] = []
    for v in raw:
        device = v.get("device") or ""
        source = "iphone" if device.startswith("iPhone") else "mac"
        if source == "mac" and v.get("bundle_id") == "com.google.Chrome":
            continue
        items.append({
            "category": v.get("classification") or "其他",
            "title": v.get("app_name") or v.get("detail") or "",
            "start": v.get("start") or "",
            "duration_seconds": int(v.get("duration_seconds") or 0),
            "detail": v.get("detail") or "",
            "source": source,
        })
    return items


def _merge_day(day: str, new_items: list[dict]) -> int:
    target = DAILY_DIR / f"{day}.json"
    existing = _load_json(target, {})
    existing_items = (
        existing.get("items", []) if isinstance(existing, dict) else []
    )

    # (start, detail) is the unique key. Later writes overwrite earlier
    # entries so the latest run's values win (duration refinements, etc.).
    keyed: dict[tuple[str, str], dict] = {}
    for it in existing_items:
        keyed[(it.get("start", ""), it.get("detail", ""))] = it
    for it in new_items:
        key = (it.get("start", ""), it.get("detail", ""))
        if not key[0] or not key[1]:
            continue
        keyed[key] = it

    merged = sorted(
        (it for it in keyed.values()
         if not (it.get("source") == "mac" and it.get("detail") == "Chrome")),
        key=lambda x: x.get("start", ""),
    )
    payload = {
        "date": day,
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": merged,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return len(merged)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build unified daily JSON")
    parser.add_argument("--browser-days", type=int, default=2,
                        help="Browser lookback (default: 2 = today+yesterday)")
    args = parser.parse_args()

    cfg = _load_json(CONFIG, {})
    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    today = dt.datetime.now(tz).date()

    days: set[str] = set()
    for i in range(args.browser_days):
        days.add((today - dt.timedelta(days=i)).isoformat())
    if SCREENTIME_DIR.exists():
        for p in SCREENTIME_DIR.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"):
            days.add(p.stem)

    if not days:
        print("[build_daily] no source data found")
        return 0

    for day in sorted(days):
        browser = _browser_items(day)
        screen = _screentime_items(day)
        if not browser and not screen:
            continue
        total = _merge_day(day, browser + screen)
        print(f"[build_daily] {day}: browser={len(browser)} screentime={len(screen)} merged={total} → outputs/daily/{day}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
