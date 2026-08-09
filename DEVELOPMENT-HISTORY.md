# SLC Price Action Trading Bot — Development History

*Reconstructed from the project's Cowork chat sessions and repository files. Dates and figures
are taken from the chats, reports, and file timestamps; where the chats disagreed with each
other (e.g. account balances, port numbers, service names), the discrepancies are noted rather
than resolved.*

---

## What this project is

The **SLC Price Action Trading Bot** is an automated implementation of a hand-written trading
playbook, the *SLC System — Structure · Liquidity · Confirmation* (`SLC-Price-Action-Playbook.md`).
The strategy is "pure price action": it never asks *why* price moved, only *where structure broke,
where liquidity sits, and whether the market confirmed intent*. One rule set runs at three speeds
(scalp / intraday / swing), each using a timeframe triplet of HTF bias → MTF setup → LTF trigger.
Risk is fixed at 0.5–1% per trade and every stop and position size is scaled by ATR, which is what
makes the system "regime-agnostic".

The software around that playbook grew into a single-process Python application:

```
MT5 terminal ──(SLCDataBridge EA, HTTP push/poll)──► server.py :8766
                                                      ├─ engine.py    SLC execution, paper broker, live commands
                                                      ├─ strategy.py   pure SLC + pattern logic
                                                      ├─ storage.py    SQLite (data/trading.db, WAL mode)
                                                      ├─ agent.py      bounded self-tuning agent
                                                      ├─ notifier.py / telegram_notifier.py   Telegram + Discord
                                                      └─ dashboard     web UI on http://localhost:8766
news_agent.py (separate process)  ── Google-News RSS monitor → SL management + alerts
```

Later additions include four Cowork "skills" for operating the bot conversationally, several
scheduled guardian jobs, and a number of strategy refinements validated forward in shadow/paper
before any live use. The system was rebranded part-way through from "SLC Bot" to **"Pattern
Strategy"** (a notification header and the addition of explicit chart-pattern detection), though
the underlying SLC engine stayed the same.

> The bot is run by the **Proaxive** desk (account base currency INR), trading an FX-first
> universe of ~42 instruments. It is explicitly described in-repo as *educational software, not
> financial advice*; the standing rule throughout the chats was "run paper for at least 50 trades
> before considering live."

---

## Timeline of milestones

### 1. Strategy design and the playbook
The foundation is the SLC playbook: structure (direction), liquidity (location), confirmation
(timing), traded only where all three agree. It defines the six-item checklist, ATR-based stops
and sizing, A vs B setup grades (A = full 6/6, B = no-sweep trend taps at half risk), and the
three timeframe triplets. Everything built afterward is an attempt to encode and operate this
honestly.

### 2. Backtester and the core engine
Early sessions (originally under the path `/Users/shakeebs/Trading Strategy/`, later relocated to
`/Users/shakeebs/Claude/Projects/Price Action Strategy/`) built the Python core: `strategy.py`
(pure signal logic), `engine.py` (a paper broker plus live command queue and trade management),
`storage.py` (SQLite), and `backtest.py` to replay stored bars. The dashboard (`server.py` +
`dashboard/index.html`) exposes performance stats, a chart with entry/exit markers, history
filters, live per-symbol "engine analysis", a pairs manager, and a settings panel.

In this era the data source was **yfinance** as a fallback, the server ran on **port 8765**, and
the MT5 side used the original **`MT5DataBridge`** EA. Several practical bugs were ironed out here:
a dashboard `openMT5Chart` `ReferenceError` (function trapped inside an IIFE but called from a
global `onclick`), a duplicate `display:none; display:grid;` CSS rule that showed the offline
banner and the prices grid simultaneously, and the usual macOS onboarding friction (`python` vs
`python3`, installing `uvicorn`/`fastapi`/deps from `requirements.txt`, "Address already in use").

