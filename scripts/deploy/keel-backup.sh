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
#
# WHY THIS ONE LINE NEEDS ALL THE CEREMONY BELOW
# ----------------------------------------------
# The engine rewrites state/open_spread.json every cycle and the news agent
# appends to state/news_agent.log continuously, so tar reads files that are
# changing underneath it and exits 1 ("file changed as we read it") as a matter
# of routine. Exit 1 from GNU tar is a WARNING — the archive is written, every
# other member is intact, and the changed member is captured as of the moment
# it was read. Exit 2 is the fatal one.
#
# Under `set -e` that routine warning aborted this script, keel-stop.sh saw a
# failed backup and REFUSED TO STOP THE STACK. The scripted safe stop was
# unreliable exactly when it was needed, and the operator's remaining option
# was --no-backup: no evidence saved at all. The DB snapshot above — the
# promotion-gate evidence, the part that actually matters — had already been
# taken and verified by then.
#
# So: distinguish 1 from 2, and do not take tar's word for either. The archive
# is only accepted if it can be listed back, which is what catches a truncated
# or half-written stream regardless of what exit code came with it.
OUT_STATE="$DEST/keel-state-$STAMP.tar.gz"
TAR_RC=0
compose exec -T "$SERVICE" tar czf - -C /app/trading-bot/state . > "$OUT_STATE" \
  || TAR_RC=$?

if [ "$TAR_RC" -ge 2 ]; then
  echo "!! state archive FAILED (tar exit $TAR_RC) — that is a fatal tar error," >&2
  echo "   not the routine 'file changed as we read it'. Discarding it." >&2
  rm -f "$OUT_STATE"
  echo "   The verified DB snapshot IS written: $OUT" >&2
  exit 3
fi

# Verify by reading it back. An archive that lists is an archive that restores;
# an unverified one is a guess, which is the same standard the DB snapshot above
# is held to.
#
# Fed on stdin, not as `-f "$OUT_STATE"`: GNU tar reads a filename containing a
# colon as host:path and tries to resolve a hostname. `keel-backup.sh /mnt/c:/b`
# would then fail verification on a perfectly good archive and delete it.
if ! tar tzf - < "$OUT_STATE" >/dev/null 2>&1; then
  echo "!! state archive is unreadable (tar exit was $TAR_RC). Discarding it." >&2
  rm -f "$OUT_STATE"
  echo "   The verified DB snapshot IS written: $OUT" >&2
  exit 3
fi

if [ "$TAR_RC" -eq 1 ]; then
  echo "    note: tar exit 1 — files changed while being read. Expected on a"
  echo "    running stack (open_spread.json every cycle, news_agent.log"
  echo "    continuously). Archive verified readable; contents are a snapshot"
  echo "    of each file as it was read, not a single instant."
fi
chmod 600 "$OUT_STATE"

echo "==> wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "    wrote $OUT_STATE ($(du -h "$OUT_STATE" | cut -f1))  [contains dashboard_token]"

# Retention last: never delete an old backup until a new one has been written
# and verified.
find "$DEST" -maxdepth 1 -name 'keel-db-*.db.gz' -mtime "+$KEEP" -print -delete
find "$DEST" -maxdepth 1 -name 'keel-state-*.tar.gz' -mtime "+$KEEP" -print -delete

echo "==> done. Restore with: scripts/deploy/keel-restore.sh $OUT"
echo "    A backup you have never restored is a hypothesis. Test one."
