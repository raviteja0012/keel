#!/usr/bin/env bash
# Deliberately re-open the entry gate after a stop, an upgrade or a restore.
#
# Every container stop writes halt_new_entries=True (keel-supervise.py), and it
# survives the restart. This script is the only thing in scripts/deploy/ that
# moves the system toward MORE trading, and it is interactive on purpose:
# resuming is a decision, and it should be made by someone who has just looked
# at the open book and the kill-switch state.
#
# It does NOT touch trading_mode. Paper -> live is live_switch.py's two-step
# confirm behind the promotion gate, and nothing in a deploy script may
# shortcut it (CLAUDE.md invariant 2).
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

DB=/app/trading-bot/data/trading.db
q() { docker compose exec -T engine sqlite3 "$DB" "$1" | tr -d '\r'; }

docker compose ps --status running --services | grep -qx engine || {
  echo "!! engine is not running. Start it first: docker compose up -d" >&2; exit 1; }

echo "==> state before resuming"
echo "    trading_mode     : $(q "SELECT COALESCE((SELECT value FROM settings WHERE key='trading_mode'),'unset');")"
echo "    halt_new_entries : $(q "SELECT COALESCE((SELECT value FROM settings WHERE key='halt_new_entries'),'unset');")"
echo "    open trades      : $(q "SELECT count(*) FROM trades WHERE status='open';")"
echo "    closed trades    : $(q "SELECT count(*) FROM trades WHERE status='closed';")"
echo "    db integrity     : $(q 'PRAGMA quick_check;')"
echo
echo "    Open positions:"
q "SELECT '      #'||id||'  '||mode||'  '||symbol||' '||side||'  entry '||entry||'  sl '||sl FROM trades WHERE status='open';"

echo
read -r -p "Re-open the entry gate (halt_new_entries=False)? type RESUME: " C
[ "$C" = "RESUME" ] || { echo "aborted — entries stay halted."; exit 1; }

# Through params_store, with an origin and a reason, so the resume lands in
# param_changes next to the halt that preceded it. Writing settings directly
# would leave a gate that opened with no record of who opened it.
docker compose exec -T engine python - <<'PY'
import params_store, storage
storage.init()
params_store.set_param("halt_new_entries", False, origin="human",
                       reason="operator resume after deploy stop (keel-resume.sh)")
print("halt_new_entries =", storage.get_setting("halt_new_entries"))
PY

echo "==> entries re-opened. Watch one cycle before walking away:"
echo "     docker compose logs -f --tail=50 engine"