### 3. MT5 data bridge EA
The bridge is an MQL5 Expert Advisor (`SLCDataBridge.mq5`, with `SLCDataBridge.original.mq5` kept
as a baseline). It is timer-based (no `OnTick`), `SymbolSelect`s every watched symbol, and pushes
a JSON feed of bars + live bid/ask/spread to `server.py` every ~5 seconds while polling for live
commands. It also reports the account `login` and balance. **v2.30** added open/close trade
execution (the older v2.20 `.ex5` could only manage stop-losses); execution is gated behind an
`AllowTradeExecution` input that defaults to **false**.

A large fraction of one whole session was spent debugging the EA↔server link, which produced a
useful field guide to the MT5 WebRequest errors:
- **5203** (`ERR_WEBREQUEST_REQUEST_FAILED`) — allowed but nothing answering (server not running
  on the port, or wrong IP).
- **4014** — the target URL isn't in MT5's WebRequest allow-list (e.g. after a DHCP IP change).
- **1003 / HTTP=-1** — server down.
The server port was migrated from **8765 → 8766** during this work (which collided with a stale
process, hence "Address already in use"), and the LAN IP drifted across DHCP leases — both common
causes of the EA showing "offline" despite "loaded successfully" in the MT5 Journal. A recurring
teaching point: in the MT5 inputs dialog, **OK** applies/attaches the EA; **Save** only exports a
`.set` preset (and on macOS looks like it does nothing).

### 4. Spread handling
Spread was wired in three places: a pre-trade filter (`skip if spread > max_spread_frac × stop_dist`,
default 10%), correct-side entry fills (buy at ask, sell at bid, with risk/size recomputed from the
real fill), and correct-side exits in paper mode (longs' SL/TP tested against bid, shorts' against
ask). Live mode takes P&L straight from the broker deal record (`profit + commission + swap`). An
acknowledged gap: **paper mode models spread but not commission or swap**, so paper P&L is slightly
optimistic versus live, especially for overnight swing holds.

### 5. Telegram (and dual-channel Discord) notifier
A notification layer was added for trade-opened, TP1-hit (50% banked, stop to breakeven), trade
closed (TP2 / stop / trailing / manual, with exit price, P&L and R), news-driven SL updates, mode
switches, agent adjustments, and a weekly sanity message. Setup is two minutes via `@BotFather`
(token) and `@userinfobot` (chat id), entered in the dashboard rather than in code.

Crucially, **Discord support was built in from the start alongside Telegram** — there is one shared
notifier that pushes every message to both channels in parallel (`notifier.py` for main bot events,
`telegram_notifier.py` for news-agent alerts), gated on `discord_enabled` + `discord_webhook_url`.
Two later sessions walked through actually turning Discord on. A repeated gotcha surfaced there: the
**main bot reads notification settings live on every send, but the news-agent builds its notifier
once at startup**, so enabling Discord needs a news-agent restart; and a config edit doesn't take
effect until the running server is bounced (a stale `discord.enabled: false` boot log kept appearing
until the service was restarted). Only **paper and live** trades notify — **shadow** trades are
intentionally silent.

### 6. The self-tuning agent
`agent.py` evaluates performance every 4 hours once ≥15 trades are closed and may make only bounded
changes: raise/lower `min_grade` (A/B), nudge `atr_buffer` (±0.05 within **0.25–0.60**), nudge
`min_rr` (±0.1 within **1.8–3.0**), disable a losing symbol (≥20 trades, < −0.2R expectancy;
re-trialed after 14 days) or a losing mode (≥25 trades). It can **never** touch risk %, daily/weekly
stops, concurrency, or trading mode. Every change is logged and announced on the notifier.

