#!/bin/bash
# ============================================================
# Keel watchdog — keeps server.py and news_agent.py running.
#
#   ./watchdog-install.sh          install + start (auto-restart on crash,
#                                  auto-start at login, Mac kept awake)
#   ./watchdog-install.sh status   show whether both jobs are running
#   ./watchdog-install.sh remove   stop + uninstall both jobs
#
# Uses macOS launchd (the native service manager) — no extra software.
# Logs: state/server.launchd.log and state/news_agent.launchd.log
# ============================================================
set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3)"
LA="$HOME/Library/LaunchAgents"
P_SERVER="$LA/com.slc.server.plist"
P_NEWS="$LA/com.slc.newsagent.plist"
P_SANITY="$LA/com.slc.sanity.plist"
P_TVCTX="$LA/com.slc.tvcontext.plist"

status() {
  for label in com.slc.server com.slc.newsagent com.slc.sanity com.slc.tvcontext; do
    if launchctl list | grep -q "$label"; then
      pid=$(launchctl list | grep "$label" | awk '{print $1}')
      echo "  $label : running (pid $pid)"
    else
      echo "  $label : NOT loaded"
    fi
  done
  echo
  echo "  server reachable: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8766/api/pairs 2>/dev/null || echo no)"
}

remove() {
  launchctl unload "$P_SERVER" 2>/dev/null || true
  launchctl unload "$P_NEWS" 2>/dev/null || true
  launchctl unload "$P_SANITY" 2>/dev/null || true
  launchctl unload "$P_TVCTX" 2>/dev/null || true
  rm -f "$P_SERVER" "$P_NEWS" "$P_SANITY" "$P_TVCTX"
  echo "Watchdog removed. (Processes stopped; start manually with python3 server.py)"
}

case "${1:-install}" in
  status) status; exit 0 ;;
  remove) remove; exit 0 ;;
esac

mkdir -p "$LA" "$BOT_DIR/state"

# --- server.py (wrapped in caffeinate so the Mac doesn't sleep) -------------
cat > "$P_SERVER" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.slc.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$PYTHON</string>
    <string>$BOT_DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$BOT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$BOT_DIR/state/server.launchd.log</string>
  <key>StandardErrorPath</key><string>$BOT_DIR/state/server.launchd.log</string>
</dict></plist>
EOF

# --- news_agent.py -----------------------------------------------------------
cat > "$P_NEWS" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.slc.newsagent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$BOT_DIR/news_agent.py</string>
  </array>
  <key>WorkingDirectory</key><string>$BOT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$BOT_DIR/state/news_agent.launchd.log</string>
  <key>StandardErrorPath</key><string>$BOT_DIR/state/news_agent.launchd.log</string>
</dict></plist>
EOF

# --- daily sanity check (07:00 local) ---------------------------------------
cat > "$P_SANITY" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.slc.sanity</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$BOT_DIR/sanity_check.py</string>
    <string>--modes</string>
    <string>swing</string>
    <string>--notify</string>
    <string>--apply</string>
  </array>
  <key>WorkingDirectory</key><string>$BOT_DIR</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$BOT_DIR/state/sanity.launchd.log</string>
  <key>StandardErrorPath</key><string>$BOT_DIR/state/sanity.launchd.log</string>
</dict></plist>
EOF

# --- TradingView context refresh (every hour) --------------------------------
cat > "$P_TVCTX" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.slc.tvcontext</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$BOT_DIR/tv_context.py</string>
    <string>--force</string>
  </array>
  <key>WorkingDirectory</key><string>$BOT_DIR</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$BOT_DIR/state/tvcontext.launchd.log</string>
  <key>StandardErrorPath</key><string>$BOT_DIR/state/tvcontext.launchd.log</string>
</dict></plist>
EOF

# stop any manually-started copies so the port is free
EXISTING=$(lsof -ti :8766 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  echo "Stopping manually-started server (pid $EXISTING) so launchd can take over..."
  kill $EXISTING 2>/dev/null || true
  sleep 2
fi
pkill -f "news_agent.py" 2>/dev/null || true

# (re)load both jobs
launchctl unload "$P_SERVER" 2>/dev/null || true
launchctl unload "$P_NEWS" 2>/dev/null || true
launchctl unload "$P_SANITY" 2>/dev/null || true
launchctl unload "$P_TVCTX" 2>/dev/null || true
launchctl load -w "$P_SERVER"
launchctl load -w "$P_NEWS"
launchctl load -w "$P_SANITY"
launchctl load -w "$P_TVCTX"

sleep 3
echo "Installed. Current status:"
status
echo
echo "All services will restart on crash and start at login."
echo "Commands:  ./watchdog-install.sh status | remove"
