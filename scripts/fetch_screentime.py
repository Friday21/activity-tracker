#!/usr/bin/env python3
"""Fetch macOS Screen Time usage (local + iPhone/iPad synced via iCloud) from
knowledgeC.db and write per-day JSON files under outputs/screentime/.

Schema per output file (outputs/screentime/YYYY-MM-DD.json):
{
  "date": "YYYY-MM-DD",
  "updated_at": "ISO8601",
  "items": [
    {
      "category": "app" | "web" | "notification",
      "stream": "/app/usage" | "/app/inFocus" | "/app/webUsage" | "/safari/history" | ...,
      "bundle_id": "com.example.app",
      "app_name": "Example",
      "device": "Mac" | "iPhone" | "iPad" | "unknown",
      "device_id": "ABCD-EFGH-...",
      "start": "YYYY-MM-DDTHH:MM:SS+08:00",
      "end":   "YYYY-MM-DDTHH:MM:SS+08:00",
      "duration_seconds": int,
      "classification": "工作" | ...,
      "detail": "<url or app name>",
      "domain": "example.com"  # web only
      "title":  "..."          # web only, optional
    }
  ]
}

Dedup: an item is uniquely identified by (start, detail). On re-run, existing
items with the same (start, detail) are preserved; new items are appended.

Requires: Full Disk Access for the process that runs this script (the
interpreter / terminal / launchd agent) — macOS TCC protects knowledgeC.db.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

APPLE_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01 UTC

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DOMAIN_CATEGORIES_PATH = PROJECT_ROOT / "domain_categories.json"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "screentime"
UNCLASSIFIED_PATH = PROJECT_ROOT / "outputs" / "unclassified_domains.txt"

KNOWLEDGE_DB = Path.home() / "Library/Application Support/Knowledge/knowledgeC.db"
BIOME_APPINFOCUS_DIR = Path.home() / "Library/Biome/streams/restricted/App.InFocus/remote"

# Maximum gap between consecutive App.InFocus events to count as active usage.
# Gaps larger than this (phone sleeping) are capped here to avoid crediting
# idle time to the last foreground app.  Calibrated to match Screen Time totals.
BIOME_MAX_GAP_SECONDS = 180

# watchOS carousel bundle prefix — UUIDs whose top bundle is this are Watches,
# not iPhones/iPads, and should be excluded from iPhone usage computation.
_WATCH_BUNDLE_PREFIX = "com.apple.carousel."

# SpringBoard system states that don't represent real app usage.
_SPRINGBOARD_SKIP = re.compile(
    r"com\.apple\.(SpringBoard|springboard)\.",
    re.IGNORECASE,
)

# Streams we care about. knowledgeC has many more; keep scope tight.
# - /app/usage       : Mac app foreground usage (NULL ZSOURCE)
# - /app/inFocus     : Mac focused app (not always present)
# - /app/webUsage    : Safari web usage (older macOS)
# - /safari/history  : Safari history (older macOS)
# - /app/intents     : iPhone/iPad Siri/Shortcut intents, ZSOURCE.ZDEVICEID != NULL
# - /app/mediaUsage  : media playback events
# - /notification/usage : notification received/interacted
STREAMS = (
    "/app/usage",
    "/app/inFocus",
    "/app/webUsage",
    "/safari/history",
    "/app/intents",
    "/app/mediaUsage",
    "/notification/usage",
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _categorize(domain_or_bundle: str, categories: dict, domain_map: dict) -> str:
    if not domain_or_bundle:
        return "其他"
    key = domain_or_bundle.lower()
    if key in domain_map:
        return domain_map[key]
    for cat, spec in categories.items():
        for d in spec.get("domains", []):
            if d in key:
                return cat
        for kw in spec.get("keywords", []):
            if kw in key:
                return cat
    return "其他"


def _bundle_to_name(bundle: str) -> str:
    if not bundle:
        return ""
    known = {
        "com.apple.mobilesafari": "Safari",
        "com.apple.Safari": "Safari",
        "com.apple.MobileSMS": "Messages",
        "com.apple.mobilephone": "Phone",
        "com.apple.mobilemail": "Mail",
        "com.apple.mail": "Mail",
        "com.apple.mobilecal": "Calendar",
        "com.apple.mobilenotes": "Notes",
        "com.apple.Notes": "Notes",
        "com.apple.reminders": "Reminders",
        "com.apple.Maps": "Maps",
        "com.apple.Music": "Music",
        "com.apple.podcasts": "Podcasts",
        "com.apple.mobileslideshow": "Photos",
        "com.apple.Photos": "Photos",
        "com.apple.camera": "Camera",
        "com.apple.weather": "Weather",
        "com.apple.findmy": "Find My",
        "com.apple.Preferences": "Settings",
        "com.apple.systempreferences": "System Settings",
        "com.apple.finder": "Finder",
        "com.apple.Terminal": "Terminal",
        "com.apple.mobiletimer": "时钟",
        "com.apple.assistant_service": "Siri",
        "com.apple.InCallService": "电话",
        "com.googlecode.iterm2": "iTerm",
        "com.tinyspeck.slackmacgap": "Slack",
        "com.microsoft.VSCode": "VS Code",
        "com.google.Chrome": "Chrome",
        "company.thebrowser.Browser": "Arc",
        "org.mozilla.firefox": "Firefox",
        "ru.keepcoder.Telegram": "Telegram",
        "net.whatsapp.WhatsApp": "WhatsApp",
        "com.tencent.xin": "WeChat",
        "com.tencent.QQMusic": "QQ音乐",
        "com.netease.cloudmusic": "网易云音乐",
        "com.burbn.instagram": "Instagram",
        "com.zhiliaoapp.musically": "TikTok",
        "com.ss.iphone.ugc.Aweme": "抖音",
        "com.xingin.discover": "小红书",
        "tv.twitch": "Twitch",
        "com.google.ios.youtube": "YouTube",
        "com.netflix.Netflix": "Netflix",
        "com.spotify.client": "Spotify",
        "com.anthropic.claudefordesktop": "Claude",
        "com.anthropic.claude": "Claude",
        "tech.miidii.MDClock": "MD Clock",
    }
    if bundle in known:
        return known[bundle]
    # heuristic: last segment of reverse-DNS
    parts = bundle.split(".")
    return parts[-1] if parts else bundle


def _apple_to_iso(ts: float, tz: ZoneInfo) -> str:
    unix = ts + APPLE_EPOCH
    return dt.datetime.fromtimestamp(unix, tz=tz).isoformat(timespec="seconds")


def _apple_to_date(ts: float, tz: ZoneInfo) -> str:
    unix = ts + APPLE_EPOCH
    return dt.datetime.fromtimestamp(unix, tz=tz).strftime("%Y-%m-%d")


def _device_label(device_id: str | None) -> str:
    # knowledgeC convention: local Mac events have NULL ZSOURCE or NULL
    # ZDEVICEID. Anything with a concrete UUID was synced from another
    # iCloud device (iPhone or iPad — we can't distinguish from DB alone).
    if not device_id:
        return "Mac"
    return "iPhone/iPad"


def _parse_segb_events(path: Path) -> list[tuple[float, str]]:
    """Parse a Biome SEGB file and return (apple_epoch_float, bundle_id) pairs.

    The SEGB format stores protobuf records.  We scan for:
      - tag 0x21 (field 4, 64-bit fixed) = app-focus timestamp
      - tag 0x32 (field 6, length-delimited) = foreground bundle ID
    within a 200-byte window around each timestamp.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if data[:4] != b"SEGB":
        return []

    events: list[tuple[float, str]] = []
    i = 32  # skip 32-byte header
    end = len(data) - 12
    while i < end:
        if data[i] != 0x21:
            i += 1
            continue
        ts_val = struct.unpack_from("<d", data, i + 1)[0]
        # Plausible Apple-epoch range: 2020-01-01 to 2030-01-01
        if not (600_000_000.0 < ts_val < 950_000_000.0):
            i += 1
            continue
        # Search for bundle ID (tag 0x32) in surrounding bytes
        win_start = max(32, i - 200)
        win = data[win_start: i + 200]
        bundle: str | None = None
        j = 0
        while j < len(win) - 2:
            if win[j] == 0x32:
                length = win[j + 1]
                if 4 < length < 80 and j + 2 + length <= len(win):
                    try:
                        s = win[j + 2: j + 2 + length].decode("ascii")
                        if s.startswith(("com.", "net.", "org.")):
                            bundle = s
                    except (UnicodeDecodeError, ValueError):
                        pass
            j += 1
        if bundle:
            events.append((ts_val, bundle))
        i += 9  # advance past the 0x21 + 8-byte double
    return events


