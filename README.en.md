# activity-tracker-mac

Collect everything you do online into one clean daily JSON, entirely from your Mac.

> 🌐 Language: [中文](README.md) · **English**

This project merges three data sources into a single per-day file:

- **Browser history** — scraped from Google My Activity (covers Chrome / Safari across all signed-in devices)
- **macOS app usage** — read from `knowledgeC.db`
- **iPhone app usage** — read from Biome `App.InFocus` SEGB streams (synced via iCloud)

plus automatic sleep detection from nightly phone-idle gaps, plus optional upload to your own HTTP API.

> **Platform:** macOS only. `knowledgeC.db` and Biome are Apple-private. Windows users — see [Windows adaptation](#windows-adaptation) below.

## What you get

Every day at 03:00 (and again every 2 hours during the day) this runs and writes:

```
outputs/daily/2026-04-23.json
```

With a schema like:

```json
{
  "date": "2026-04-23",
  "updated_at": "2026-04-24T09:08:12+08:00",
  "items": [
    {
      "category": "工作",
      "title": "github.com",
      "start": "2026-04-23T10:23:45+08:00",
      "duration_seconds": 697,
      "detail": "https://github.com/user/repo/pull/123",
      "source": "browser"
    },
    {
      "category": "娱乐",
      "title": "抖音",
      "start": "2026-04-23T21:05:10+08:00",
      "duration_seconds": 1820,
      "detail": "com.ss.iphone.ugc.Aweme",
      "source": "iphone"
    },
    {
      "category": "睡眠",
      "title": "睡眠",
      "start": "2026-04-23T00:11:00+08:00",
      "duration_seconds": 29280,
      "detail": "sleep",
      "source": "calculated"
    }
  ]
}
```

Categories (Chinese, configurable): `学习 / 工作 / 娱乐 / 社交 / 购物 / 新闻 / 工具 / 其他 / 睡眠`.

Sources: `browser` / `mac` / `iphone` / `calculated`.

For the full write-up / design notes, see [`docs/BLOG.md`](docs/BLOG.md).

---

## Quick start

```bash
git clone https://github.com/<you>/activity-tracker-mac.git
cd activity-tracker-mac

./install.sh                              # creates .venv, installs deps, installs Playwright
.venv/bin/python3 scripts/setup_browser.py  # log in to Google once (headed browser opens)

cp config.example.json config.json        # then edit — see below
./run_daily.sh                            # one-shot test run

./schedule.sh install                     # install launchd agent (auto-run)
```

Output lives in `outputs/daily/YYYY-MM-DD.json`.

---

## Prerequisites

1. **macOS 12+** (tested on 14 Sonoma and 15 Sequoia).
2. **Python 3.10+** (`python3 --version`).
3. **Full Disk Access** for the program that runs the script. Without this, reading `knowledgeC.db` fails silently.

   System Settings → Privacy & Security → **Full Disk Access** → add whichever of these is actually launching the script:
   - `Terminal.app` / `iTerm.app` (if you run it by hand)
   - `/bin/zsh` (if launchd runs it — add the exact binary)
   - `launchd` itself (on some macOS versions)

   Toggle the entry off and on after adding; macOS caches TCC decisions per binary hash.

4. **A Google account** with activity saved. If you disabled Web & App Activity in your Google account, this project has nothing to scrape — re-enable it at [myactivity.google.com/activitycontrols](https://myactivity.google.com/activitycontrols).

---

## Configuration

Copy `config.example.json` to `config.json` and edit:

```jsonc
{
  "timezone": "Asia/Shanghai",

  "upload": {
    "enabled": true,
    "endpoint": "https://your-api.example.com/api/time/upload/{open_id}/",
    "open_id": "PASTE_YOUR_OPEN_ID_HERE"
  },

  "notifications": {
    "telegram": {
      "bot_token": "",
      "chat_id": ""
    }
  },

  "top_domains_limit": 15,
  "top_urls_limit": 20,
  "default_gap_seconds": 30,
  "max_gap_seconds": 1800,
  "same_domain_continuation_bonus_seconds": 60,

  "categories": {
    "学习": { "domains": ["wikipedia.org", "..."], "keywords": ["docs", "..."] },
    "工作": { "domains": ["..."], "keywords": ["..."] }
  }
}
```

- **`upload.enabled: false`** if you just want local JSON and no remote upload.
- **`upload.open_id`** never commit this to git. `config.json` is in `.gitignore`.
- **`categories`** — 8 top-level buckets the project ships with. Rename / translate freely; downstream code doesn't care.
- **`domain_categories.json`** (separate file) — specific-domain overrides. E.g. `"github.com": "工作"`. Unknown domains are accumulated in `outputs/unclassified_domains.txt` for later review.

---

## What the pipeline does

`run_daily.sh` runs 6 steps in order. Each step is independent enough that the next one still runs if an earlier one fails.

| # | Script | Output |
|---|---|---|
| 1 | `scripts/fetch_screentime.py --days 7` | `outputs/screentime/YYYY-MM-DD.json` — merged Mac + iPhone events |
| 2 | `scripts/fetch_activity.py --headless` | `inputs/activity/capture-*.jsonl` — raw browser records |
| 3 | `src/analyze.py --day today` & `--day yesterday` | `outputs/data/YYYY-MM-DD.json` — deduped + classified visits with estimated durations |
| 4 | `scripts/build_daily_json.py` | `outputs/daily/YYYY-MM-DD.json` — unified daily file |
| 5 | `scripts/calc_sleep.py --all` | injects `睡眠` item into each daily JSON |
| 6 | `scripts/summary_from_report.py` | `outputs/daily/YYYY-MM-DD.summary.txt` — human-readable digest |
| 7 | `scripts/upload.py` (optional) | POSTs each built daily JSON to your API |

Quiet-hours gate: the script self-skips runs between 04:00–08:59 local time (unless `FORCE_RUN=1`), because that's when you're asleep and the browser scrape would just waste 2 minutes.

---

## Scheduling

One-liner:

```bash
./schedule.sh install      # install launchd agent
./schedule.sh uninstall    # remove it
./schedule.sh status       # show next fire time
./schedule.sh run          # fire it now
```

Under the hood this writes `~/Library/LaunchAgents/com.user.activity-tracker.plist` from [`launchd/com.user.activity-tracker.plist.template`](launchd/com.user.activity-tracker.plist.template) with the correct project path, then `launchctl load`s it.

Default schedule: **03:00 daily** + **every 2 hours between 09:00 and 23:00**.

Prefer cron? Add:

```cron
0 3,9,11,13,15,17,19,21,23 * * * cd /absolute/path/activity-tracker-mac && ./run_daily.sh
```

---

## Manual usage

Fetch last 7 days of screen time:

```bash
.venv/bin/python3 scripts/fetch_screentime.py --days 7
```

Fetch latest Google Activity (headless, uses saved login):

```bash
.venv/bin/python3 scripts/fetch_activity.py --headless
```

Analyze a specific day:

```bash
.venv/bin/python3 src/analyze.py --day 2026-04-23
.venv/bin/python3 src/analyze.py --day yesterday
```

Re-build a daily JSON from whatever sources exist:

```bash
.venv/bin/python3 scripts/build_daily_json.py
```

Upload one specific day:

```bash
.venv/bin/python3 scripts/upload.py 2026-04-23
```

Upload every day that has a daily JSON:

```bash
.venv/bin/python3 scripts/upload.py --all
```

---

## Upload protocol

If `upload.enabled` is true, the script POSTs each built daily JSON to:

```
POST {endpoint}
Content-Type: application/json

{ "items": [ ... ] }
```

where `{endpoint}` has `{open_id}` substituted from config.

Your server is expected to:

- accept `{ items: [...] }`
- deduplicate on `(start, detail)` (same key as local dedup)
- return any JSON; the script just logs it

Reference response from my setup:

```json
{
  "code": 0,
  "msg": "上传成功",
  "data": { "total": 619, "created": 128, "updated": 0, "skipped": 491, "errors": 0 }
}
```

---

## File layout

```
activity-tracker-mac/
├── README.md                           ← default (中文)
├── README.en.md                        ← English (you are here)
├── docs/BLOG.md                        ← full write-up / design notes
├── install.sh                          ← one-shot setup (venv + deps + playwright)
├── schedule.sh                         ← install/uninstall launchd agent
├── run_daily.sh                        ← the pipeline entrypoint
├── config.example.json
├── domain_categories.json              ← domain → category mappings (extensible)
├── requirements.txt
├── launchd/
│   └── com.user.activity-tracker.plist.template
├── scripts/
│   ├── fetch_screentime.py             ← knowledgeC.db + Biome reader
│   ├── fetch_activity.py               ← Google My Activity scraper
│   ├── setup_browser.py                ← first-time Google login
│   ├── build_daily_json.py             ← merge browser + screentime
│   ├── calc_sleep.py                   ← sleep from iPhone idle gaps
│   ├── summary_from_report.py          ← human-readable digest
│   ├── upload.py                       ← POST daily JSON to your API
│   └── notify.py                       ← Telegram notifier (optional)
├── src/
│   └── analyze.py                      ← browser-activity analyzer
├── inputs/activity/                    ← Playwright captures (generated)
├── outputs/
│   ├── screentime/YYYY-MM-DD.json
│   ├── data/YYYY-MM-DD.json            ← analyzed browser visits
│   ├── daily/YYYY-MM-DD.json           ← ★ final per-day output
│   ├── daily/YYYY-MM-DD.summary.txt
│   ├── logs/
│   └── unclassified_domains.txt
└── examples/
    └── daily.example.json
```

---

## Windows adaptation

The three data sources map to Windows as follows:

| Source | macOS | Windows alternative |
|---|---|---|
| Browser history | `scripts/fetch_activity.py` (Google My Activity, Playwright) | **Same script works.** Playwright is cross-platform. |
| Local app usage | `knowledgeC.db` + Biome | [ActivityWatch](https://activitywatch.net/) (open source, installer for Windows) — use `aw-watcher-window` + `aw-watcher-afk` buckets. Or read `%LOCALAPPDATA%\ConnectedDevicesPlatform\...\ActivitiesCache.db` directly (Timeline data). |
| iPhone app usage | Biome (via iCloud sync to Mac) | **Not directly available on Windows.** Use an iPhone Shortcut to export Screen Time summaries daily (hour-granularity only). |

As long as your Windows adapter produces the **same `items[].{category, title, start, duration_seconds, detail, source}` schema**, the rest of the pipeline — sleep calculation, upload, server-side dedup — is identical.

PRs welcome if you write a Windows adapter.

---

## Privacy

Everything runs locally. The only network calls are:

- Playwright fetching `myactivity.google.com` (your own account)
- Optional Telegram notification (if configured)
- Optional `POST` to your own API endpoint (if configured)

Nothing is sent to any third-party service. `.browser_profile/`, `config.json`, `inputs/`, `outputs/` are all in `.gitignore`.

---

## Troubleshooting

**`ERROR: cannot read knowledgeC.db`** — Full Disk Access missing. See [Prerequisites](#prerequisites).

**Playwright fetch returns 0 items** — your Google login cookie expired. Re-run `scripts/setup_browser.py`.

**iPhone data missing** — check iCloud: Settings → [your name] → iCloud → Screen Time must be ON, and the iPhone must have synced to this Mac at least once.

**Durations look off for iPhone** — `BIOME_MAX_GAP_SECONDS` (default 180) caps per-session duration; if an app legitimately runs in foreground for >3 min without any other event, it gets capped. Tune in `scripts/fetch_screentime.py` if needed.

**Full Disk Access granted but still 0 rows** — toggle the entry off-and-on in System Settings, and verify you granted it to the *exact* binary launchd is executing (often `/bin/zsh`, not Terminal).

---

## License

MIT. See [LICENSE](LICENSE).

---

## Credits

- Schema and column reverse-engineering borrows heavily from [mac_apt](https://github.com/ydkhatri/mac_apt) and [APOLLO](https://github.com/mac4n6/APOLLO).
- Playwright-based Google Activity scraping adapts well-known patterns from the open-source community.
