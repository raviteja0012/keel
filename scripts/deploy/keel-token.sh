#!/usr/bin/env bash
# Print the dashboard control token.
#
# The token authorises every mutating dashboard endpoint, including the
# live-switch router. It is generated on first run by dash_auth.get_token() and
# persisted 0600 at state/dashboard_token on the state volume — never in the
# repo, never in config.yaml, never in the DB, and deliberately never in
# docker-compose.yml, because a committed compose file is source.
#
# It prints to your terminal. Do not paste it into a chat, a ticket or a shell
# that logs history to a synced folder.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

SVC=dashboard
docker compose ps --status running --services | grep -qx "$SVC" || SVC=engine
docker compose ps --status running --services | grep -qx "$SVC" || {
  echo "!! nothing running; start the stack first: docker compose up -d" >&2; exit 1; }

docker compose exec -T "$SVC" cat /app/trading-bot/state/dashboard_token 2>/dev/null || {
  echo "!! no token yet — the dashboard writes it on first start." >&2
  echo "   docker compose up -d dashboard && sleep 5 && $0" >&2
  exit 1
}
echo
echo "Use it as the X-Dashboard-Token header, or paste it into the dashboard UI."
echo "Rotate by deleting the file and restarting the dashboard:"
echo "  docker compose exec dashboard rm /app/trading-bot/state/dashboard_token"
echo "  docker compose restart dashboard"
