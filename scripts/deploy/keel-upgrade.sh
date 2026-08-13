#!/usr/bin/env bash
# Upgrade the running node without losing the volume.
#
# Three refusals, in order of how expensive the mistake is:
#
#   1. Refuses while any position is open. storage._MIGRATIONS is additive with
#      no down path, and a schema change landing under an open position is a
#      change to the rules mid-trade. ARCHITECTURE-V3 §6 makes the flat-book
#      gate a condition of updating at all.
#   2. Refuses without a verified backup taken in this run. `docker compose
#      down -v`, a bad migration and a mistyped volume name all end the same
#      way, and the 50-trade gate sample is the thing being risked.
#   3. Refuses to auto-resume. The new build starts with entries halted and a
#      human decides when it trades.
#
# The volume is never named on a destructive command line here. `docker compose
# up -d --build` replaces containers and leaves named volumes alone; the only
# thing that deletes them is `down -v`, which this script does not contain and
# should not be added to.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

DB=/app/trading-bot/data/trading.db
q() { docker compose exec -T engine sqlite3 "$DB" "$1" | tr -d '\r'; }

echo "==> current build"
docker compose images 2>/dev/null | sed 's/^/    /' || true
echo "    git: $(git rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"

# ---- gate 1: flat book -----------------------------------------------------
if docker compose ps --status running --services | grep -qx engine; then
  OPEN="$(q "SELECT count(*) FROM trades WHERE status='open';")"
  if [ "${OPEN:-0}" != "0" ]; then
    echo "!! $OPEN position(s) still open. Not upgrading." >&2
    q "SELECT '   #'||id||'  '||symbol||' '||side||'  sl '||sl FROM trades WHERE status='open';" >&2
    echo "   Wait for a flat book, or close deliberately from the dashboard." >&2
    exit 1
  fi
  echo "==> book is flat"
else
  echo "==> engine not running; skipping the flat-book check"
fi

# ---- gate 2: verified backup ----------------------------------------------
echo "==> backup before touching anything"
"$REPO_DIR/scripts/deploy/keel-backup.sh"

# ---- pull, build, replace --------------------------------------------------
if [ "${1:-}" = "--pull" ]; then
  echo "==> git pull"
  git pull --ff-only
fi

echo "==> building"
docker compose build

echo "==> replacing containers (volumes are untouched)"
docker compose up -d --remove-orphans

echo "==> waiting for health"
for _ in $(seq 1 40); do
  STATUS="$(docker inspect --format '{{.State.Health.Status}}' keel-engine 2>/dev/null || echo starting)"
  [ "$STATUS" = "healthy" ] && break
  sleep 5
done
echo "    engine health: ${STATUS:-unknown}"
[ "${STATUS:-}" = "healthy" ] || {
  echo "!! engine did not become healthy. docs/DEPLOYMENT-LINUX.md §10." >&2
  docker compose logs --tail=60 engine >&2
  exit 1
}

echo "==> post-upgrade checks"
echo "    schema/db integrity : $(q 'PRAGMA quick_check;')"
echo "    closed trades       : $(q "SELECT count(*) FROM trades WHERE status='closed';")   <- must not have dropped"
echo "    halt_new_entries    : $(q "SELECT COALESCE((SELECT value FROM settings WHERE key='halt_new_entries'),'unset');")"
echo
echo "==> upgraded. Entries are still halted."
echo "    Run the rails against the new image before you resume:"
echo "      docker compose exec engine python tests/test_risk_rails.py"
echo "    Then: scripts/deploy/keel-resume.sh"