def _detect_iphone_uuid_dirs() -> list[Path]:
    """Return Biome App.InFocus/remote UUID directories that look like iPhones.

    Excludes Apple Watch UUIDs (top bundle = com.apple.carousel.*) and empty dirs.
    """
    if not BIOME_APPINFOCUS_DIR.exists():
        return []
    result: list[Path] = []
    for uuid_dir in BIOME_APPINFOCUS_DIR.iterdir():
        if not uuid_dir.is_dir():
            continue
        bundle_counts: dict[str, int] = {}
        for segb in uuid_dir.iterdir():
            if not segb.is_file():
                continue
            for _, bundle in _parse_segb_events(segb):
                bundle_counts[bundle] = bundle_counts.get(bundle, 0) + 1
        if not bundle_counts:
            continue
        top_bundle = max(bundle_counts, key=lambda b: bundle_counts[b])
        if top_bundle.startswith(_WATCH_BUNDLE_PREFIX):
            continue  # Apple Watch
        result.append(uuid_dir)
    return result


def _biome_iphone_items(
    since_apple: float,
    tz: ZoneInfo,
    categories: dict,
    domain_map: dict,
    unclassified: set[str],
) -> dict[str, list[dict]]:
    """Parse Biome App.InFocus remote SEGB files for iPhone usage data.

    Returns a dict mapping date string → list of screentime-format items.
    Each item represents one App.InFocus session with estimated duration.
    """
    uuid_dirs = _detect_iphone_uuid_dirs()
    if not uuid_dirs:
        return {}

    # Collect all events from all iPhone UUIDs in the requested time range
    all_events: list[tuple[float, str]] = []
    for uuid_dir in uuid_dirs:
        for segb in sorted(uuid_dir.iterdir()):
            if not segb.is_file():
                continue
            for ts_val, bundle in _parse_segb_events(segb):
                if ts_val >= since_apple:
                    all_events.append((ts_val, bundle))

    if not all_events:
        return {}

    all_events.sort(key=lambda x: x[0])

    by_day: dict[str, list[dict]] = {}
    for idx, (ts_val, bundle) in enumerate(all_events):
        # Skip SpringBoard/home-screen system states — they act as timing
        # boundaries but don't represent real app usage.
        if _SPRINGBOARD_SKIP.match(bundle):
            continue

        # Duration = time until the NEXT event of any kind (including
        # SpringBoard transitions), capped at BIOME_MAX_GAP_SECONDS to exclude
        # phone-sleep intervals.  Using all events as boundaries gives more
        # accurate per-app durations than only using real-app events.
        if idx + 1 < len(all_events):
            gap = all_events[idx + 1][0] - ts_val
            duration = max(0, min(gap, BIOME_MAX_GAP_SECONDS))
        else:
            duration = 0

        start_iso = _apple_to_iso(ts_val, tz)
        day = start_iso[:10]
        app_name = _bundle_to_name(bundle) if bundle else ""
        # Use bundle_id as detail to avoid key collision with knowledgeC intent
        # items that use app_name as their detail field.
        detail = bundle
        classification = _categorize(bundle, categories, domain_map)
        if classification == "其他" and bundle:
            unclassified.add(bundle.lower())

        item = {
            "category": "app",
            "stream": "/app/inFocus/biome",
            "bundle_id": bundle,
            "app_name": app_name,
            "device": "iPhone",
            "device_id": "",
            "start": start_iso,
            "end": _apple_to_iso(ts_val + duration, tz),
            "duration_seconds": int(duration),
            "classification": classification,
            "detail": detail,
        }
        by_day.setdefault(day, []).append(item)

    # Consolidate by (start, detail): keep the item with the highest duration.
    # Multiple Biome events can round to the same ISO-second and same bundle_id;
    # the first in sorted order often has 0-gap while a nearby duplicate has
    # the real duration.  We must surface the best value before handing off to
    # _merge_into_day_file (which keeps the first-seen key).
    consolidated: dict[str, list[dict]] = {}
    for day, items in by_day.items():
        best: dict[tuple[str, str], dict] = {}
        for it in items:
            key = (it["start"], it["detail"])
            if key not in best or it["duration_seconds"] > best[key]["duration_seconds"]:
                best[key] = it
        consolidated[day] = list(best.values())
    return consolidated


