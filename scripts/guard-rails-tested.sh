#!/usr/bin/env bash
# A rail changed with no test touched is usually a miss. This warns rather than
# blocks: there are legitimate cases (a comment, a log line), and a guard that
# cries wolf gets bypassed until it is useless. Blocking lives in CI and review.
set -uo pipefail

staged_rails="${*:-}"
[ -z "$staged_rails" ] && exit 0

# Did anything under tests/ get staged in the same commit?
if git diff --cached --name-only | grep -q '^trading-bot/tests/'; then
  exit 0
fi

echo
echo "  NOTE: this commit changes a safety rail but touches no test:"
for f in $staged_rails; do echo "    $f"; done
echo
echo "  CLAUDE.md asks that a behaviour change name the invariant it was checked"
echo "  against, and CONTRIBUTING.md asks that a fix ship with the test that would"
echo "  have caught the bug. If this is a comment or a log line, carry on."
echo
exit 0
