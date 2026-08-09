# SLC Price Action Trading Bot

Automated implementation of the **SLC price-action playbook** (*Structure · Liquidity · Confirmation*)
for Forex, metals and crypto. A single Python process ingests live data from MetaTrader 5, runs the
SLC strategy at intraday and swing speeds, paper- or live-trades it through an MT5 Expert Advisor,
manages every position to playbook rules, and reports to a web dashboard plus Telegram/Discord. A
bounded self-tuning agent and a news-monitoring agent run alongside it, inside hard safety rails.

> ⚠️ **Educational software, not financial advice.** Run in paper mode for at least 50 trades with
> positive expectancy before considering live trading. See [`LICENSE.md`](LICENSE.md).
>
> 🔐 **No secrets are committed.** `trading-bot/data/` and `trading-bot/state/` are gitignored, so the
> Telegram token, Discord webhook, MT5 login and LAN IPs are **not** in this repo — they live only in
> the runtime DB, entered via the dashboard on each deployment. The repo is proprietary; keep it
> private and read [`SECURITY.md`](SECURITY.md) before sharing.

---

## Architecture

```
MT5 terminal ──(SLCDataBridge.mq5 EA · HTTP push every 5s / poll commands)──► server.py  :8766
                                                                              │
   ┌──────────────────────────────────────────────────────────────────────────────────────┐
   │ server.py    Flask app · EA endpoints · serves dashboard · starts the threads below     │
   │ engine.py    SLC signal execution · paper broker · live command queue · trade mgmt      │
   │ strategy.py  pure SLC + chart-pattern logic (structure, liquidity, sweep, confirmation) │
   │ storage.py   SQLite (data/trading.db, WAL mode) · runtime settings incl. credentials    │
   │ agent.py     bounded self-tuning (whitelisted params only, never risk/stops/mode)        │
   │ notifier.py / telegram_notifier.py   dual-channel Telegram + Discord                     │
   └──────────────────────────────────────────────────────────────────────────────────────┘
                                                                              ▲
news_agent.py (separate process) ── Google-News RSS → sentiment → SL management + market alerts
tv_webhook.py + /api/tv_webhook ── optional TradingView/external alert intake (off by default)
dashboard/index.html ── web UI at http://localhost:8766
```

## Current configuration (shipped defaults — from `trading-bot/config.yaml`)

The runtime DB (`trading-bot/data/trading.db`) is **not** committed, so a fresh clone runs the
`config.yaml` defaults below. A live deployment may tune some of these from the dashboard (the DB
value wins after first run).

| Setting | Value |
|---|---|
| Trading mode | **paper** (virtual balance) |
| Speeds | intraday + swing |
| Server | Flask on `0.0.0.0:8766`, dashboard at `http://localhost:8766` |
| EA | `SLCDataBridge.mq5` **v2.30**; the bot tags its trades with magic **770001** |
| Notifications | Telegram + Discord (configured at runtime via the dashboard) |
| Risk | 1% per A+ trade (B setups ×0.5), min RR **2.0**, ATR buffer 0.35, daily stop −2%, weekly −5% |
| Grade / volume gate | `min_grade = B`, `vol_mult = 1.0` |
| Universe | **8 enabled pairs** (EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD, XAGUSD, BTCUSD, ETHUSD) |

> Port/EA naming is now consistent across the live docs (port **8766**, `SLCDataBridge`). The
> 8765 → 8766 migration is narrated in [`DEVELOPMENT-HISTORY.md`](DEVELOPMENT-HISTORY.md); the
> `legacy/` build legitimately still uses 8765 / `MT5DataBridge`.

## Quick start (the machine running the bot)

```bash
cd trading-bot
pip install -r requirements.txt
python3 server.py            # prints the dashboard URL + LAN IP on startup
```

Open **http://localhost:8766**, then attach the EA in MT5. Full step-by-step (with the MT5
WebRequest allow-list, firewall and EA inputs) is in [`SETUP-GUIDE.md`](SETUP-GUIDE.md).

## What's in this archive

| Doc | Read it for |
|---|---|
| [`README.md`](README.md) | this overview |
| [`TEAM-ONBOARDING.md`](TEAM-ONBOARDING.md) | getting a teammate from zip → running bot, and who owns what |
| [`SETUP-GUIDE.md`](SETUP-GUIDE.md) | detailed MT5 + server + Telegram setup, phase by phase |
| [`WEBHOOKS-AND-INTEGRATIONS.md`](WEBHOOKS-AND-INTEGRATIONS.md) | every endpoint, webhook, token, port and magic number |
| [`SECURITY.md`](SECURITY.md) | where secrets live (runtime DB only) and how to rotate them |
| [`SLC-Price-Action-Playbook.md`](SLC-Price-Action-Playbook.md) | the strategy this code implements |
| [`DEVELOPMENT-HISTORY.md`](DEVELOPMENT-HISTORY.md) | how it was built, key decisions, open items |
| [`CONSOLIDATION.md`](CONSOLIDATION.md) | how the current SLC build and the `legacy/` build relate |
| [`LICENSE.md`](LICENSE.md) | licensing + disclaimer |

| Code & runtime | |
|---|---|
| `trading-bot/` | the Python application (server, engine, strategy, agents, dashboard) — see [`trading-bot/README.md`](trading-bot/README.md) |
| `trading-bot/config.yaml` | startup defaults (the DB wins after first run) |
| `trading-bot/data/`, `trading-bot/state/` | runtime DB, logs, traces — **gitignored, created at runtime, not in the repo** |
| `SLCDataBridge.mq5` / `.original.mq5` | MT5 data-bridge Expert Advisor (v2.30) and baseline |
| `cowork-skills/slc-bot/` | the consolidated Cowork skill (operate / analyze / develop) — replaces the four root `slc-*.skill` bundles |
| `*-REPORT.md`, `*.patch`, `*.diff`, `volume_gate_shadow.py`, `recover-db.sh`, `hallucination_check.py` | strategy experiments, validation reports, and ops tooling |

## Status & roadmap

Live in paper mode. Validated forward: a relative-volume confirmation gate and a dynamic
spread-based stop-loss. A strategy-plugin registry (`trading-bot/strategies/`) and an optional
TradingView webhook (`/api/tv_webhook`, off by default) have landed (SLC = strategy #1). Open
items (tick-accurate EA spread reporting, paper commission/swap modelling, TP2 trailing) are
tracked in [`DEVELOPMENT-HISTORY.md`](DEVELOPMENT-HISTORY.md#open-items--todos--drafts).
