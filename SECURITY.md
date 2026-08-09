# Security notes — read before sharing or pushing

This repo is **hardened so that no secrets are committed**. Runtime data and credentials live only
in the bot's runtime database and logs, which are gitignored and are **not** part of the repository.

## Where secrets live (and where they don't)

| Secret | Lives in | In the repo? |
|---|---|---|
| Telegram bot token | runtime DB (`trading-bot/data/trading.db`, settings table) | **No** — `data/` is gitignored |
| Telegram chat ID | runtime DB | **No** |
| Discord webhook URL | runtime DB | **No** |
| MT5 account login + balances | runtime logs (`trading-bot/state/*.log`) | **No** — `state/` is gitignored |
| LAN IP addresses | runtime logs (`trading-bot/state/*.log`) | **No** |

`trading-bot/config.yaml` ships these as **empty strings** by design — the real values are entered
through the dashboard at runtime and written to the database. The `.gitignore` already excludes
`trading-bot/data/`, `trading-bot/state/`, `trading-bot/.backups/`, and the usual secret/DB/log
patterns, so a clone of this repo contains **no live credentials**.

## Do this

1. **Keep the repository private.** `LICENSE.md` is proprietary, and the dashboard controls a
   trading account.
2. **Never commit `trading-bot/data/`, `trading-bot/state/`, or `.backups/`.** They are gitignored;
   don't force-add them. Don't paste real tokens into any tracked file (docs included — keep them
   redacted/placeholder).
3. Re-enter the Telegram/Discord credentials through the dashboard on each deployment.

## How to rotate credentials (if one is ever exposed)

- **Telegram:** message **@BotFather** → `/revoke` → pick the bot → it issues a new token; paste the
  new token into the dashboard Telegram panel. The old token stops working immediately.
- **Discord:** Server Settings → Integrations → Webhooks → delete the webhook (or copy a fresh URL);
  paste the new URL into the dashboard.

## If a credential was ever committed in history

Rotating (above) is the first and most important step — it invalidates the leaked value. The current
tree is clean; if a secret appears anywhere in past git history, rotate it and, if needed, scrub the
history (e.g. `git filter-repo`) before making the repo more widely accessible. After first push,
enable **Secret scanning + Push protection** in the GitHub repo settings as a backstop.

## Note on git history

This repo was hardened after an earlier "team archive" era that bundled the runtime DB and logs on
purpose. If you are auditing, confirm that no `trading.db`, `state/*.log`, or `.backups/` blobs are
reachable from the current `HEAD` (they are not) and rotate any credential that ever lived in an
older commit.
