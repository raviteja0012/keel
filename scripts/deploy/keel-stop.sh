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
  # Exit codes from keel-backup.sh, and why they are not all the same answer:
  #   0  DB snapshot and state archive both written and verified.
  #   3  DB snapshot written and VERIFIED; only the state archive failed.
  #   *  the DB snapshot itself did not happen.
  #
  # 3 must not block the stop. The promotion-gate evidence is the `trades`
  # table and it is already safe on disk; state/ is the decision traces beside
  # it. Refusing to stop over the lesser of the two leaves the operator with
  # --no-backup as their only route, which saves nothing at all — and a stop
  # script that is unreliable in a hurry is a stop that gets done by hand
  # without a drain.
  BK_RC=0
  "$REPO_DIR/scripts/deploy/keel-backup.sh" || BK_RC=$?
  if [ "$BK_RC" -eq 3 ]; then
    echo "!! state archive failed, DB snapshot is written and verified." >&2
    echo "   Continuing with the stop; the gate evidence is safe." >&2
  elif [ "$BK_RC" -ne 0 ]; then
    echo "!! backup failed before the DB snapshot was verified (exit $BK_RC)." >&2
    echo "   NOT stopping: a stop now would be a stop with no evidence saved." >&2
    echo "   Investigate, or accept the loss explicitly: keel-stop.sh --no-backup" >&2
    exit 1
  fi
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