### 7. News-monitoring agent
`news_agent.py` is a separate long-running process that pulls headlines from **free Google News RSS**
(no API key), scores sentiment, and manages open positions: trail the stop, move to breakeven, or
cut a losing trade early on a strong adverse score, plus market-wide headline alerts. It only manages
the bot's own positions via a **magic-number filter (`770001`)** and, in live mode, the EA still
refuses any SL change that *loosens* a stop. A notable correctness fix in this area was **equity-context
contamination** — stock-market headlines (the NZDUSD case) were bleeding into FX scoring and had to be
filtered out (`news_evaluator.py`).

### 8. Operating skills (Cowork)
A set of conversational skills was built so the bot can be driven in plain language. Four
**`slc-*`** skills were authored with the skill-creator: `slc-status` (live trades / equity / PnL),
`slc-tv-context` (TradingView snapshot, USD bias, top setups by confluence), `slc-sanity`
(parameter sweep + health + auto-tune recommendations), and `slc-backtest` (replay stored bars,
per-symbol stats). Four more had been built in earlier sessions: `trading-bot-health` (launchd
status, last bar, test suite, config validity), `trading-signal-review` (why signals were taken /
rejected, by stage and pair), `strategy-study` (shadow + closed-trade analysis, gate calibration),
and `fx-news-impact` (headline scoring vs positions). A later session corrected a mix-up where the
generic `slc-*` skills had been re-pointed at this bot with the wrong port (8766 vs 8765) and
references to non-existent scripts (`sanity_check.py`/`backtest.py`/`tv_context.py`); the originals
were reverted and the project's *own* four skills (`trading-signal-review`, `trading-bot-health`,
`fx-news-impact`, `strategy-study`, reading `state/signals.log`, launchd services,
`state/news_alerts.jsonl`, and `tools/shadow_report.py` respectively) were kept distinct. The repo
ships the four `slc-*.skill` packages.

### 9. Pattern detection and the "Pattern Strategy" rebrand
A distinct chart-pattern detector layer was added (double bottoms/tops "DB"/"DT", trendline "TL",
head-and-shoulders "HS"), and the system was rebranded to "Pattern Strategy" via a shared
notification header (`notifications.header: "Pattern Strategy"`) prepended to every message on both
channels. The test suite grew from **136 → 171 tests** across this work.

The deepest single piece of pattern work investigated *why a real CHFJPY double bottom on the user's
chart wasn't being traded*. The detector **was** firing "DB buy" repeatedly — it was being gated
downstream (waiting for a strong-bodied confirmation candle, a Choppiness Index of 71–74 above the
62 threshold, and a correlation conflict where AUDUSD at +0.72 was moving the other way). But the
user's specific shape — a **higher second low riding a rising trendline** — was being rejected. The
root cause was that a recently-tightened equal-lows check (`abs(low1 − low2) > atr × 0.25`) was
**symmetric**: it treated a higher second low (bullish, trend-aligned) the same as a lower one
(a breakdown). The fix made the tolerance **asymmetric** — counter-trend drift stays tight at
0.25·ATR, trend-aligned drift is allowed up to 1.0·ATR, with the 2.0·ATR minimum depth retained —
so ascending double bottoms / descending double tops are caught without re-admitting the earlier
false positives. Verified on synthetic data and recorded in `strategy-study/parameter_tuning.md`.

This thread also added manually-armed price levels. A design decision worth recording: a
level-triggered entry still passes through the **same** confirmation, choppiness, and correlation
gates — it does not bypass the strategy. The option of a per-level `force` flag to override the
correlation gate for a deliberate discretionary call was offered but left as a choice for the user.

### 10. Volume-confirmation gate experiment
A relative-volume gate was proposed and validated (`volume-confirmation-gate-REPORT.md`,
`volume_gate_shadow.py`, `volume-confirmation-gate.patch`). After a confirmation candle is found,
the gate computes `relvol = confirmation_bar.volume / mean(prev 20 trigger-TF bars)` and rejects the
signal if `relvol < vol_mult` (`vol_mult = 0` disables it).

