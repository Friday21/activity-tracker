#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

PROJECT_DIR="$(pwd)"
PLIST_NAME="com.user.activity-tracker.plist"
PLIST_SRC="launchd/${PLIST_NAME}.template"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
LABEL="com.user.activity-tracker"

usage() {
  cat <<EOF
Usage: $0 <install|uninstall|status|run|logs>

  install    — generate the launchd plist (substituting the project path) and load it
  uninstall  — unload and remove the launchd plist
  status     — print whether the agent is loaded and when it will next fire
  run        — fire the job now (kickstart)
  logs       — tail today's log file

Default schedule: 03:00 daily + every 2 hours between 09:00 and 23:00.
Edit ${PLIST_SRC} to customize.
EOF
}

cmd="${1:-}"
case "$cmd" in
  install)
    if [[ ! -f "$PLIST_SRC" ]]; then
      echo "ERROR: template not found at $PLIST_SRC" >&2
      exit 1
    fi
    mkdir -p "${HOME}/Library/LaunchAgents"
    sed "s|__PROJECT_DIR__|${PROJECT_DIR}|g" "$PLIST_SRC" > "$PLIST_DST"
    # Unload first in case it's already running
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"
    echo "✓ Installed: $PLIST_DST"
    echo "  Next fire times:"
    launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -E 'next fire|state' || \
      echo "  (launchctl print not available; check with: launchctl list | grep ${LABEL})"
    ;;
  uninstall)
    if [[ -f "$PLIST_DST" ]]; then
      launchctl unload "$PLIST_DST" 2>/dev/null || true
      rm -f "$PLIST_DST"
      echo "✓ Removed: $PLIST_DST"
    else
      echo "(nothing to remove; no plist at $PLIST_DST)"
    fi
    ;;
  status)
    if [[ -f "$PLIST_DST" ]]; then
      echo "Plist: $PLIST_DST (installed)"
      launchctl list | grep "${LABEL}" || echo "(agent not loaded)"
      launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -E 'next fire|state|last exit' || true
    else
      echo "Plist not installed. Run: $0 install"
    fi
    ;;
  run)
    launchctl kickstart -k "gui/$(id -u)/${LABEL}"
    echo "✓ Kickstarted ${LABEL}"
    ;;
  logs)
    log_file="outputs/logs/$(date +%F).log"
    if [[ -f "$log_file" ]]; then
      tail -f "$log_file"
    else
      echo "No log file for today at $log_file"
    fi
    ;;
  *)
    usage
    exit 1
    ;;
esac