def _copy_db(src: Path) -> Path:
    """Copy knowledgeC.db (+ -wal/-shm if present) to a temp dir to avoid locks."""
    tmpdir = Path(tempfile.mkdtemp(prefix="knowledgeC_"))
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, tmpdir / (src.name + suffix))
    return tmpdir / src.name


def _list_metadata_columns(con: sqlite3.Connection) -> set[str]:
    try:
        rows = con.execute("PRAGMA table_info(ZSTRUCTUREDMETADATA)").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[1] for r in rows}


def _fetch_rows(con: sqlite3.Connection, since_apple: float) -> list[dict]:
    meta_cols = _list_metadata_columns(con)
    # Different macOS versions use slightly different column names for web
    # usage metadata. We probe for whichever exists.
    url_col = next(
        (c for c in meta_cols if c.endswith("WEBPAGEURL") or c.endswith("WEBPAGEURLKEY") or c.endswith("__URL") or c.endswith("URLKEY") or c.endswith("PAGEURLKEY")),
        None,
    )
    title_col = next(
        (c for c in meta_cols if c.endswith("WEBPAGETITLE") or c.endswith("WEBPAGETITLEKEY") or c.endswith("__TITLE") or c.endswith("TITLEKEY") or c.endswith("PAGETITLEKEY")),
        None,
    )
    domain_col = next(
        (c for c in meta_cols if c.endswith("DOMAIN") or c.endswith("DOMAINKEY")),
        None,
    )

    # Always-safe base columns.
    base_sel = [
        "ZOBJECT.Z_PK AS pk",
        "ZOBJECT.ZSTREAMNAME AS stream",
        "ZOBJECT.ZVALUESTRING AS value",
        "ZOBJECT.ZSTARTDATE AS start_ts",
        "ZOBJECT.ZENDDATE AS end_ts",
        "ZOBJECT.ZSECONDSFROMGMT AS tz_offset",
        "ZSOURCE.ZDEVICEID AS device_id",
        "ZSOURCE.ZBUNDLEID AS source_bundle",
    ]
    if url_col:
        base_sel.append(f'ZSTRUCTUREDMETADATA."{url_col}" AS url')
    if title_col:
        base_sel.append(f'ZSTRUCTUREDMETADATA."{title_col}" AS title')
    if domain_col:
        base_sel.append(f'ZSTRUCTUREDMETADATA."{domain_col}" AS domain')

    placeholders = ",".join("?" * len(STREAMS))
    sql = f"""
        SELECT {', '.join(base_sel)}
        FROM ZOBJECT
        LEFT JOIN ZSOURCE ON ZOBJECT.ZSOURCE = ZSOURCE.Z_PK
        LEFT JOIN ZSTRUCTUREDMETADATA ON ZOBJECT.ZSTRUCTUREDMETADATA = ZSTRUCTUREDMETADATA.Z_PK
        WHERE ZOBJECT.ZSTREAMNAME IN ({placeholders})
          AND ZOBJECT.ZSTARTDATE >= ?
        ORDER BY ZOBJECT.ZSTARTDATE ASC
    """
    rows = con.execute(sql, (*STREAMS, since_apple)).fetchall()
    cols = [d[0] for d in con.execute(sql, (*STREAMS, since_apple)).description]
    return [dict(zip(cols, r)) for r in rows]