A deliberate decision was made to use **tick volume already in the MT5 feed**, *not* TradingView
footprint: spot FX is decentralized, so both MT5 and TradingView "volume" on FX pairs is synthetic
tick volume and metals are index feeds with no real volume — footprint would give false confidence
on most of the watchlist, and there's no REST path to pipe Pine's `request.footprint()` into the bot
anyway. Backtests (~21-day window, 8 pairs) showed the gate and an ATR-buffer widening fix address
*different* leaks and stack: the standout intraday config was `atr_buffer 0.50 + gate 1.0×`
(62% win, PF 2.07, max DD −3.1R); swing kept `atr_buffer 0.35` with the gate halving drawdown.
Thresholds above ~1.2× over-filtered. `vol_mult` was deliberately kept **out of the auto-tune
agent's bounds** so it stays a manual decision.

Rather than apply it blind, a **scheduled shadow test** was set up: `volume_gate_shadow.py`
snapshots the live DB read-only, re-runs the strategy gate-OFF vs gate-ON, logs a JSON reading to
`volume_gate_shadow_log.jsonl`, and emits IMPLEMENT/HOLD. The user's conditional authorization was
explicit: auto-apply the patch (`vol_mult: 1.0`, `atr_buffer: 0.50`) **only if** the gate-on win
rate beats gate-off with n ≥ 30 *and* it won in both the baseline and the latest reading. One
shadow run met that bar and the patch was **applied to the (paper) config** — win rate 49.7% → 55.8%
overall (n=113) — with the agent told the user must restart the server themselves (the task is
forbidden from touching launchd services or `trading_mode`).

### 11. Dynamic spread-based stop-loss
`dynamic-spread-SL-REPORT.md` / `dynamic-spread-SL.diff` made paper trades feel the broker's *live,
changing* spread for the whole life of a trade, not just at entry. Previously the stop was only
tested at the 20-second management poll, while the EA pushes prices every 5 seconds — so up to three
of four spread snapshots, and brief blowouts between polls (e.g. a ~28-pip spike at the 21:00
rollover), were missed. The fix accumulates the worst exit-side price and max spread per symbol on
every 5s push (`_accum_px_window`), tests the stop against that worst case, tags spread-induced
stops in the exit reason, and logs them to `state/spread_trace.jsonl` (`spread-readout.patch` adds
the readout). It can only be validated forward in live paper (no stored historical spread series),
and TP detection deliberately stays on the current snapshot so a fleeting favourable spike can't
over-credit a target.

### 12. DB corruption recovery incident (2026-06-16)
The live `trading.db` became corrupted — *"rowid out of order"* in the `bars` table. `recover-db.sh`
was written to handle it safely: stop the server gracefully (not `kill -9`), back up the corrupt DB
(`trading.db.corrupt-…`, `trading.db.bad-…` — created locally during recovery; gitignored, not in
the repo), rebuild via `sqlite3 .recover` into a fresh DB, integrity-check, and swap in. The database was subsequently put
into **WAL mode** for resilience. This incident also retroactively explained an earlier scare: a
"negative swing" warning had been a **corrupt-data artifact**, not a real strategy problem — on the
clean DB, swing is positive. The lesson recorded was *don't cull pairs on tiny samples or on numbers
from a corrupt DB*.

### 13. Anti-hallucination grounding audit
Prompted by the corruption incident, `hallucination_check.py` (read-only) was added to verify the
self-tuning agent is acting on healthy data within its rules: DB integrity OK, feed fresh, the agent
changed only its allowed keys (never `risk_pct`/stops/`max_concurrent`/`trading_mode`), tuned params
in bounds, any symbol/mode disable backed by enough trades, and risk settings still matching config.
It logs a GROUNDED / WARN / FAIL verdict to `hallucination_check.jsonl` and was wired into the daily
scheduler to run **just before** the volume-gate check, so corrupt or stale data is caught before any
auto-tuning decision rides on it. Its first run came back GROUNDED. A "pattern hallucination check"
(`pattern_sanity_check.py`) was also added for the pattern detector (verifying peaks/troughs exist in
the bars, entry = pattern level, stop side, RR, direction, clarity).

