#!/usr/bin/env bash
# Regenerate scripts/deploy/constraints.txt — the pinned dependency set the
# image is built from.
#
# Run this after any change to trading-bot/requirements.txt, and never as part
# of a deploy. Pinning exists so that the version set is a reviewed decision;
# refreshing it silently inside `keel-upgrade.sh` would defeat the point.
#
# Resolution happens in the same base image the build uses, so the answer is
# for the platform that will actually run it, not for the laptop you typed this
# on.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/constraints.txt"

echo "==> resolving in python:3.12-slim"
RESOLVED="$(docker run --rm \
  -v "$REPO_DIR/trading-bot/requirements.txt:/tmp/req.txt:ro" \
  python:3.12-slim sh -c \
  'python -m venv /v >/dev/null 2>&1 && /v/bin/pip install -q -r /tmp/req.txt && /v/bin/pip freeze --exclude-editable')"

[ -n "$RESOLVED" ] || { echo "!! resolution produced nothing" >&2; exit 1; }

TMP="$(mktemp)"
cat > "$TMP" <<'HDR'
# Keel — pinned dependency set for the Linux container image.
#
# trading-bot/requirements.txt uses `>=`. Two nodes provisioned a week apart
# would then run different ccxt against the same exchange, which is a venue
# behaviour difference the promotion-gate sample cannot see and cannot control
# for (ARCHITECTURE-V3 §5 item 25). This file pins the fully resolved set
# WITHOUT editing requirements.txt.
#
# Resolved for: CPython 3.12, manylinux2014_x86_64, wheels only.
# Regenerate:   scripts/deploy/keel-freeze.sh
#
# Read the diff when you regenerate. A ccxt bump changes how orders reach a
# venue; a cryptography bump does not. They are not the same review.
HDR
printf '%s\n' "$RESOLVED" | tr -d '\r' | sort -f >> "$TMP"

if [ -f "$OUT" ] && command -v diff >/dev/null; then
  echo "==> changes:"
  diff -u "$OUT" "$TMP" | sed 's/^/    /' || true
fi

mv "$TMP" "$OUT"
echo "==> wrote $OUT"
echo "    Rebuild and re-run the rails before trusting it:"
echo "      docker compose build && docker compose run --rm engine python tests/test_risk_rails.py"