def _row_to_item(row: dict, tz: ZoneInfo,
                 categories: dict, domain_map: dict,
                 unclassified: set[str]) -> dict | None:
    start_ts = row.get("start_ts")
    end_ts = row.get("end_ts")
    if start_ts is None or end_ts is None:
        return None
    stream = row.get("stream") or ""
    value = row.get("value") or ""
    url = row.get("url") or ""
    title = row.get("title") or ""
    domain = row.get("domain") or ""
    source_bundle = row.get("source_bundle") or ""

    # Prefer ZSOURCE.ZBUNDLEID when present (iPhone intents put the real
    # bundle here while ZVALUESTRING holds the intent verb/name).
    if source_bundle:
        bundle = source_bundle
        intent_label = value if value and "." not in value else ""
    elif value and "." in value:
        bundle = value
        intent_label = ""
    else:
        bundle = ""
        intent_label = value

    app_name = _bundle_to_name(bundle) if bundle else ""

    is_web = stream in ("/app/webUsage", "/safari/history") or bool(url)

    if stream == "/app/intents":
        category = "intent"
    elif stream == "/notification/usage":
        category = "notification"
    elif stream == "/app/mediaUsage":
        category = "media"
    elif is_web:
        category = "web"
    else:
        category = "app"

    if is_web:
        detail = url or value or ""
        if not domain and detail:
            try:
                domain = urlparse(detail).netloc
            except Exception:
                domain = ""
        classify_key = domain or bundle
    else:
        detail = app_name or bundle or value or stream
        classify_key = bundle or value

    classification = _categorize(classify_key, categories, domain_map)
    if classification == "其他" and classify_key:
        unclassified.add(classify_key.lower())

    device_id = row.get("device_id") or ""
    device = _device_label(device_id)

    start_iso = _apple_to_iso(start_ts, tz)
    end_iso = _apple_to_iso(end_ts, tz)
    duration = max(0, int(round(end_ts - start_ts)))

    item = {
        "category": category,
        "stream": stream,
        "bundle_id": bundle,
        "app_name": app_name,
        "device": device,
        "device_id": device_id,
        "start": start_iso,
        "end": end_iso,
        "duration_seconds": duration,
        "classification": classification,
        "detail": detail,
    }
    if intent_label:
        item["intent"] = intent_label
    if is_web:
        item["domain"] = domain
        if title:
            item["title"] = title
    elif title:
        item["title"] = title
    return item


