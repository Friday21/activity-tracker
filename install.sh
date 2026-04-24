#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

echo "=== activity-tracker-mac: install ==="
echo ""

# ── Python venv ──────────────────────────────────────────────────────────────
if [[ ! -d .venv ]]; then
  echo "Creating Python virtual environment (.venv)..."
  python3 -m venv .venv
else
  echo "✓ Virtual environment already exists"
fi

# ── pip dependencies ─────────────────────────────────────────────────────────
echo "Installing Python dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo "✓ Python dependencies installed"

# ── Playwright Chromium ──────────────────────────────────────────────────────
echo "Installing Playwright Chromium browser..."
.venv/bin/playwright install chromium
echo "✓ Playwright Chromium installed"

# ── Config ───────────────────────────────────────────────────────────────────
if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  echo "✓ Created config.json from config.example.json"
  echo "  → Edit config.json to set your upload endpoint and open_id (optional)"
else
  echo "✓ config.json already exists"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
cat <<'EOF'

✅ Install complete!

Next steps:
  1. Grant Full Disk Access to the program that will run the script
     (Terminal / iTerm / launchd / zsh). See README.md → Prerequisites.

  2. Log in to Google once (opens a visible Chromium window):
       .venv/bin/python3 scripts/setup_browser.py

  3. (Optional) Edit config.json to enable remote upload and notifications.

  4. Run a one-shot test:
       ./run_daily.sh

  5. Install the launchd schedule (runs daily at 03:00 + every 2h during the day):
       ./schedule.sh install
EOF
