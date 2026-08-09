# Team Onboarding

How to go from this zip to a running bot, and how the project is meant to be operated. New to the
strategy itself? Read [`SLC-Price-Action-Playbook.md`](SLC-Price-Action-Playbook.md) first — the code
is just an honest implementation of it.

## 1. Prerequisites

- **Python 3.10+** on the machine that will run the bot (macOS or Linux; the owner runs macOS).
- **MetaTrader 5** on a machine that can reach the bot over the LAN (can be the same machine).
- A broker account in MT5 for live data (paper trading still needs the live MT5 price feed).
- Optional: a Telegram account and/or a Discord server for notifications (already configured — see
  [`WEBHOOKS-AND-INTEGRATIONS.md`](WEBHOOKS-AND-INTEGRATIONS.md)).

## 2. Get it running (≈10 minutes)

```bash
unzip SLC-Trading-Bot-team-archive.zip
cd slc-trading-bot/trading-bot
pip install -r requirements.txt
python3 server.py
```

The server prints the dashboard URL and the machine's LAN IP. Open **http://localhost:8766** — the
header will show *EA: offline* until MT5 is connected. Then follow
[`SETUP-GUIDE.md`](SETUP-GUIDE.md) to compile/attach the EA and allow-list the WebRequest URL.

> Ports/EA naming: the live system uses **port 8766** and the **`SLCDataBridge`** EA throughout the
> docs. (The `legacy/` build legitimately uses 8765 / `MT5DataBridge` — see [`CONSOLIDATION.md`](CONSOLIDATION.md).)

## 3. What ships — and what doesn't

This repo ships **code, docs, and config only** — no runtime state and no secrets:

- `trading-bot/data/` (the SQLite DB) and `trading-bot/state/` (logs, news decisions, spread traces)
  are **gitignored and not in the repo**. They are created on first run.
- `trading-bot/config.yaml` ships startup defaults with **empty** credential fields. On first run the
  bot creates `data/trading.db` from these defaults; settings you change in the dashboard (including
  Telegram/Discord credentials) are written to that DB and win over `config.yaml` thereafter.
- There are therefore **no live credentials in a clone**. Enter them via the dashboard on each
  deployment, and read [`SECURITY.md`](SECURITY.md) before sharing the repo.

## 4. Paper vs live (the safety model)

- **PAPER** (current): the engine simulates fills on live MT5 prices and tracks P&L against a virtual
  balance. Full TP1/breakeven/trail management runs. This is the default and where you should stay.
- **LIVE**: requires *both* `AllowTradeExecution = true` in the EA inputs *and* switching the
  dashboard to LIVE (with a confirmation prompt). The EA refuses any stop change that loosens a stop.
- **OFF**: data flows and analysis runs, no new trades; open trades still managed.

House rule from the playbook and the build history: **50+ paper trades with positive expectancy before
going live.** If another system manages stops on the same MT5 account, run live on only one at a time.

## 5. Operating the bot

- **Dashboard** (`http://localhost:8766`): performance, chart with entry/exit markers, trade history
  filters, live per-symbol engine analysis, pairs manager, and the settings panel.
- **Cowork skill** (`cowork-skills/slc-bot/`): operate it conversationally. The consolidated `slc-bot`
  skill (operate / analyze / develop, loaded on demand) replaces the four older root-level
  `slc-*.skill` bundles (status, tv-context, sanity, backtest). Package and install it via
  Cowork → Settings → Capabilities. See [`cowork-skills/README.md`](cowork-skills/README.md).
- **Self-tuning agent**: evaluates every 4 h once ≥15 trades close; only nudges a small whitelist of
  parameters and can never touch risk %, stops, concurrency, or trading mode. Toggle/force-run it from
  the dashboard header.
- **News agent** (`python3 news_agent.py`, separate process): monitors headlines and manages the
  bot's own positions' stops; restart it after changing notification settings.

## 6. Recovering from problems

- **EA offline:** server running? LAN IP correct? exact WebRequest allow-list URL? firewall? same
  network? See the troubleshooting tables in [`SETUP-GUIDE.md`](SETUP-GUIDE.md).
- **DB corruption:** run `recover-db.sh` — it stops the server gracefully, backs up the bad DB,
  rebuilds with `sqlite3 .recover`, integrity-checks, and swaps it in. The DB runs in WAL mode.
- **"Is the agent acting on bad data?"** `hallucination_check.py` (read-only) verifies DB integrity,
  feed freshness, and that the agent stayed within its rules; verdict logged to
  `hallucination_check.jsonl`.

## 7. Where to read next

- Strategy rationale and rules → [`SLC-Price-Action-Playbook.md`](SLC-Price-Action-Playbook.md)
- Every endpoint/webhook/token/port → [`WEBHOOKS-AND-INTEGRATIONS.md`](WEBHOOKS-AND-INTEGRATIONS.md)
- How and why it was built, open items → [`DEVELOPMENT-HISTORY.md`](DEVELOPMENT-HISTORY.md)
- Detailed first-time MT5 setup → [`SETUP-GUIDE.md`](SETUP-GUIDE.md)
