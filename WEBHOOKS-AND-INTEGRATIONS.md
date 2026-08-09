# Webhooks & Integrations

Every external connection the bot makes or accepts, with the exact endpoints, ports and magic
number in use.

> 🔐 Credentials (Telegram token, chat ID, Discord webhook) live **only** in the runtime DB
> (`trading-bot/data/trading.db`, settings table), which is **gitignored and not in the repo**. The
> values are entered via the dashboard at runtime; this doc uses placeholders, never real secrets.
> Treat the repo as private. To rotate, see [`SECURITY.md`](SECURITY.md).

---

## 1. Hosts & ports

| What | Value |
|---|---|
| Bot server (Flask) | binds `0.0.0.0`, port **8766** |
| Dashboard | `http://localhost:8766` (or `http://<server-LAN-IP>:8766` from another machine) |
| News agent → server | `http://127.0.0.1:8766` |
| MT5 EA → server | `http://<server-LAN-IP>:8766` (must be added to MT5's WebRequest allow-list) |
| TradingView/external → server | `POST http://<server-LAN-IP>:8766/api/tv_webhook` (optional, off by default) |
| Bot magic number | **770001** — the bot tags its trades with this (sent in each `open_trade` command; the news agent manages only positions with this magic). The EA does not hardcode it. |

The server prints its dashboard URL and detected LAN IP on startup. The LAN IP changes with DHCP —
if the EA goes "offline," re-check the IP and the allow-list entry first.

## 2. MT5 ⇄ server (the EA "bridge")

The Expert Advisor `SLCDataBridge.mq5` (v2.30) is timer-based. It pushes a JSON feed and bars to the
server over HTTP and polls for trade commands. There is **no inbound connection to MT5** — MT5 always
initiates, which is why the URL must be allow-listed inside the terminal.

One-time MT5 setup: **Tools → Options → Expert Advisors → Allow WebRequest for listed URL**, then add
`http://<server-LAN-IP>:8766` (exact host + port, `http://`, no trailing slash). Attach the EA with
`AllowTradeExecution = false` for paper. Full walkthrough in [`SETUP-GUIDE.md`](SETUP-GUIDE.md).

### Server HTTP API

EA-facing endpoints (the integration surface — see the `# EA-facing endpoints` block in `server.py`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/mt5_feed` | EA pushes live bid/ask/spread per symbol (~every 5 s) |
| POST | `/api/mt5_bars` | EA pushes OHLC bars (~every 60 s) |
| GET | `/api/pairs` | EA polls which symbols the dashboard has enabled |
| GET | `/api/commands/next` | EA polls for the next queued command (open/close/modify) |
| POST | `/api/commands/ack/<cmd_id>` | EA acknowledges a command it executed |

News-agent-facing endpoints (`news_agent.py`, a separate process — see the `# News-agent-facing endpoints` block in `server.py`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | open-positions snapshot for the news agent (live broker positions **plus** the bot's paper trades as pseudo-positions with negative tickets); also reports `ea_connected` |
| POST | `/api/commands` | **SL-management only**: `trail_sl` / `move_sl_be` / `close_trade` (other types rejected `400`; tighten-only — opening/closing new positions stays exclusive to the engine) |

Dashboard & control endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | dashboard UI |
| GET | `/api/state`, `/api/bars`, `/api/trades`, `/api/performance`, `/api/equity`, `/api/signals`, `/api/agent_log` | dashboard data feeds |
| GET | `/api/spread`, `/spread` | spread trace readout |
| POST | `/api/settings` | save runtime settings (risk, RR, pairs, Telegram/Discord, TradingView webhook, etc.) |
| POST | `/api/trade/close/<trade_id>` | manually close a trade |
| POST | `/api/telegram_test` | send a test notification |
| POST | `/api/agent/run` | force a self-tuning agent evaluation now |
| POST | `/api/tv_webhook` | TradingView/external alert intake (off by default — see §3) |

## 3. TradingView / external webhook (inbound, optional)

`POST /api/tv_webhook` accepts alerts from TradingView (or any source). It is **disabled by
default** and only acts when **both** are set via the dashboard:

- `tradingview_webhook_enabled` = true, and
- `tradingview_webhook_token` = a shared secret.

**Auth.** The alert must carry that secret in a `token` (or `passphrase`) field; the server compares
it with `hmac.compare_digest` (constant-time). If either side is empty the request is rejected, so an
unconfigured webhook authenticates nothing. Responses: `403` disabled, `400` unparseable, `401` bad
token, `422` unknown ticker, `200` accepted.

**Payload (JSON).** Fields read (synonyms accepted): `token`/`passphrase`, `ticker`/`symbol`,
`action`/`side` (`buy|long` / `sell|short`), `trade_mode`/`mode` (default `swing`),
`entry`/`price`/`close`, `sl`/`stop`/`stoploss`/`stop_loss`, `tp`/`tp2`/`target`/`takeprofit`,
`tp1`, `strategy`/`strategy_name` (default `tradingview`). Unknown fields are ignored. The TradingView
ticker is mapped to a broker symbol via a built-in table plus any `tradingview_symbol_map` overrides
from settings.

**Safety.** A valid alert is a **candidate**, not an order. It is routed through
`engine.ingest_external_signal` → the **same global risk rails** as a native SLC signal (mode, stop
side, spread, RR, concurrency, correlation, loss limits, position sizing). It is never a raw market
order and never flips `trading_mode` — paper stays the default, live remains the double-gated step.
Parser and gating are covered by `trading-bot/tests/test_tv_webhook.py`.

## 4. Telegram

Outbound only, via the Telegram Bot API: `https://api.telegram.org/bot<token>/sendMessage`
with `{chat_id, text, parse_mode: HTML}`. Every notification is prefixed with a header so it's
distinguishable from other bots sharing the same Telegram account.

**Settings keys (runtime DB — placeholders shown, never real values):**

```
telegram_enabled   = true
telegram_bot_token = <YOUR_TELEGRAM_BOT_TOKEN>
telegram_chat_id   = <YOUR_CHAT_ID>
```

Re-create from scratch if needed: message **@BotFather** → `/newbot` → copy token; message your bot
once; message **@userinfobot** for your numeric chat id; paste both into the dashboard Telegram panel →
Enable → Send test message.

You get alerts on: trade opened, TP1 hit (50% banked, stop to breakeven), trade closed (TP2 / stop /
trailing / manual, with price + P&L + R), news-driven SL changes, mode switches, and agent
adjustments. Shadow trades are intentionally silent.

## 5. Discord

Outbound only, via a Discord **incoming webhook**. The shared notifier converts the Telegram-HTML
message to Discord markdown and POSTs it to the webhook URL in parallel with Telegram.

**Settings keys (runtime DB — placeholders shown, never real values):**

```
discord_enabled     = true
discord_webhook_url = <YOUR_DISCORD_WEBHOOK_URL>
```

Re-create from scratch if needed: Discord → Server Settings → Integrations → Webhooks → New Webhook →
copy URL → paste into the dashboard.

> Operational gotcha: the **main bot reads notifier settings live** on each send, but the
> **news agent builds its notifier once at startup** — so after enabling/changing Discord you must
> restart the news agent for it to pick up the change. A `config.yaml` edit also requires a server
> restart to take effect.

## 6. News data source

`news_agent.py` pulls headlines from **free Google News RSS** (no API key, no webhook in). It scores
sentiment, and for the bot's own open positions (filtered by magic **770001**) it may trail the stop,
move to breakeven, or cut a losing trade on a strong adverse score. The EA still refuses any SL change
that *loosens* a stop. Market-wide headline alerts are pushed to Telegram/Discord.

> `config.yaml` ships `news_agent.live_mode: true`, so as deployed the news agent **acts** (updates
> paper SLs in the DB; sends live SL changes to MT5). Set it to `false` for a dry run. It only ever
> manages positions with magic 770001.

## 7. Credentials summary

| Integration | Where stored | Direction | Status |
|---|---|---|---|
| Telegram bot | runtime DB (`data/trading.db`, not in repo) → settings | outbound | runtime-configured |
| Discord webhook | runtime DB (`data/trading.db`, not in repo) → settings | outbound | runtime-configured |
| TradingView webhook | runtime DB → settings (enable flag + token) | inbound to server | off by default |
| MT5 EA | terminal-side WebRequest allow-list | inbound to server | configured per machine |
| Google News RSS | none (public) | outbound | no key |

`trading-bot/config.yaml` ships these as **empty strings** by design — the real values are written to
the database at runtime from the dashboard and are never committed (the DB is gitignored).
