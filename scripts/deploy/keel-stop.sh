#!/usr/bin/env bash
# Stop the stack the way you want it stopped: evidence saved first.
#
# The drain itself is NOT here. keel-supervise.py does it inside the container
# on SIGTERM, so `docker compose down`, a host reboot, a systemd stop and an
# operator pressing the wrong button all get the same treatment. A drain that
# only happens when someone remembers to run the nice script is not a rail.
#
# What this adds on top is the backup, and a stop grace long enough for the
# supervisor to finish.
#
# Usage:  keel-stop.sh            # stop containers, keep volumes
#         keel-stop.sh --no-backup
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

if [ "${1:-}" != "--no-backup" ]; then
  echo "==> backing up before stopping"
  "$REPO_DIR/scripts/deploy/keel-backup.sh" || {
    echo "!! backup failed. Stop anyway with: keel-stop.sh --no-backup" >&2
    exit 1
  }
fi

echo "==> open positions at stop time:"
docker compose exec -T engine sqlite3 /app/trading-bot/data/trading.db \
  "SELECT id, mode, symbol, side, entry, sl FROM trades WHERE status='open';" \
  2>/dev/null | sed 's/^/    /' || echo "    (could not read; engine may already be down)"

echo "==> stopping (supervisor drains the entry gate, then SIGINTs the child)"
# `stop` not `down`: down removes containers and, with -v, volumes. Nothing here
# should ever be one typo away from deleting the gate evidence.
docker compose stop

echo "==> stopped. Volumes intact:"
docker volume ls --filter name=keel_ --format '    {{.Name}}'
echo
echo "    Entries are now halted (halt_new_entries=True, audited in param_changes)."
echo "    They STAY halted across the next start. That is deliberate: an engine"
echo "    that resumes trading by itself after an unexplained stop is failing open."
echo "    Resume deliberately with: scripts/deploy/keel-resume.sh"