### 14. Recurring jobs — daily reviews and shadow tests
Two scheduled jobs run on a daily cadence and were sampled as representatives:

- **Daily trading bot review** — a strictly read-only analyzer that writes
  `strategy-study/performance/review_YYYYMMDD.md` + `latest.md`: equity vs start, win rate, profit
  factor, average R, max drawdown, best/worst setup × pair × timeframe, and tuning *suggestions* it
  never acts on. In sampled runs it consistently declined to recommend changes on tiny samples (e.g.
  reframing a "losing DB setup" as two early stop-outs dragging down two recent +2R winners, and
  flagging a single oversized 0.79-lot USDCAD position as the only thing worth checking). The paper
  ledger was **reset on 2026-06-10** (old ledger backed up), so review numbers after that are a fresh
  run.

- **SLC volume-gate shadow check** — the scheduled implementation of milestone 10, running
  autonomously with the conditional auto-apply authorization baked into its task prompt and strict
  safety rules (never modify the live DB, never change risk/stops/concurrency/trading mode, never
  restart services).

---

## Key design decisions

- **Honesty over flattery in modelling.** Repeatedly, the simplest *honest* version was chosen over
  an impressive-looking one: tick volume instead of fake FX "footprint"; paper P&L openly flagged as
  missing commission/swap; the dynamic-spread stop limited to 5-second granularity with the tick-level
  upgrade left as a clearly-labelled draft.
- **Validate forward before trusting.** Backtest wins were treated as hypotheses. Changes (the volume
  gate especially) had to prove themselves in shadow/paper over a real sample, with an explicit
  anti-noise rule (consistent across two readings, n ≥ 30), before being applied — and even then only
  to paper config, never flipped live by an agent.
- **A tightly-bounded autonomous agent.** The self-tuning agent and every scheduled task operate
  inside hard rails: they may nudge a small whitelist of parameters but may **never** touch risk %,
  stops, concurrency, or `trading_mode`, and may **never** restart services or switch to live. Those
  remain the human's explicit action.
- **Secrets live in the database, not in source.** `config.yaml` ships Telegram/Discord credentials as
  empty strings; the dashboard writes the real values into the SQLite settings table at runtime, and
  the notifiers read them live on each send. This keeps the repo shippable without leaking creds.
- **Dual-channel notifications by construction.** One shared notifier feeds Telegram and Discord
  identically rather than bolting Discord on later, so enabling a channel is configuration, not code.
- **Asymmetric, conceptually-grounded thresholds.** The double-bottom tolerance fix is the model:
  rather than tweak a magic number, the *reason* a higher low differs from a lower low (trend-aligned
  vs breakdown) was encoded directly.