def _merge_into_day_file(day_path: Path, new_items: list[dict]) -> tuple[int, int]:
    """Merge new_items into day_path. Returns (added, kept)."""
    existing = _load_json(day_path)
    existing_items = existing.get("items", []) if isinstance(existing, dict) else []
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for it in existing_items:
        key = (it.get("start", ""), it.get("detail", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(it)
    added = 0
    for it in new_items:
        key = (it.get("start", ""), it.get("detail", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(it)
        added += 1
    merged.sort(key=lambda x: x.get("start", ""))
    payload = {
        "date": day_path.stem,
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": merged,
    }
    day_path.parent.mkdir(parents=True, exist_ok=True)
    with day_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return added, len(merged)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch screen time from knowledgeC.db")
    parser.add_argument("--days", type=int, default=7,
                        help="Look back this many days (default: 7)")
    parser.add_argument("--db", type=Path, default=KNOWLEDGE_DB,
                        help="Path to knowledgeC.db")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR,
                        help="Output directory for per-day JSON")
    args = parser.parse_args()

    cfg = _load_json(CONFIG_PATH)
    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    categories = cfg.get("categories", {})
    domain_map = _load_json(DOMAIN_CATEGORIES_PATH)

    if not args.db.exists():
        print(f"ERROR: knowledgeC.db not found at {args.db}", file=sys.stderr)
        return 2
    if not os.access(args.db, os.R_OK):
        print(
            "ERROR: cannot read knowledgeC.db — grant Full Disk Access to the "
            "process running this script.\n"
            "  System Settings → Privacy & Security → Full Disk Access → add "
            "Terminal / iTerm / Python / the launchd agent that runs "
            "run_daily.sh.\n"
            f"  Path: {args.db}",
            file=sys.stderr,
        )
        return 3

    try:
        db_copy = _copy_db(args.db)
    except PermissionError as e:
        print(f"ERROR: copy failed ({e}). Full Disk Access required.", file=sys.stderr)
        return 3

    try:
        con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        since_unix = dt.datetime.now().timestamp() - args.days * 86400
        since_apple = since_unix - APPLE_EPOCH
        rows = _fetch_rows(con, since_apple)
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass
        shutil.rmtree(db_copy.parent, ignore_errors=True)

    # Group items by local date, track unclassified keys for later review.
    unclassified: set[str] = set()
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        item = _row_to_item(row, tz, categories, domain_map, unclassified)
        if item is None:
            continue
        day = item["start"][:10]
        by_day.setdefault(day, []).append(item)

    # Augment with iPhone App.InFocus data from Biome (provides real durations,
    # unlike the 0-duration intents from knowledgeC.db).
    biome_by_day = _biome_iphone_items(since_apple, tz, categories, domain_map, unclassified)
    biome_total = sum(len(v) for v in biome_by_day.values())
    if biome_total:
        for day, items in biome_by_day.items():
            by_day.setdefault(day, []).extend(items)
        print(f"[screentime] biome: +{biome_total} iPhone App.InFocus events across {len(biome_by_day)} day(s)")

    total_added = 0
    total_kept = 0
    for day, items in sorted(by_day.items()):
        day_path = args.out / f"{day}.json"
        added, kept = _merge_into_day_file(day_path, items)
        total_added += added
        total_kept += kept
        print(f"[screentime] {day}: +{added} new, {kept} total → {day_path}")

    # Merge new unclassified keys into outputs/unclassified_domains.txt so
    # the classify-unclassified-domains scheduled task can pick them up.
    if unclassified:
        existing: set[str] = set()
        if UNCLASSIFIED_PATH.exists():
            with UNCLASSIFIED_PATH.open("r", encoding="utf-8") as f:
                existing = {line.strip() for line in f if line.strip()}
        new_keys = {k for k in unclassified if k and k not in existing}
        if new_keys:
            UNCLASSIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
            merged = sorted(existing | new_keys)
            with UNCLASSIFIED_PATH.open("w", encoding="utf-8") as f:
                for k in merged:
                    f.write(k + "\n")
            print(f"[screentime] unclassified: +{len(new_keys)} new keys → {UNCLASSIFIED_PATH}")

    print(f"[screentime] done: {len(by_day)} day(s), +{total_added} new items, {total_kept} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
