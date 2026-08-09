# CLAUDE.md — SLC Trading Bot

Canonical instructions for Claude Code (and any AI agent) working in this repo. Read this
before touching anything. The chat-side Claude Project mirrors these rules; this file is the
source of truth for the repo.

## What this is and what we're building toward

The **SLC Price Action Trading Bot** implements the SLC (Structure · Liquidity · Confirmation)
price-action playbook for Forex, metals and crypto. A single Python process ingests live MT5
data via the `SLCDataBridge` Expert Advisor, runs the strategy at intraday/swing speeds,
trades it through MT5, manages every position to playbook rules, and reports to a web
dashboard plus Telegram/Discord. A bounded self-tuning agent and a news-monitoring agent run
alongside it inside hard safety rails.

**Objective:** this is being built to trade **live**. Paper mode is the current state and the
*validation gate*, not the end goal. The plan is paper → prove it → promote to live. The
safety rails below are not "anti-live" — they are how we survive once real money is on the
line, so they apply in paper and live alike (and matter more live).

`SLC-Price-Action-Playbook.md` is the north star. The code is an honest implementation of it;
when code and playbook disagree, the playbook is the intended behavior and the gap is a bug.

## Repo layout

| Path | Role |
|---|---|
| `SLC-Price-Action-Playbook.md` | the strategy this code implements (north star) |
| `docs/MULTI-ASSET-ARCHITECTURE.md` | multi-asset architecture (Phase 1 proposal + inventory) |
| `docs/LIVE-EXECUTION-VPS.md` | live topology runbook: Mac ↔ Windows-VPS EA split |
| `trading-bot/server.py` | Flask app, EA endpoints, serves legacy dashboard, starts threads |
| `trading-bot/engine.py` | execution choke point (ALL rails), paper broker, live command queue, trade mgmt, spread window |
| `trading-bot/strategy.py` | pure SLC + chart-pattern logic (structure, liquidity, sweep, confirmation) |
| `trading-bot/strategies/` | strategy-plugin registry (SLC = plugin #1) behind the shared engine |
| `trading-bot/storage.py` | SQLite (WAL), additive migrations, hardened command queue, runtime settings **including credentials** |
| `trading-bot/instruments.py` | per-symbol registry: asset class, factor exposures, calendars, news entities |
| `trading-bot/sessions.py` | per-class market calendars, day/week boundaries, crypto weekend risk factor |
| `trading-bot/exposure.py` | cross-asset factor-bucket exposure ledger + gate (lesson: correlated bets stack) |
| `trading-bot/params_store.py` | the ONLY parameter write path: per-origin whitelists, bounds, code ceilings, structured audit |
| `trading-bot/decisions.py` | decision audit — every decision incl. decisions NOT to trade |
| `trading-bot/news_calendar.py` | scheduled-event blackouts per asset class; fails safe when the calendar is stale |
| `trading-bot/analysis.py` | importable studies: regime/session perf, exposure, drawdown forensics, promotion-gate status |
| `trading-bot/dashboard_api.py` + `dashboard/multiasset.html` | localhost-only FastAPI control dashboard (port 8767) |
| `trading-bot/live_switch.py`, `dash_auth.py` | two-step, promotion-gated live switch — the ONLY path to live |
| `trading-bot/agent.py` | bounded self-tuning (whitelisted params only; writes only via params_store) |
| `trading-bot/notifier.py`, `telegram_notifier.py` | dual-channel Telegram + Discord |
| `trading-bot/news_agent.py`, `news_evaluator.py` | RSS news monitor + SL management (separate process) |
| `trading-bot/tv_webhook.py`, `tv_context.py` | TradingView/external alert intake (`/api/tv_webhook`) + market-context snapshot |
| `trading-bot/backtest.py`, `sanity_check.py` | replay + parameter sweep |
| `trading-bot/tests/` | risk-rail / circuit-breaker / promotion-gate suites — keep green (`python3 tests/<file>.py`), no MT5 needed |
| `trading-bot/config.yaml`, `config.example.yaml` | startup defaults (DB values win after first run); example is the committed template |
| `install-multiasset-services.sh` | launchd install/status/uninstall for the multi-asset stack (`com.slcmulti.*`) |
| `SLCDataBridge.mq5` / `.original.mq5` | MT5 data-bridge EA (v2.30) and baseline |
| `cowork-skills/slc-bot/` | consolidated Cowork skill (operate/analyze/develop) — replaces the four root `slc-*.skill` bundles |
| `*-REPORT.md`, `*.patch`, `*.diff`, `volume_gate_shadow.py`, `recover-db.sh`, `hallucination_check.py` | experiments, validation, ops tooling |

`trading-bot/data/` (the SQLite DB) and `trading-bot/state/` (logs, decisions, traces) are
**runtime state and are gitignored** — see Security.

## Run / dev

```bash
cd trading-bot
pip install -r requirements.txt
python3 server.py          # EA endpoints + engine + legacy dashboard at http://localhost:8766
python3 news_agent.py      # separate process; restart after changing notification settings
python3 dashboard_api.py   # multi-asset control dashboard at http://127.0.0.1:8767 (localhost only)
for t in tests/test_*.py; do python3 "$t"; done   # rails/gate suites — must stay green
```

Then attach the `SLCDataBridge` EA in MT5 and allow-list the WebRequest URL — see
`SETUP-GUIDE.md`.

## Authoritative config

- Live system uses **port 8766** and the **`SLCDataBridge`** EA (v2.30). The bot tags its trades
  with magic **770001** (sent in each open command; the news agent manages only these) — the EA
  itself does not hardcode 770001. `config.yaml` is authoritative for what ships; the runtime DB
  (gitignored, not in the repo) wins after first run.
- Shipped defaults (`config.yaml`): mode **paper**, speeds **intraday + swing**, risk 1% per A+
  trade (B setups ×0.5), min RR **2.0**, ATR buffer 0.35, daily stop −2%, weekly −5%, volume gate
  `vol_mult = 1.0`, `min_grade = B`, **8 enabled pairs**. (A live deployment's DB may differ; since
  the DB isn't committed, treat `config.yaml` as the source of truth for the repo.)
- The `legacy/` build legitimately uses port 8765 / `MT5DataBridge`; that is not stale, it is the
  superseded build (see `CONSOLIDATION.md`).

## Safety invariants (hold in paper AND live — never weaken)

1. **Paper is the default/safe state.** Live is the goal, but only via the promotion gate below.
2. Going live requires BOTH `AllowTradeExecution = true` in the EA inputs AND a deliberate
   dashboard switch with confirmation. Never script, automate, or shortcut around that double gate.
3. **Never widen or loosen a stop.** The EA itself refuses any stop change that loosens a stop —
   never write code that bypasses that refusal.
4. The self-tuning agent may only nudge a small whitelist of parameters. It must **never** touch
   `risk_pct`, stops, `max_concurrent`, or `trading_mode`.
5. The self-tuning agent never restarts services and never flips to live. Reject any change that
   grants it those powers.
6. Shadow trades are silent: they never notify and are never tuned on as if live. Keep
   paper/live and shadow strictly separate.
7. **Never act on numbers from a corrupt DB.** If integrity is in doubt, run
   `hallucination_check.py` (read-only) / `recover-db.sh` first.
8. Risk ≤ 1% per trade — 1% only for 6/6 A+ setups in a normal regime, otherwise 0.5%.
9. Daily **−2%** and weekly **−5%** kill switches are hard stops.
10. After 3 consecutive losses, halve risk until 2 consecutive wins.
11. No averaging down, no revenge trades, no moving stops away from price.
12. Stand aside in shock regime (ATR(14)/ATR(100) > 2.5); don't hold a scalp through a known
    data release with a stop inside the expected spike range.
13. **Secrets never live in source.** They live only in the runtime DB (entered via the
    dashboard). Never commit `data/` or `state/`; rotate any credential the moment it's exposed.

## Going-live promotion gate (the actual milestone)

Before flipping any account to live:
- **≥ 50 closed paper trades with positive expectancy** on the current parameter set (playbook §12).
- Behavior changes since the last validation re-tested in paper or shadow first.
- Daily/weekly kill switches, breakeven-at-TP1, and stop-loss management verified working in paper.
- DB integrity clean (`hallucination_check.py` green).
- If another system manages stops on the same MT5 account, only one runs live at a time.
- Start live at minimum size; treat the first live trades as a continuation of forward-testing.

## Security

The original team archive bundled live runtime data on purpose. In this repo, secrets do **not**
go in git:
- `trading-bot/data/` and `trading-bot/state/` are gitignored. The Telegram token, chat ID,
  Discord webhook, MT5 login and LAN IPs only ever existed in the DB/logs — keep it that way.
- Re-enter Telegram/Discord credentials through the dashboard on each deployment. The code is
  written for exactly this (secrets in the DB at runtime, never in source).
- If a secret is exposed: Telegram → @BotFather `/revoke`; Discord → delete/recreate the webhook;
  paste the new value into the dashboard. See `SECURITY.md`.

## How to work in this repo

- Don't edit anything under `data/` or `state/` — that's runtime state.
- Back up before editing `config.yaml`, `strategy.py`, or `engine.py` (the project already
  timestamps backups under `.backups/`).
- Treat `config.yaml` as the source of truth for runtime params; the DB wins after first run.
- Prefer changes that are paper-validated or shadow-tested before they affect live behavior.
- When you change behavior, state which file and why, and which invariant(s) you checked it against.

## Multi-strategy scope (consolidated repo)

This repo is the home for the whole effort, and is expanding beyond SLC. SLC is **strategy #1**.
New strategies are added as **isolated modules behind the shared engine and the GLOBAL risk
rails** — never fork the rails per strategy, never let one strategy's tuning touch another, and
each new strategy clears its **own** ≥50-trade positive-expectancy paper gate before live. The
plumbing for this has landed: a strategy-plugin registry (`trading-bot/strategies/__init__.py`,
SLC registered as plugin #1) and an optional TradingView/external-signal intake
(`/api/tv_webhook`, off by default) that routes candidates through the same global rails. The
earlier FastAPI/port-8765 build under `legacy/` is **superseded**: reference only (its labeled
dataset, validation gallery, and context handoff), never deployed or merged.

## Open items (not done — see DEVELOPMENT-HISTORY.md)

EA tick-accurate spread reporting (untested MQL5 draft), paper commission/swap modelling (not
built), per-level `force` flag (deferred), TP2/runner trailing leaving gains on the table
(swing winners avg +1.16R but capture only +0.16R), and symbol culling deferred until ~15+ trades
per pair. Recently resolved: the EA now reports **2.30** consistently (`#property`, startup `Print`,
and JSON feed); the legacy port-8765 autostart scripts (`install_autostart.sh`, `watchdog.sh`) were
removed from the root (`watchdog-install.sh` → `com.slc.*`/8766 is the only autostart path now); and
`config.example.yaml` is now a documented template rather than an exact copy of `config.yaml`.

---
*Educational trading software, not financial advice. See `LICENSE.md`.*
