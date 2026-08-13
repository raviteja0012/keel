#!/usr/bin/env bash
# Restore a Keel database snapshot over the live volume.
#
# This is destructive and it stops trading. It refuses to run while the stack is
# up, because writing under a live SQLite WAL reader is how you turn one bad
# database into two.
#
# The restored database keeps whatever `halt_new_entries` and `trading_mode`
# were true at snapshot time. Read the summary this prints before you resume.
#
# Usage:  keel-restore.sh backups/keel-db-20260809T120000Z.db.gz
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${1:-}"
cd "$REPO_DIR"

if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "usage: keel-restore.sh <keel-db-*.db.gz>" >&2
  ls -1t backups/keel-db-*.db.gz 2>/dev/null | head -10 | sed 's/^/  available: /' >&2
  exit 1
fi

# Ask compose for the real volume name rather than assuming the
# "<project>_<key>" convention: a COMPOSE_PROJECT_NAME in the environment, or a
# `name:` under the volume, changes it, and restoring into a volume nothing
# mounts looks like a successful restore that lost every trade.
resolve_volume() {
  docker compose config --format json 2>/dev/null \
    | sed -n 's/.*"keel_data"[^}]*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    | head -1
}
VOLUME="$(resolve_volume || true)"
if [ -z "$VOLUME" ]; then
  VOLUME="$(docker volume ls --format '{{.Name}}' | grep -E '_keel_data$|^keel_data$' | head -1)"
fi
[ -n "$VOLUME" ] || { echo "!! cannot determine the data volume name." >&2
                      docker volume ls >&2; exit 1; }
docker volume inspect "$VOLUME" >/dev/null 2>&1 || {
  echo "!! volume '$VOLUME' does not exist." >&2; docker volume ls >&2; exit 1; }

echo "==> source : $SRC"
echo "==> volume : $VOLUME"

RUNNING="$(docker compose ps --status running --services 2>/dev/null | tr '\n' ' ')"
if [ -n "${RUNNING// /}" ]; then
  echo "!! the stack is running ($RUNNING)." >&2
  echo "   Stop it first so nothing holds a WAL reader open:" >&2
  echo "     scripts/deploy/keel-stop.sh" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
gunzip -c "$SRC" > "$WORK/trading.db"

# Verify the archive BEFORE the live file is touched. A restore that installs a
# corrupt database has destroyed the only remaining copy of the evidence.
echo "==> verifying archive"
if command -v sqlite3 >/dev/null 2>&1; then
  CHECK="$(sqlite3 "$WORK/trading.db" 'PRAGMA integrity_check;')"
else
  CHECK="$(docker run --rm -v "$WORK:/w" keel:${KEEL_VERSION:-local} \
           sqlite3 /w/trading.db 'PRAGMA integrity_check;')"
fi
[ "$CHECK" = "ok" ] || { echo "!! archive fails integrity_check: $CHECK" >&2; exit 2; }

SUMMARY="$(docker run --rm -v "$WORK:/w" keel:${KEEL_VERSION:-local} sqlite3 /w/trading.db \
  "SELECT 'closed trades: '||count(*) FROM trades WHERE status='closed';
   SELECT 'open trades  : '||count(*) FROM trades WHERE status='open';
   SELECT 'trading_mode : '||COALESCE((SELECT value FROM settings WHERE key='trading_mode'),'unset');
   SELECT 'halt_entries : '||COALESCE((SELECT value FROM settings WHERE key='halt_new_entries'),'unset');")"
echo "$SUMMARY" | sed 's/^/    /'

read -r -p "Overwrite the live database with this snapshot? type YES: " CONFIRM
[ "$CONFIRM" = "YES" ] || { echo "aborted."; exit 1; }

# Keep the file we are about to replace. The most common restore mistake is
# restoring the wrong snapshot, and the recovery from that is this copy.
echo "==> preserving the current database as trading.db.pre-restore"
docker run --rm -v "${VOLUME}:/d" alpine sh -c \
  'if [ -f /d/trading.db ]; then cp -a /d/trading.db /d/trading.db.pre-restore; fi'

echo "==> installing snapshot"
# Remove the stale -wal/-shm alongside it: they belong to the OLD database and
# SQLite would try to replay them over the new one.
docker run --rm -v "${VOLUME}:/d" -v "$WORK:/w" alpine sh -c \
  'rm -f /d/trading.db-wal /d/trading.db-shm && cp /w/trading.db /d/trading.db && chown 10001:10001 /d/trading.db && chmod 600 /d/trading.db'

echo "==> restored. Start with: docker compose up -d"
echo "    Then confirm the gate count you expect:"
echo "      docker compose exec engine sqlite3 /app/trading-bot/data/trading.db \\"
echo "        \"SELECT count(*) FROM trades WHERE status='closed';\""
echo "    Entries stay halted until you deliberately resume: keel-resume.sh"
