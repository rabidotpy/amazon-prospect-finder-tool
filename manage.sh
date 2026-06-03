#!/usr/bin/env bash
# Manage the always-on prospecting worker (auto.py) as a macOS launchd agent.
#
#   ./manage.sh start     install + start the background worker (survives reboot)
#   ./manage.sh stop      stop + uninstall the agent
#   ./manage.sh restart   stop then start
#   ./manage.sh status     is it running?
#   ./manage.sh log        tail the worker log
#   ./manage.sh run        run the worker in the FOREGROUND (Ctrl-C to quit)
#
# The worker watches ~/Downloads, ingests Helium10 exports, enriches brands one
# by one, and keeps data/green_prospects.csv up to date.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.amazonprospect.worker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/data/auto.log"
# Prefer the project venv (it has Playwright + the scrapers); fall back to system.
if [ -x "$DIR/.venv/bin/python" ]; then PY="$DIR/.venv/bin/python"; else PY="$(command -v python3)"; fi

mkdir -p "$DIR/data"

write_plist() {
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/auto.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF
}

case "${1:-}" in
  start)
    write_plist
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "started ($LABEL). log: $LOG"
    ;;
  stop)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "stopped + uninstalled."
    ;;
  restart)
    "$0" stop || true
    "$0" start
    ;;
  status)
    if launchctl list | grep -q "$LABEL"; then
      echo "RUNNING:"; launchctl list | grep "$LABEL"
    else
      echo "not running."
    fi
    ;;
  log)
    touch "$LOG"; tail -n 50 -f "$LOG"
    ;;
  run)
    exec "$PY" "$DIR/auto.py" "${@:2}"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|log|run}"; exit 1
    ;;
esac
