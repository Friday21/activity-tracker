#!/usr/bin/env python3
"""Upload unified daily JSON files to your own HTTP API.

Reads `upload.endpoint` and `upload.open_id` from config.json. The endpoint
may contain a literal `{open_id}` placeholder, which will be substituted.

Usage:
    scripts/upload.py 2026-04-23                 # upload one day
    scripts/upload.py 2026-04-22 2026-04-23      # upload several
    scripts/upload.py --all                      # upload every outputs/daily/*.json
    scripts/upload.py --since 2026-04-20         # upload from date onward
    scripts/upload.py --dry-run 2026-04-23       # show what would be sent
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
DAILY_DIR = ROOT / "outputs" / "daily"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERROR: config.json not found at {CONFIG_PATH}", file=sys.stderr)
        print("       Copy config.example.json to config.json and fill it in.", file=sys.stderr)
        sys.exit(2)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_endpoint(cfg: dict) -> str:
    up = cfg.get("upload", {})
    if not up.get("enabled", False):
        print("[upload] disabled in config.json (upload.enabled = false)", file=sys.stderr)
        sys.exit(0)
    endpoint = up.get("endpoint", "").strip()
    open_id = up.get("open_id", "").strip()
    if not endpoint:
        print("ERROR: upload.endpoint is empty in config.json", file=sys.stderr)
        sys.exit(2)
    if "{open_id}" in endpoint:
        if not open_id:
            print("ERROR: upload.endpoint contains {open_id} but upload.open_id is empty",
                  file=sys.stderr)
            sys.exit(2)
        endpoint = endpoint.replace("{open_id}", open_id)
    return endpoint


def _resolve_dates(args: argparse.Namespace) -> list[str]:
    if args.all:
        return sorted(p.stem for p in DAILY_DIR.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"))
    if args.since:
        all_days = sorted(p.stem for p in DAILY_DIR.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"))
        return [d for d in all_days if d >= args.since]
    return args.dates


def _upload_one(endpoint: str, day: str, dry_run: bool) -> dict | None:
    path = DAILY_DIR / f"{day}.json"
    if not path.exists():
        print(f"[upload] {day}: file not found ({path}), skip", file=sys.stderr)
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    payload = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")

    if dry_run:
        print(f"[upload] {day}: would POST {len(items)} items, {len(payload)} bytes to {endpoint}")
        return {"dry_run": True, "items": len(items)}

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[upload] {day}: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[upload] {day}: exception: {e}", file=sys.stderr)
        return None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {"raw": body}

    print(f"[upload] {day}: {parsed}")
    return parsed


def main() -> int:
    p = argparse.ArgumentParser(description="Upload daily JSON to your HTTP API")
    p.add_argument("dates", nargs="*", help="YYYY-MM-DD (may be repeated)")
    p.add_argument("--all", action="store_true", help="upload every daily JSON found")
    p.add_argument("--since", help="upload everything on or after this date")
    p.add_argument("--dry-run", action="store_true", help="print what would be sent, don't POST")
    args = p.parse_args()

    if not (args.all or args.since or args.dates):
        p.error("provide at least one date, or --all, or --since YYYY-MM-DD")

    cfg = _load_config()
    endpoint = _resolve_endpoint(cfg)
    dates = _resolve_dates(args)
    if not dates:
        print("[upload] no dates to upload", file=sys.stderr)
        return 0

    failures = 0
    for day in dates:
        result = _upload_one(endpoint, day, args.dry_run)
        if result is None:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
