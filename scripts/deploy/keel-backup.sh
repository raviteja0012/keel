#!/usr/bin/env bash
# Back up the Keel SQLite volume — online, verified, and worth restoring.
#
# WHY THIS IS NOT `cp trading.db backup.db`
# -----------------------------------------
# trading.db is WAL mode. At any instant the committed truth is spread across
# trading.db, trading.db-wal and trading.db-shm. Copying the main file alone
# while the engine is running gives you a file that opens without complaint and
# is missing every transaction since the last checkpoint — which is exactly the
# recent trades you cared about. `sqlite3 .backup` uses the online backup API:
# it takes a consistent snapshot of a live database without stopping the engine.
#
# WHY IT MATTERS MORE HERE THAN USUAL
# -----------------------------------
# The promotion gate is 50 closed trades with positive expectancy, counted per
# strategy x asset-class cell out of the `trades` table (analysis.py
# GATE_MIN_TRADES). That table IS the evidence. Lose it and you have not lost a
# database, you have restarted a clock that takes months to run, and there is no
# way to reconstruct it — the cost model, the git SHA and the fill assumptions
# behind each closed trade only exist in the row.
#
# Usage:
#   keel-backup.sh                    # -> ./backups/
#   keel-backup.sh /mnt/backups       # -> given directory
#   KEEP=30 keel-backup.sh            # retention in days (default 14)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${1:-$REPO_DIR/backups}"
KEEP="${KEEP:-14}"
SERVICE="${KEEL_BACKUP_SERVICE:-engine}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DB_IN_CTR=/app/trading-bot/data/trading.db
TMP_IN_CTR="/tmp/keel-backup-$STAMP.db"

cd "$REPO_DIR"
mkdir -p "$DEST"
chmod 700 "$DEST"

compose() { docker compose "$@"; }

if ! compose ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
  echo "!! service '$SERVICE' is not running." >&2
  echo "   A stopped stack can be backed up cold instead:" >&2
  echo "     docker run --rm -v keel_keel_data:/d -v \"\$PWD\":/out alpine \\" >&2
  echo "       tar czf /out/keel-data-cold-$STAMP.tar.gz -C /d ." >&2
  exit 1
fi

echo "==> online snapshot of $DB_IN_CTR"
# .backup is safe against a live writer. It retries internally on contention.
compose exec -T "$SERVICE" sqlite3 "$DB_IN_CTR" ".backup '$TMP_IN_CTR'"

echo "==> verifying the snapshot before it is allowed to become a backup"
# An unverified backup is a guess. Check the copy, not the original: a snapshot
# that cannot pass integrity_check must never overwrite a good one in rotation.
INTEGRITY="$(compose exec -T "$SERVICE" sqlite3 "$TMP_IN_CTR" 'PRAGMA integrity_check;' | tr -d '\r')"
if [ "$INTEGRITY" != "ok" ]; then
  echo "!! integrity_check on the snapshot returned: $INTEGRITY" >&2
  echo "!! NOT writing this backup. Investigate with hallucination_check.py." >&2
  compose exec -T "$SERVICE" rm -f "$TMP_IN_CTR" || true
  exit 2
fi

# Evidence count, printed so the operator can see the gate clock advancing and
# would notice a backup that suddenly holds fewer trades than the last one.
TRADES="$(compose exec -T "$SERVICE" sqlite3 "$TMP_IN_CTR" \
          "SELECT count(*) FROM trades WHERE status='closed';" | tr -d '\r')"
echo "    integrity ok; closed trades in snapshot: $TRADES"

OUT="$DEST/keel-db-$STAMP.db.gz"
compose exec -T "$SERVICE" cat "$TMP_IN_CTR" | gzip -9 > "$OUT"
compose exec -T "$SERVICE" rm -f "$TMP_IN_CTR"
chmod 600 "$OUT"

# state/ carries the decision traces and the news audit trail — the "why" behind
# the rows in trades. It ALSO carries state/dashboard_token, which is a live
# credential for the control plane, so this archive is 0600 and must never be
# copied off the host unencrypted. Encrypt before it goes anywhere: see docs §7.
OUT_STATE="$DEST/keel-state-$STAMP.tar.gz"
compose exec -T "$SERVICE" tar czf - -C /app/trading-bot/state . > "$OUT_STATE"
chmod 600 "$OUT_STATE"

echo "==> wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "    wrote $OUT_STATE ($(du -h "$OUT_STATE" | cut -f1))  [contains dashboard_token]"

# Retention last: never delete an old backup until a new one has been written
# and verified.
find "$DEST" -maxdepth 1 -name 'keel-db-*.db.gz' -mtime "+$KEEP" -print -delete
find "$DEST" -maxdepth 1 -name 'keel-state-*.tar.gz' -mtime "+$KEEP" -print -delete

echo "==> done. Restore with: scripts/deploy/keel-restore.sh $OUT"
echo "    A backup you have never restored is a hypothesis. Test one."
