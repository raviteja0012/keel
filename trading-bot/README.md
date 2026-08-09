# SLC Trading Bot

Automated implementation of the SLC price action playbook (`../SLC-Price-Action-Playbook.md`)
with paper/live trading through MetaTrader 5, a self-tuning agent, a news agent, an optional
TradingView webhook, Telegram **and** Discord alerts, and a web dashboard.

```
MT5 terminal ──(SLCDataBridge.mq5 EA · HTTP push 5s / poll commands)──► server.py :8766
                                                                        │
   ┌──────────────────────────────────────────────────────────────────────────────────┐
   │ server.py     Flask app · EA endpoints · serves dashboard · starts the threads     │
   │ engine.py     SLC signal execution · paper broker · live command queue · trade mgmt │
   │ strategy.py   pure SLC + chart-pattern logic (structure, liquidity, sweep, confirm) │
   │ strategies/   strategy-plugin registry (SLC = plugin #1) behind the shared engine   │
   │ storage.py    SQLite (data/trading.db, WAL) · runtime settings incl. credentials    │
   │ agent.py      bounded self-tuning (whitelisted params only, never risk/stops/mode)   │
   │ notifier.py + telegram_notifier.py   dual-channel Telegram + Discord                 │
   │ tv_webhook.py + tv_context.py        TradingView alert intake + market context       │
   └──────────────────────────────────────────────────────────────────────────────────┘
                                                                        ▲
news_agent.py (separate process) ── Google-News RSS → sentiment → SL management + alerts
dashboard/index.html ── web UI at http://localhost:8766
```

> Live system uses **port 8766** and the **`SLCDataBridge`** EA (v2.30). Earlier revisions of this
> file referenced port 8765 / `MT5DataBridge`; those values were stale and have been corrected.

## 1. Install & run (this machine)

```bash
cd trading-bot
pip install -r requirements.txt   # flask, requests, pyyaml
python3 server.py                 # prints the dashboard URL + LAN IP on startup
```

Open **http://localhost:8766**. One process runs the engine, agent, and notifier threads.
The news agent is a **separate** process:

```bash
python3 news_agent.py             # restart this after changing notification settings
```

## 2. MT5 side (one-time)

1. Open `../SLCDataBridge.mq5` in MetaEditor and **recompile (F7)** — v2.30 adds
   open/close trade execution on top of SL management.
2. MT5 → Tools → Options → Expert Advisors → tick *Allow WebRequest* and add
   `http://<your-server-ip>:8766` (the IP `server.py` prints at startup; `http://`, no trailing slash).
3. Attach the EA to any chart. Inputs that matter:
   - `ServerHost` — IP of the machine running `server.py` (`127.0.0.1` if same machine)
   - `ServerPort` — `8766` (default; must match `config.yaml` and the WebRequest URL)
   - `AllowTradeExecution` — **false by default.** Leave false for paper trading.
     Set true ONLY when you switch the dashboard to LIVE.
   - `MaxLotsPerTrade`, `MaxOpenPositions` — broker-side hard caps.
4. Within ~60 s the dashboard header shows **EA: connected** and charts fill.

Full step-by-step (firewall, failure signatures, going-live) is in `../SETUP-GUIDE.md`.

## 3. Notifications (Telegram + Discord)

Both are outbound-only and configured at runtime via the dashboard — never in source.

- **Telegram:** message **@BotFather** → `/newbot` → copy the **token**; message your bot once;
  message **@userinfobot** for your numeric **chat id**; paste both into the dashboard Telegram
  panel → Enable → **Send test message**.
- **Discord:** Server Settings → Integrations → Webhooks → New Webhook → copy URL → paste into the
  dashboard. After enabling/changing Discord, **restart `news_agent.py`** (it builds its notifier
  once at startup; the main bot reads settings live).

You'll get alerts on: trade opened, TP1 hit (50% banked, stop to breakeven), trade closed
(TP2 / stop / trailing / manual, with price + P&L + R), skipped signals (optional), news-driven SL
changes, agent adjustments, and mode switches. Shadow trades are intentionally silent.

See `../WEBHOOKS-AND-INTEGRATIONS.md` for every endpoint, port, magic number and integration.

## 4. Paper vs Live

- **PAPER** (default): the engine simulates fills on live MT5 prices, full TP1/BE/trail
  management, P&L tracked against a virtual balance (`paper_start_balance`, default 10,000).
- **LIVE**: the engine queues `open_trade` commands; the EA executes them on your account.
  Requires `AllowTradeExecution=true` in EA inputs *and* switching the dashboard to LIVE
  (it asks for confirmation). Exits at the broker are detected and reconciled automatically.
- **OFF**: data keeps flowing and analysis runs; no new trades. Open trades are still managed.

Run paper for **at least 50 trades with positive expectancy** before considering live. No strategy
guarantees returns; this is educational software, not financial advice.

## 5. The self-tuning agent

Evaluates every 4 h (config) once ≥15 trades are closed. It may only:
nudge `atr_buffer` (within 0.25–0.60), nudge `min_rr` (within 1.8–3.0), raise/lower `min_grade`
(A/B), disable/re-enable a symbol, or disable a mode (intraday/swing). It can **never** touch
`risk_pct`, daily/weekly stops, `max_concurrent`, or `trading_mode`. Every change is logged in the
dashboard Agent Log and announced on Telegram/Discord. Toggle it off in the header; "Evaluate now"
forces a run.

## 6. TradingView webhook (optional, OFF by default)

