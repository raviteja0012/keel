#!/bin/zsh
# Install the multi-asset stack as macOS launchd services (background,
# auto-restart, start at login).
#
#   ./install-multiasset-services.sh            install + start
#   ./install-multiasset-services.sh status     show state
#   ./install-multiasset-services.sh uninstall  stop + remove
#
# Deliberately uses the com.slcmulti.* prefix so it can NEVER collide with the
# older services (com.slc.* on port 8766, com.tradingbot.* on 8765). Port and
# paths are detected at install time, so a different clone location just works.
#
# Logs: trading-bot/state/launchd_<service>.log  (gitignored)
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
BOT="$HERE/trading-bot"
PY="${SLC_PYTHON:-$(command -v python3)}"
PORT="${SLC_PORT:-8768}"
DASH_PORT="${SLC_DASH_PORT:-8767}"
AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
PREFIX="com.slcmulti"
SERVICES=(server newsagent dashboard)

if [ ! -f "$BOT/server.py" ]; then
  echo "error: $BOT/server.py not found — run this from the repo root" >&2
  exit 1
fi
if [ -z "$PY" ]; then
  echo "error: python3 not found on PATH (set SLC_PYTHON=/full/path/to/python3)" >&2
  exit 1
fi

write_plist() {
  local name="$1" script="$2" envkey="$3" envval="$4"
  local label="$PREFIX.$name"
  local plist="$AGENTS/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$BOT/$script</string>
  </array>
  <key>WorkingDirectory</key><string>$BOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>$envkey</key><string>$envval</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>20</integer>
  <key>StandardOutPath</key><string>$BOT/state/launchd_$name.log</string>
  <key>StandardErrorPath</key><string>$BOT/state/launchd_$name.log</string>
</dict>
</plist>
PLIST
  echo "wrote $plist"
}

case "${1:-install}" in
install)
  mkdir -p "$AGENTS" "$BOT/state"
  write_plist server    server.py       SLC_PORT       "$PORT"
  write_plist newsagent news_agent.py   SLC_SERVER_URL "http://127.0.0.1:$PORT"
  write_plist dashboard dashboard_api.py SLC_PORT      "$PORT"
  for s in "${SERVICES[@]}"; do
    label="$PREFIX.$s"
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$label.plist" 2>/dev/null \
      || launchctl load -w "$AGENTS/$label.plist"
    launchctl enable "gui/$UID_NUM/$label" 2>/dev/null || true
    echo "started $label"
  done
  echo
  echo "engine    : http://127.0.0.1:$PORT   (EA target — point the EA here)"
  echo "dashboard : http://127.0.0.1:$DASH_PORT"
  echo "logs      : $BOT/state/launchd_*.log"
  echo
  echo "The older bots are untouched (com.slc.* on 8766, com.tradingbot.* on 8765)."
  ;;
status)
  echo "PID  STATUS  LABEL"
  launchctl list | grep "$PREFIX" || echo "(no $PREFIX services loaded)"
  echo
  for p in "$PORT" "$DASH_PORT"; do
    printf 'port %s: ' "$p"
    lsof -ti ":$p" >/dev/null 2>&1 && echo "in use" || echo "free"
  done
  ;;
uninstall)
  for s in "${SERVICES[@]}"; do
    label="$PREFIX.$s"
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "removed $label"
  done
  ;;
*)
  echo "usage: $0 [install|status|uninstall]" >&2
  exit 2
  ;;
esac
