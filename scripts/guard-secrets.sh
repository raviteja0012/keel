#!/usr/bin/env bash
# Refuse a commit that stages a credential. Same patterns as .github/workflows/guard.yml
# so local and CI agree; the only difference is this one runs before the mistake exists.
set -uo pipefail

files="${*:-}"
[ -z "$files" ] && exit 0

fail=0
scan() {
  local pattern="$1" label="$2"
  for f in $files; do
    [ -f "$f" ] || continue
    case "$f" in *.lock|*.png|*.jpg|*.pdf) continue;; esac
    if hits=$(grep -nE "$pattern" "$f" 2>/dev/null); then
      echo "REFUSED: possible $label in $f"
      echo "$hits" | head -3
      fail=1
    fi
  done
}

scan '[0-9]{8,12}:[A-Za-z0-9_-]{30,}'          'Telegram bot token'
scan 'discord(app)?\.com/api/webhooks/[0-9]+/' 'Discord webhook'
scan '(sk|pk|gh[pousr])_[A-Za-z0-9]{20,}'      'API key or GitHub token'
scan -- '-----BEGIN [A-Z ]*PRIVATE KEY-----'   'private key'
scan '(api[_-]?key|api[_-]?secret|passwd|password|token)[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9/+_-]{16,}' 'hardcoded credential'

if [ "$fail" = "1" ]; then
  echo
  echo "Credentials belong in the runtime DB, entered through the dashboard."
  echo "See SECURITY.md. To override a false positive: git commit --no-verify"
  exit 1
fi
exit 0