`server.py` exposes `POST /api/tv_webhook` to accept external alerts (TradingView or any source).
It is **disabled** unless both `tradingview_webhook_enabled` is on and a
`tradingview_webhook_token` is set (via the dashboard). A valid, token-authenticated alert is
treated as a *candidate* and routed through `engine.ingest_external_signal` → the **same risk rails**
as a native SLC signal (mode, stop side, spread, RR, concurrency, correlation, loss limits, sizing).
It is never a raw market order and never flips `trading_mode`. See the parser and tests in
`tv_webhook.py` / `tests/test_tv_webhook.py`.

## 7. Dashboard

- **Performance** — win rate, expectancy (R), profit factor, total R, grade A/B split.
- **Chart** — MT5 bars; ▲▼ entry markers, ⚑ exit flags, dashed SL/TP1/TP2 lines on open trades.
- **History** — filter by mode, speed, symbol, grade, win/loss, period.
- **Engine analysis** — live per-symbol reasoning (bias, regime, why it's waiting).
- **Pairs manager** — toggle symbols; the EA follows within 30 s.
- **Settings** — risk %, RR, ATR buffer, stops, grades, intraday/swing toggles, Telegram/Discord,
  TradingView webhook enable + token.

## 8. Default parameters (`config.yaml`)

`config.yaml` holds startup defaults; anything also shown in the dashboard Settings panel is
runtime-tunable and stored in the DB (the DB value wins after first run). Shipped defaults:

| Param | Default | Param | Default |
|---|---|---|---|
| `trading_mode` | paper | `min_grade` | B |
| `risk_pct` | 1.0 % (A+) | `b_setup_risk_factor` | 0.5 |
| `min_rr` | 2.0 | `atr_buffer` | 0.35 |
| `vol_mult` | 1.0 | `max_concurrent` | 2 / mode |
| `max_correlated` | 3 | `daily_stop_pct` | 2.0 |
| `weekly_stop_pct` | 5.0 | enabled pairs | 8 (EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD, XAGUSD, BTCUSD, ETHUSD) |

> `config.example.yaml` is shipped as a copy of `config.yaml` for reference.

## 9. Files

| File / dir | Role |
|---|---|
| `config.yaml` | startup defaults (DB-stored settings win after first run) |
| `config.example.yaml` | reference copy of `config.yaml` |
| `server.py` | Flask app + EA/dashboard endpoints + starts engine/agent/notifier threads |
| `engine.py` | signal execution, paper broker, live commands, trade management, external-signal intake |
| `strategy.py` | pure SLC logic (structure, liquidity, sweep, confirmation, ATR regime) |
| `strategies/__init__.py` | strategy-plugin registry (SLC = plugin #1) behind the shared engine |
| `agent.py` | performance evaluation + bounded self-tuning |
| `notifier.py` | dual-channel notifier (Telegram + Discord) |
| `telegram_notifier.py` | Telegram Bot API client |
| `news_agent.py` | **separate process** — Google-News RSS monitor + SL management + market alerts |
| `news_evaluator.py` | sentiment scoring / decision logic used by the news agent |
| `tv_webhook.py` | TradingView/external alert parser + token auth (pure, testable) |
| `tv_context.py` | market-context snapshot (USD bias, top setups) used by the `slc-tv-context` skill |
| `storage.py` | SQLite layer (`data/trading.db`, created automatically, WAL mode) |
| `dashboard/index.html` | the web UI |
| `backtest.py` | replay stored bars through the strategy |
| `sanity_check.py` | parameter sweep + health checks |
| `tests/` | `test_strategy_registry.py`, `test_tv_webhook.py` (no MT5 required) |
| `watchdog-install.sh` | installs the launchd watchdog services (`com.slc.*`, port 8766) |

## 10. Testing

The unit tests need no MT5 or running server:

```bash
cd trading-bot
python3 tests/test_strategy_registry.py   # strategy-plugin registry
python3 tests/test_tv_webhook.py          # TradingView parser + gated endpoint
```

## 11. Tools & scripts (repo root)

| Script | What it does |
|---|---|
| `recover-db.sh` | stops the server, backs up a corrupt DB, rebuilds via `sqlite3 .recover`, integrity-checks, swaps it in |
| `hallucination_check.py` | read-only DB/feed/agent-rule integrity check; verdict → `hallucination_check.jsonl` |
| `reset_ledger.py` | reset the paper ledger / balance |
| `shadow_report.py`, `shadow_report_corrected.py` | summarize shadow-trade performance |
| `spread_report.py` | summarize spread traces |
| `pattern_sanity_check.py` | chart-pattern detector sanity sweep |
| `watchdog-install.sh` | installs/keeps the launchd services alive (current; `com.slc.*`, 8766) |

> **Autostart:** use `watchdog-install.sh` (installs/keeps the `com.slc.*` launchd services on port
> 8766). The old `install_autostart.sh` / `watchdog.sh` scripts (legacy `com.tradingbot.*` / port 8765)
> have been removed from the repo root; the superseded `legacy/` build retains its own copies under
> `legacy/pattern-strategy-fastapi/trading-bot/tools/`.

## Troubleshooting

- **EA: offline** — check the WebRequest allow-list URL matches `ServerHost:Port` (8766), firewall,
  and that both machines are on the same network.
- **No trades for days** — normal. The system trades when the SLC checklist aligns; check Engine
  Analysis to see what it's waiting for per symbol.
- **`open_trade REFUSED`** in MT5 Experts tab — `AllowTradeExecution` is still false (the safety working).
- Scalping (1–5m) isn't supported: the bridge pushes 15m+ bars by design.
