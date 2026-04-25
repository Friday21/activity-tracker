#!/usr/bin/env python3
"""Calculate nightly sleep duration from iPhone Screen Time gaps.

For each day D, scans iPhone screen-time events in the window
[D-1 18:00 → D 14:00] and finds the longest contiguous gap.
That gap is treated as last night's sleep.

Handles "past midnight" naturally: if the person was awake until 1:30am,
those events appear in D's records and the longest gap is still detected
correctly (e.g. 01:30 → 07:45).

Output: a "睡眠" item injected into outputs/daily/D.json
  {
    "category": "睡眠",
    "title":    "睡眠",
    "start":    "<ISO8601 sleep-start time>",
    "duration_seconds": <seconds>,
    "detail":   "sleep",
    "source":   "calculated"
  }

If D has no iPhone screen time at all, duration_seconds is recorded as 0
(a placeholder; updated on the next run once data arrives).
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
SCREENTIME_DIR = ROOT / "outputs" / "screentime"
DAILY_DIR = ROOT / "outputs" / "daily"

# Minimum contiguous gap (seconds) that qualifies as a sleep period.
# 2 hours avoids confusing a "phone-down for a movie" pause with sleep.
MIN_SLEEP_GAP = 2 * 3600

# Search window: look for sleep starting from 18:00 the day before.
WINDOW_PREV_HOUR = 18
# Look for wake-up no later than 14:00 the same day.
WINDOW_TODAY_HOUR = 14

# Earliest hour that counts as "waking up"; activity before this is pre-sleep.
WAKE_EARLIEST_HOUR = 3

# Bundle IDs that indicate the alarm/clock being dismissed — not true waking.
CLOCK_BUNDLE_IDS = {
    "com.apple.ClockAngel",
    "com.apple.clock",
    "com.apple.mobiletimer",
}

# Events within this many seconds of a clock event are treated as alarm-adjacent.
CLOCK_CLUSTER_GAP = 60


def _load_json(p: Path, default):
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _iphone_intervals(
    day: str, tz: ZoneInfo
) -> list[tuple[dt.datetime, dt.datetime, str]]:
    """Return sorted (start, end, bundle_id) triples for iPhone screen-time events on `day`."""
    d = _load_json(SCREENTIME_DIR / f"{day}.json", {})
    raw = d.get("items", []) if isinstance(d, dict) else []
    intervals: list[tuple[dt.datetime, dt.datetime, str]] = []
    for v in raw:
        if not v.get("device", "").startswith("iPhone"):
            continue
        try:
            start = dt.datetime.fromisoformat(v["start"])
            end = dt.datetime.fromisoformat(v["end"])
        except (KeyError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        bundle_id = v.get("bundle_id", "")
        intervals.append((start, end, bundle_id))
    intervals.sort()
    return intervals


def _skip_clock_events(
    raw_events: list[tuple[dt.datetime, dt.datetime, str]],
    wake_time: dt.datetime,
) -> dt.datetime:
    """Advance wake_time past alarm/clock activity to the first genuinely-awake event.

    Skips both explicit clock bundle IDs and any event within CLOCK_CLUSTER_GAP
    seconds of a clock event (e.g. Wallet notifications that fire alongside an alarm).
    """
    clock_times = [(s, e) for s, e, bid in raw_events if bid in CLOCK_BUNDLE_IDS]

    def _is_alarm_adjacent(t: dt.datetime) -> bool:
        return any(
            abs((t - cs).total_seconds()) <= CLOCK_CLUSTER_GAP
            or abs((t - ce).total_seconds()) <= CLOCK_CLUSTER_GAP
            for cs, ce in clock_times
        )

    non_clock = [
        start for start, _end, bundle_id in raw_events
        if start >= wake_time
        and bundle_id not in CLOCK_BUNDLE_IDS
        and not _is_alarm_adjacent(start)
    ]
    if non_clock:
        return non_clock[0]
    return wake_time


def calc_sleep(day: str, tz: ZoneInfo) -> dict:
    """Return a daily-JSON sleep item for day `day` (YYYY-MM-DD).

    Sleep detection rules:
      - The wake-up event must be at or after WAKE_EARLIEST_HOUR (3am).
        Activity earlier than that (e.g. a 1am toilet/phone session) is
        treated as pre-sleep noise, not waking up.
      - If the user has *no* iPhone activity on day D at or after 3am, sleep
        is treated as undetermined and we record duration_seconds=0 — a later
        run will fill it in once the user actually starts using the phone.
    """
    today_date = dt.date.fromisoformat(day)
    prev_date = (today_date - dt.timedelta(days=1)).isoformat()

    today_events = _iphone_intervals(day, tz)
    prev_events = _iphone_intervals(prev_date, tz)

    # Anchor used for placeholder records (timezone-aware so the offset is correct
    # even on non-Shanghai setups).
    placeholder_start = dt.datetime(
        today_date.year, today_date.month, today_date.day, tzinfo=tz,
    )

    # No screen-time at all today → placeholder with 0 duration.
    if not today_events:
        return _make_item(placeholder_start, 0, day)

    # Activities before this time are treated as pre-sleep, not wake-up.
    cutoff = dt.datetime(
        today_date.year, today_date.month, today_date.day,
        WAKE_EARLIEST_HOUR, 0, 0, tzinfo=tz,
    )

    # Sleep can only be determined if there is at least one event today at or
    # after the cutoff. Without it, the user might still be asleep, or only had
    # short pre-3am awakenings — leave sleep undetermined and let the next run
    # pick it up.
    post_cutoff_events = [s for s, _e, _b in today_events if s >= cutoff]
    if not post_cutoff_events:
        return _make_item(placeholder_start, 0, day)

    # Build search window boundaries.
    window_start = placeholder_start - dt.timedelta(days=1) + dt.timedelta(hours=WINDOW_PREV_HOUR)
    window_end = placeholder_start + dt.timedelta(hours=WINDOW_TODAY_HOUR)

    # Collect events that overlap the search window.
    combined: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end, _bid in prev_events + today_events:
        if end >= window_start and start <= window_end:
            # Clamp to window for gap purposes.
            combined.append((max(start, window_start), min(end, window_end)))
    combined.sort()

    # Whether we have real events from the previous day inside the window.
    has_prev_context = any(
        end >= window_start and start <= window_end
        for start, end, _bid in prev_events
    )

    if not combined:
        if not prev_events:
            return _make_item(placeholder_start, 0, day)
        sleep_start = prev_events[-1][1]
        wake_time = _skip_clock_events(today_events, post_cutoff_events[0])
        if wake_time < cutoff:
            wake_time = post_cutoff_events[0]
        duration = max(0, int((wake_time - sleep_start).total_seconds()))
        return _make_item(sleep_start, duration, day)

    # Build candidate gaps. Consider gap from window_start → first event only
    # when we actually have previous-day data; otherwise that gap is an artifact
    # of missing data.
    candidates: list[tuple[dt.datetime, dt.datetime, int]] = []
    prev_end = window_start
    for start, end in combined:
        if prev_end == window_start and not has_prev_context:
            prev_end = max(prev_end, end)
            continue
        gap = int((start - prev_end).total_seconds())
        if gap > 0:
            candidates.append((prev_end, start, gap))
        prev_end = max(prev_end, end)

    # Only consider gaps whose wake-time is at or after the 3am cutoff. A gap
    # that "ends" at 1am is not a sleep cycle — it's the user going to bed at
    # 9pm and briefly checking their phone at 1am. Picking the longest such gap
    # would mis-report sleep as 9pm→1am.
    valid_candidates = [c for c in candidates if c[1] >= cutoff]

    best_sleep_start: dt.datetime | None = None
    best_wake_time: dt.datetime | None = None
    best_gap_secs = 0

    if valid_candidates:
        best = max(valid_candidates, key=lambda x: x[2])
        best_sleep_start, best_wake_time, _ = best
        best_wake_time = _skip_clock_events(today_events, best_wake_time)
        if best_wake_time < cutoff:
            best_wake_time = post_cutoff_events[0]
        best_gap_secs = max(0, int((best_wake_time - best_sleep_start).total_seconds()))

    if best_gap_secs < MIN_SLEEP_GAP:
        # No qualifying gap ≥ 2h. Try a coarse fallback: last event yesterday
        # → first post-cutoff event today.
        if not has_prev_context and not prev_events:
            return _make_item(placeholder_start, 0, day)

        fb_sleep = prev_events[-1][1] if prev_events else combined[0][1]
        fb_wake = _skip_clock_events(today_events, post_cutoff_events[0])
        if fb_wake < cutoff:
            fb_wake = post_cutoff_events[0]
        fb_gap = max(0, int((fb_wake - fb_sleep).total_seconds()))

        if best_sleep_start is None or fb_gap > best_gap_secs:
            best_sleep_start = fb_sleep
            best_wake_time = fb_wake
            best_gap_secs = fb_gap

    if best_sleep_start is None or best_wake_time is None:
        return _make_item(placeholder_start, 0, day)

    duration = max(0, int((best_wake_time - best_sleep_start).total_seconds()))
    return _make_item(best_sleep_start, duration, day)


def _make_item(sleep_start: dt.datetime, duration_seconds: int, day: str) -> dict:
    return {
        "category": "睡眠",
        "title": "睡眠",
        "date": day,
        "start": sleep_start.isoformat(timespec="seconds"),
        "duration_seconds": duration_seconds,
        "detail": "sleep",
        "source": "calculated",
    }


def _inject_sleep(day: str, sleep_item: dict) -> None:
    """Replace any existing sleep item in outputs/daily/D.json with the new one."""
    target = DAILY_DIR / f"{day}.json"
    existing = _load_json(target, {})
    items: list[dict] = existing.get("items", []) if isinstance(existing, dict) else []

    # Remove previous sleep entry (keyed by detail="sleep").
    items = [it for it in items if it.get("detail") != "sleep"]

    items.append(sleep_item)
    items.sort(key=lambda x: x.get("start", ""))

    payload = {
        "date": day,
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": items,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate nightly sleep from Screen Time")
    parser.add_argument(
        "--days", type=int, default=2,
        help="Number of recent days to process (default: 2 = today + yesterday)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Process all days with screentime data",
    )
    args = parser.parse_args()

    cfg = _load_json(CONFIG, {})
    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    today = dt.datetime.now(tz).date()

    if args.all and SCREENTIME_DIR.exists():
        days = sorted(
            p.stem for p in SCREENTIME_DIR.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")
        )
    else:
        days = [
            (today - dt.timedelta(days=i)).isoformat()
            for i in range(args.days)
        ]

    for day in days:
        item = calc_sleep(day, tz)
        _inject_sleep(day, item)

        dur = item["duration_seconds"]
        if dur == 0:
            print(f"[sleep] {day}: 0 (no screen time or undetermined)")
        else:
            start_dt = dt.datetime.fromisoformat(item["start"])
            wake_dt = start_dt + dt.timedelta(seconds=dur)
            print(
                f"[sleep] {day}: {_fmt_duration(dur)}"
                f"  ({start_dt.strftime('%H:%M')} → {wake_dt.strftime('%H:%M')})"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
