---
name: slc-bot
description: >
  Operate AND develop the SLC (Structure·Liquidity·Confirmation) price-action
  trading bot in this project (Python + MT5 + Flask; paper/live FX, metals,
  crypto; strategy-plugin engine; TradingView webhook; Telegram/Discord;
  self-tuning + news agents). Use for ANYTHING about this bot: status, open
  trades, PnL/equity, the sanity check, backtests, TradingView market context,
  why a signal fired or was skipped, health/DB integrity — and for development:
  adding a strategy or technique, extending the TradingView webhook, adding
  notifications/alerts, running the tests, packaging a release. Trigger even
  when unnamed: "how's the bot", "open trades", "run sanity", "backtest XAUUSD",
  "market context", "why was that skipped", "add a strategy", "wire up an alert",
  "extend the webhook", "run the tests", "ship it". Always consult this skill
  before editing the bot's code so its safety rails and architecture are kept.

---

# SLC Trading Bot — operate & develop

This skill drives the project's trading bot. It has three **sectors**; read only the
reference file for the sector you need (progressive disclosure keeps this fast and cheap):

| Sector | Use it for | Read |
|---|---|---|
| **operate** | live status, open trades, PnL/equity, signals (taken/skipped), TradingView market context, health & DB integrity | `references/operate.md` |
| **analyze** | backtests, the sanity/parameter sweep, live-vs-shadow study, tuning recommendations | `references/analyze.md` |
| **develop** | add a strategy/technique, extend the TradingView webhook, add notifications/alerts, run tests, package a release | `references/develop.md` |

Exact endpoints, response fields, and script flags live in `references/api.md` — read it whenever
you need precise field names or CLI options.

A thin helper is provided for quick reads (no dependencies, stdlib only):
```bash
python3 scripts/slc.py status     # account + open trades + perf, compact
python3 scripts/slc.py signals    # recent signals with status + reason
python3 scripts/slc.py perf       # closed + shadow performance
python3 scripts/slc.py health     # server/EA/feed/DB reachability
```

## Configuration (set once)

The bot runs locally; the operational commands target it. Defaults:
- `BOT_DIR` = `~/Claude/Projects/Price Action Strategy/trading-bot`  (note the space — always quote it)
- `BASE_URL` = `http://127.0.0.1:8766`  (used by the `curl` examples)
- `SLC_BASE_URL` = same value — the env var `scripts/slc.py` actually reads (defaults to `http://127.0.0.1:8766`)

If the user's paths differ, ask once and use theirs. Every shell example below assumes:
```bash
BOT_DIR="$HOME/Claude/Projects/Price Action Strategy/trading-bot"
BASE_URL="http://127.0.0.1:8766"              # for the curl examples below
export SLC_BASE_URL="$BASE_URL"               # scripts/slc.py reads SLC_BASE_URL
```
If a command fails with "connection refused", the server isn't running:
```bash
cd "$BOT_DIR" && python3 server.py        # dashboard at http://localhost:8766
# or check the watchdog:  ./watchdog-install.sh status
```

## Global safety rules (apply in EVERY sector — never weaken)

These are the bot's invariants. They hold in paper AND live. Operating or developing the bot must
never break them; if code and the playbook disagree, the playbook wins and the gap is a bug to flag.

1. **Paper is the default/safe state.** Live only via the promotion gate (≥50 closed paper trades with
   positive expectancy, kill switches + breakeven-at-TP1 verified, DB clean).
2. **Going live is double-gated:** EA `AllowTradeExecution=true` AND a deliberate dashboard switch with
   confirmation. Never script, automate, or shortcut around that. Never flip a bot to live from a skill.
3. **Stops are only ever tightened, never loosened.** The EA refuses any loosening; never write code or
   issue a command that bypasses that.
4. **The self-tuning agent only nudges a whitelist** (min_rr, atr_buffer — bounded). It must never touch
   `risk_pct`, stops, `max_concurrent`, or `trading_mode`, never restart services, never flip to live.
5. **Risk ≤ 1%/trade** (1% only for 6/6 A+ in a normal regime, else 0.5%); daily −2% / weekly −5% are
   hard kill switches; after 3 losses halve risk until 2 wins; no averaging down / revenge / chasing.
6. **External signals (TradingView webhook) are candidates, not orders** — they must pass the same rails
   as an SLC signal (mode, valid stop on the correct side, spread, RR, concurrency, correlation, balance,
   loss limits, sizing). Never a raw market order.
7. **Never act on a corrupt DB** — if integrity is in doubt run `hallucination_check.py` (read-only) /
   `recover-db.sh` first. Integrity checks and validators are read-only.
8. **Secrets never live in source** — only in the runtime DB (entered via the dashboard). Never echo,
   paste, or commit `data/` or `state/`; rotate any credential the moment it's exposed.

When you change behaviour, say which file and why, and which invariant(s) you checked it against.
The bot is **educational software, not financial advice** — never promise returns.
