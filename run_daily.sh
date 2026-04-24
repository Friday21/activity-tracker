#!/bin/zsh
set -euo pipefail
setopt NULL_GLOB
cd "$(dirname "$0")"

# ── Virtual environment ─────────────────────────────────────────────────────
PYTHON="$(pwd)/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: venv not found. Run: ./install.sh" >&2
  exit 1
fi

# ── Logging ─────────────────────────────────────────────────────────────────
mkdir -p outputs/logs
LOG_FILE="outputs/logs/$(date +%F).log"

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg" | tee -a "$LOG_FILE"
}

# ── Error trap ───────────────────────────────────────────────────────────────
on_error() {
  local line=$1
  log "ERROR: Script failed at line $line"
  "$PYTHON" scripts/notify.py \
    --message "⚠️ activity-tracker 日报运行失败（line $line），日志: $LOG_FILE" \
    2>>"$LOG_FILE" || true
  exit 1
}
trap 'on_error $LINENO' ERR

log "=== activity-tracker daily run start ==="

# ── Quiet-hours gate ─────────────────────────────────────────────────────────
# Skip unscheduled runs during deep-sleep hours. 03:00 is an intentional
# scheduled run and must pass through. Block 04:00–08:59 only.
# Override with FORCE_RUN=1.
hour_now=$(date +%H)
if [[ "${FORCE_RUN:-0}" != "1" ]] && (( 10#$hour_now >= 4 && 10#$hour_now < 9 )); then
  log "Quiet hours (${hour_now}:00) — skipping run. Set FORCE_RUN=1 to override."
  exit 0
fi

# ── Step 1: Fetch Screen Time (past 7 days, Mac + iPhone via iCloud) ─────────
# Requires Full Disk Access. Non-fatal on failure so a single-source glitch
# doesn't block the rest of the pipeline.
log "Step 1: Fetching Screen Time from knowledgeC.db..."
if ! "$PYTHON" scripts/fetch_screentime.py --days 7 2>>"$LOG_FILE"; then
  log "Screen Time fetch failed (likely missing Full Disk Access); continuing."
fi

# ── Step 2: Fetch browser activity ──────────────────────────────────────────
log "Step 2: Fetching activity from Google My Activity..."
if ! "$PYTHON" scripts/fetch_activity.py --headless 2>>"$LOG_FILE"; then
  log "Activity fetch failed; will still analyze any existing capture files."
fi

activity_files=(inputs/activity/*.jsonl)
if (( ${#activity_files[@]} == 0 )); then
  log "No activity capture files found in inputs/activity/."
  "$PYTHON" scripts/notify.py \
    --message "⚠️ activity-tracker: 未找到抓取文件，今日无数据" \
    2>>"$LOG_FILE" || true
fi
log "Found ${#activity_files[@]} capture file(s)"

# ── Step 3: Analyze browser activity for today + yesterday ──────────────────
log "Step 3: Analyzing browser activity (today + yesterday)..."
for day in today yesterday; do
  "$PYTHON" src/analyze.py --day "$day" 2>>"$LOG_FILE" || log "analyze --day $day failed, continuing"
done

# ── Step 4: Build unified daily JSON (browser + screen time) ────────────────
log "Step 4: Building unified daily JSON..."
built_days=()
build_output=$("$PYTHON" scripts/build_daily_json.py 2>>"$LOG_FILE" | tee -a "$LOG_FILE") || \
  log "build_daily_json failed, continuing"

# Parse built days from output so we only upload what changed.
while IFS= read -r line; do
  if [[ "$line" =~ '\[build_daily\] ([0-9]{4}-[0-9]{2}-[0-9]{2}):' ]]; then
    built_days+=("${match[1]}")
  fi
done <<< "$build_output"

# ── Step 5: Calculate nightly sleep from iPhone idle gaps ───────────────────
log "Step 5: Calculating sleep duration..."
"$PYTHON" scripts/calc_sleep.py --all 2>>"$LOG_FILE" || log "calc_sleep failed, continuing"

# ── Step 6: Generate human-readable summaries ───────────────────────────────
log "Step 6: Generating summaries..."
for report_path in outputs/daily/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].txt; do
  day=$(basename "$report_path" .txt)
  summary_path="outputs/daily/${day}.summary.txt"
  if [[ ! -f "$summary_path" ]]; then
    "$PYTHON" scripts/summary_from_report.py "$report_path" > "$summary_path" 2>>"$LOG_FILE" || \
      log "summary for $day failed"
    log "Summary written: $summary_path"
  fi
done

# ── Step 7: Upload to remote API (if enabled in config) ─────────────────────
upload_enabled=$("$PYTHON" -c "
import json, sys
try:
    with open('config.json') as f: c = json.load(f)
    print('1' if c.get('upload', {}).get('enabled') else '0')
except Exception:
    print('0')
" 2>/dev/null)

if [[ "$upload_enabled" == "1" && ${#built_days[@]} -gt 0 ]]; then
  log "Step 7: Uploading ${#built_days[@]} day(s) to remote API..."
  for day in "${built_days[@]}"; do
    "$PYTHON" scripts/upload.py "$day" 2>>"$LOG_FILE" || log "upload $day failed, continuing"
  done
elif [[ "$upload_enabled" == "1" ]]; then
  log "Step 7: Upload enabled but no days were built this run."
else
  log "Step 7: Upload disabled in config (skip)."
fi

log "=== activity-tracker daily run complete ==="