- **Shadow trades are silent and never notify or get tuned on as if live.** Paper/live and shadow are
  kept cleanly separate throughout (notifications, P&L, the agent's trade counts).

---

## Open items / TODOs / drafts

- **EA tick-accurate spread reporting (`EA-tick-spread-DRAFT.md`)** — an *untested, uncompiled* MQL5
  draft to make the dynamic-spread stop tick-accurate instead of 5-second, by having the EA report the
  worst bid / best ask / max spread seen on **every tick** since the last push (via `CopyTicks`, since
  the timer-based EA has no `OnTick`). Includes the matching Python change to `_accum_px_window`, which
  is written to fall back gracefully if the new fields aren't present. Needs compiling in MetaEditor
  and verifying the first feed payloads carry `min_bid`/`max_ask`/`max_spread`/`point` before being
  relied on.
- **Paper commission + swap modelling** — offered but not built; would close the known gap between
  paper and live P&L for commission accounts and overnight holds.
- **Per-level `force` flag** — offered, to let a manually-armed level override the correlation gate as
  a deliberate discretionary call; left as the user's decision.
- **Exit/TP2 trailing improvement** — a review observed swing winners average +1.16R but only capture
  +0.16R, and only 1 of 10 trades that reached +2R actually booked ≥2R, suggesting the structure/TP2
  trailing gives back gains. Flagged as a tunable, not yet pursued.
- **Symbol culling deferred** — per-symbol numbers are too thin (n≈1–5) to bench reliably; revisit once
  there are ~15+ closed trades per pair. (And never act on numbers from a corrupt DB.)
- **Inconsistencies — mostly reconciled (docs pass, 2026-06-24).** The user-facing docs were swept and
  aligned to `config.yaml` (port **8766**, EA `SLCDataBridge`, min RR **2.0**, 8 enabled pairs); the
  stale 8765 / `MT5DataBridge` references in `README.md`, `trading-bot/README.md` and `server.py` were
  fixed, and the TradingView webhook + strategies registry were documented. A follow-up code/config
  pass (2026-06-24) then resolved:
    - **EA version strings** — `SLCDataBridge.mq5` now reports **2.30** consistently: the startup `Print`
      and the JSON feed `terminal.version` were updated from `v2.20` / `2.00` to `2.30`, matching the
      `#property version`, and the lone `v2.31` `PushBars` comment was de-versioned.
    - **Legacy autostart scripts removed** — `install_autostart.sh` and `watchdog.sh` (legacy
      `com.tradingbot.*` / port 8765, byte-identical to the `legacy/.../tools/` copies) were deleted from
      the repo root; `watchdog-install.sh` (`com.slc.*` / 8766) is the only autostart path now.
    - **`config.example.yaml`** — now a documented template (copy-to-`config.yaml` header, secrets-blank
      guidance) rather than an exact copy.
  Still open (harmless): the other root analysis scripts (`reset_ledger.py`, `shadow_report.py`,
  `shadow_report_corrected.py`, `spread_report.py`, `pattern_sanity_check.py`) are copies of
  `legacy/.../tools/*` whose docstrings assume a `tools/` subdir, so run-from-root path handling is off.
  The account-size brief (₹10,000 INR) also differs from the ledger's 100,000 base. Treat the live
  `config.yaml` as authoritative.

---

## File map (selected)

| Path | Role |
|---|---|
| `SLC-Price-Action-Playbook.md` | the hand-written strategy this all implements |
| `trading-bot/strategy.py` | pure SLC + pattern logic |
| `trading-bot/engine.py` | signal execution, paper broker, live commands, spread-window logic |
| `trading-bot/server.py` | Flask app, EA endpoints, starts engine/agent/notifier threads |
| `trading-bot/agent.py` | bounded self-tuning |
| `trading-bot/notifier.py`, `telegram_notifier.py` | dual-channel Telegram + Discord |
| `trading-bot/news_agent.py`, `news_evaluator.py` | RSS news monitor + SL management |
| `trading-bot/storage.py` | SQLite (WAL), runtime settings incl. credentials |
| `trading-bot/backtest.py`, `sanity_check.py` | replay + parameter sweep |
| `trading-bot/config.yaml` | startup defaults (DB values win after first run) |
| `SLCDataBridge.mq5` / `.original.mq5` | MT5 data-bridge EA (v2.30) and baseline |
| `volume-confirmation-gate-REPORT.md` / `.patch`, `volume_gate_shadow.py` | volume gate experiment |
| `dynamic-spread-SL-REPORT.md` / `.diff`, `spread-readout.patch` | dynamic spread stop |
| `EA-tick-spread-DRAFT.md` | unfinished tick-accurate spread EA change |
| `recover-db.sh` | DB corruption recovery |
| `hallucination_check.py` / `.jsonl`, `pattern_sanity_check.py` | grounding audits |
| `slc-status.skill`, `slc-tv-context.skill`, `slc-sanity.skill`, `slc-backtest.skill` | operating skills |
