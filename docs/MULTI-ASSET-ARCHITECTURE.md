# Multi-Asset Trading System — Phase 1: Inventory & Architecture Proposal

**Status:** PROPOSAL — awaiting sign-off. Nothing in the running bots is modified by this phase.
**Branch:** `claude/multi-asset-trading-system-8xtjby` · **Date:** 2026-07-05

This document is the Phase 1 deliverable: (1) an inventory of the two existing bots as they
exist **in this repository**, (2) the proposed shared architecture and data-schema approach for
extending them into a multi-asset (forex / crypto / indices / metals) system, and (3) the open
questions that block Phase 2.

---

## 0. Method and environment reality

This analysis was produced in a clean clone of `mathubabulu/slc-trading-bot`, not on the Mac
that runs the bots. Consequences, stated up front:

- `lsof -i :8766/:8765`, `launchctl list`, and `ps` find nothing here — but both systems are
  fully present as source: the SLC bot at `trading-bot/` (Flask, port 8766, SQLite) and the
  clarity-gated pattern bot at `legacy/pattern-strategy-fastapi/` (FastAPI, port 8765, launchd
  plists at `legacy/pattern-strategy-fastapi/trading-bot/tools/launchd/`, JSON ledger).
- Runtime state (`trading-bot/data/trading.db`, `state/`) is gitignored, so **no claim in this
  document is based on your actual trade data**. Everything data-dependent (e.g. current Grade A
  expectancy) is treated as an input you'll re-verify on the Mac.
- Several artifacts you referenced are **not in this repo** and were treated as existing only on
  your Mac: `db_health.py`, `verify_live_trade.py`, the correlation audit that found 30/47
  correlated losers, the drawdown-forensics lessons log, and the regime/session performance
  studies. (`hallucination_check.py` *is* in the repo.) → Sign-off question Q4.

Every load-bearing claim below carries a `file:line` reference.

---

## 1. Inventory — what exists

### 1.1 SLC bot (canonical, port 8766) — `trading-bot/`

One Python process: Flask server + two daemon threads (`engine.engine_loop` 20s poll,
`agent.agent_loop`). MT5 is the **only** market connection, via the `SLCDataBridge` EA
(v2.30, magic 770001) speaking a fixed HTTP contract (`server.py:72`):

- EA → server: `POST /api/mt5_feed` (~5s: account, open/closed positions, prices with
  `tick_value`/`tick_size`/spread, optional tick-accurate `min_bid/max_ask/max_spread`),
  `POST /api/mt5_bars` (closed OHLCV bars, 6 TFs).
- Server → EA: SQLite-backed command queue — `GET /api/commands/next`, `POST
  /api/commands/ack/<id>`; commands are `open_trade | close_trade | trail_sl | move_sl_be`.
  Live execution is entirely "enqueue command, EA executes" — there is no MetaTrader5 Python
  dependency anywhere, which is why the Mac can run it at all.

Signal flow: `engine_loop` → `strategies.generate_all` (plugin registry,
`strategies/__init__.py`) → `strategy.analyze` (SLC checklist: HTF bias → regime filter →
POI → sweep (A) / trend-tap (B) → LTF confirmation → volume gate → spread-adjusted RR) →
`engine.try_execute` (`engine.py:272`) — **the single choke point for every rail**: mode gate,
max_concurrent, one-per-symbol, same-direction cap, daily −2% / weekly −5% stops, spread/drift/
RR-at-fill re-checks, broker-exact sizing, min-lot risk-inflation cap. Paper fills simulate in
the DB; live additionally enqueues an EA command. Valid-but-filtered setups become silent
`mode='shadow'` trades — free counterfactual sample collection.

Persistence: one SQLite file, 7 tables (`storage.py:19-67`): `bars, trades, signals, equity,
agent_log, settings, commands`. All runtime params live in `settings` (JSON values); config.yaml
seeds once, DB wins after.

Already multi-asset-ready pieces (verified):
- **Strategy plugin registry** (`strategies/__init__.py`) — built exactly for "isolated
  strategies behind shared global rails"; SLC is plugin #1; tested.
- **Sizing has no pip math** — `calc_lots` uses per-symbol `tick_value/tick_size` pushed by the
  EA (`engine.py:167-182`); works identically for metals/crypto/indices *if* volume constraints
  are known (they aren't — see gaps).
- **External-signal ingestion** (`engine.ingest_external_signal`, `engine.py:412-488`, used by
  the TradingView webhook): the proven "candidate in → shared rails decide" pattern, forced
  half-risk grade B, never a raw order.
- The default watchlist already includes XAUUSD/XAGUSD/BTCUSD/ETHUSD as MT5 CFDs
  (`config.yaml:36-44`) — metals and CFD-crypto already flow through this path in paper/shadow.

Satellites: bounded self-tuning agent (`agent.py`, §2.3 below); news agent (separate process,
Google News RSS + keyword lexicon, SL-management only via server-whitelisted commands, every
decision including "hold" logged to `state/news_decisions.jsonl`); dual Telegram/Discord
notifiers; TV context annotator (informational only).

Ops/validation layer: `backtest.py` (replays SQLite bars through the *live* strategy code,
pessimistic SL-first fills), `sanity_check.py` (parameter grid + a second bounded auto-tuner),
`hallucination_check.py` (the data-trust gate: read-only, 7 checks — DB integrity, bar
freshness, agent-change whitelist, bounds, sample-justified disables, config drift, ground-truth
recompute; verdict GROUNDED/WARN/FAIL, exit 2 on FAIL), `volume_gate_shadow.py` (off-path
change evaluation against a snapshot), `recover-db.sh`, watchdog.

### 1.2 Clarity-gated pattern bot (port 8765) — `legacy/pattern-strategy-fastapi/`

Single asyncio FastAPI process; same EA-push data model (`MT5DataBridge`); **no SQLite** —
flat JSON/JSONL under `state/` (paper ledger, per-ticket trade journal, signals.log, shadow
files). 8 pattern detectors (DT/DB live; H&S, inverse, triples, rectangle, trendline built but
config-disabled), clarity-scored 0–100, gated through an ordered gauntlet: dedupe → cooldown →
HTF-trend → dead-market ATR percentile → clarity → candle/momentum confirmation → choppiness →
**correlation** → session → news blackout → risk/sizing.

Its four crown jewels for the new system (verified in code):

1. **The adapter seam already exists**: `marketdata/base.py` `DataSource` protocol
   (`fetch_history/fetch_latest` → normalized `Bar`) and `execution/base.py` `OrderRouter`
   protocol (`submit / open_positions / on_bar / flatten_all / equity / closed_trades`) with
   `OrderRequest/OrderFill/PositionUpdate` dataclasses. Caveat: `modify_sl`, `close_at_market`,
   `reversal_exit`, `reset` are duck-typed extras that must be lifted into the interface.
2. **The decision-audit stack**: every rejection flows through one `_reject` funnel
   (`strategy/engine.py:451-467`) emitting structured `{stage, failed_check,
   checks:[{name,passed,value,threshold,detail}]}` → `state/signals.log` JSONL → `/api/funnel`
   aggregation → **ShadowTracker** (`strategy/shadow.py`) which follows every rejected signal to
   its hypothetical TP/SL so each gate's opportunity cost is *measured*. This is the best
   "decisions NOT to trade are logged and priced" implementation in the repo.
3. **Runtime invariants** (`execution/paper.py`): time-ordering guard (no same-bar look-ahead
   exits) and the sizing invariant `|pnl| ≈ |R| × risked_money ±10%` with CRITICAL Telegram page
   on violation — this caught the NAS100 index-oversizing bug (see
   `strategy-study/STRATEGY_ANALYSIS_WAYFORWARD.md`) and is exactly the class of unit bug a
   multi-asset system multiplies.
4. **Real correlation gating** (`strategy/correlation.py`): Pearson on returns (|r|≥0.70,
   100-bar lookback), direction-conflict blocking, correlated-open blocking,
   `same_directional_bet` logic that already understands "long EURUSD + short USDCHF = same
   USD bet".

Its history also documents the reference *process*: scope-shrink until samples mean something,
sizing invariant before tuning, shadow-priced gates, evidence-cited parameter changes with
reversion when the evidence proved contaminated (`config.example.yaml` body-ratio saga,
`docs/PROJECT_CONTEXT_HANDOFF.md` §6).

Live execution was never implemented there (`execution/mt5_router.py` raises
`NotImplementedError`; `/api/mode live` returns 409) — the SLC EA-command-queue approach
superseded it.

### 1.3 Honest reality check — the "research/risk layer" vs. this repo

| Capability (task brief) | State in this repo |
|---|---|
| Data-trust gate | ✅ `hallucination_check.py` (SLC) + `check_feed.py`/`validate_trades.py`/`pattern_sanity_check.py` (legacy) |
| Promotion gate | ⚠️ **Prose only** (CLAUDE.md, SKILL.md). No code computes ≥50-trade/positive-expectancy status anywhere. |
| Correlation/exposure auditing | ⚠️ Legacy has the pairwise gate; the *audit* (30/47 finding) and any currency-exposure snapshot code are **not in the repo** (the legacy dashboard has a placeholder panel for it). SLC's `max_correlated` is only a same-direction position count (`engine.py:316-319`). |
| Drawdown forensics + lessons log | ❌ Not in repo. |
| Regime/session performance studies | ⚠️ Session labeling exists (`legacy .../session.py:93-121`); no study code. Legacy `performance_review.py` computes per-setup/pair/TF tables. |
| `db_health.py`, `verify_live_trade.py` | ❌ Not in repo (Mac-only?). |
| Decision audit incl. no-trades | ⚠️ Legacy: excellent (signals.log + shadow). SLC: signals table logs skipped *signals*, but pre-signal no-trade reasoning lives only in memory (`_last_info`) and dies on restart (`engine.py` gap). |

Implication: the new system doesn't just "generalize" the research layer — several pieces must
be **built as importable library code for the first time**, using the legacy bot's patterns as
the template. Q4 asks whether newer versions exist on your Mac that should be pushed here first.

---

## 2. The three lessons, mapped to code

### Lesson 1 — Correlated exposure across asset classes
Current state: SLC counts same-direction open positions (`max_correlated=3`, direction-only, no
notion of *what* is correlated). Legacy computes real pairwise return-correlation but (a) only
gates pair-vs-pair, (b) same-timeframe co-firing dedupe only, (c) has **no aggregate exposure
ledger** — three USD-quote longs whose pairwise trailing r dips below 0.70 all pass. The
playbook already states the target semantics: *"EURUSD+GBPUSD+Gold long = ~one USD-short trade;
count it as such"* (`SLC-Price-Action-Playbook.md:128`). Nothing enforces it.
→ Design answer: **structural factor-bucket exposure ledger** (§3.5), with the empirical
Pearson gate retained as a second, independent layer.

### Lesson 2 — Quality labels don't transfer
Grades are strategy-local claims, not risk-globals: SLC's A = sweep-based (currently the losing
class on your data), legacy's A+/B/C = clarity score. The architecture therefore treats a grade
as an *input feature* whose risk multiplier is **derived per (strategy × asset-class) from that
cell's own closed-trade data** and starts conservative (B-level sizing) for every new cell until
its own sample exists. No grade ever maps to "safer" by assumption; promotion gates are per
cell (§3.9).

### Lesson 3 — No silent self-tuning
Verified mechanics of the incident class: `agent.py:99-102` flips the **global** `min_grade`
B→A. It *is* logged (`agent_log` row, always) — but the only push-channel is gated on a
`notify_agent` setting that is **never seeded** (`server.py` seeding list; `agent.py:74`), the
dashboard shows the new value in Settings as if user-set, and there is no "agent-modified"
marker. Worse: `_change()` has **no whitelist guard** — the MAY-NEVER list is call-site
discipline only (`agent.py:70-77`); `sanity_check.py --apply` is a **second** independent tuner
writing to the same log; and if you flip `min_grade` back while B-expectancy is still negative,
the agent silently re-raises it next eval (a quiet tug-of-war). `hallucination_check` audits
changes by regex-parsing free-text `detail` strings — a reworded message defeats the audit.
→ Design answer: one `set_param()` choke point that enforces whitelist + bounds **at the write
layer**, structured `param_changes` rows (old/new/origin/trigger-data), a dashboard
change-feed with unacknowledged-badge, and human-flip precedence (§3.7).

---

## 3. Proposed architecture

### 3.1 Core decision: one shared pipeline, evolved in place

**Shared pipeline with per-asset-class configuration and per-strategy detectors** — not separate
systems per asset class, and not a greenfield rewrite. Reasons:

- The canonical bot already has the two hardest shared pieces working: the single rails
  choke point (`engine.try_execute`) and the strategy plugin registry. CLAUDE.md's
  multi-strategy roadmap says exactly this ("isolated modules behind the shared engine and the
  GLOBAL risk rails — never fork the rails per strategy").
- Every constraint you listed (global drawdown halts, cross-class exposure caps, one audit
  trail, one promotion-gate view) is *only* enforceable if there is one place decisions pass
  through. Forked per-class stacks would re-create the current situation — two bots that can't
  see each other's exposure.
- The things that genuinely differ per asset class (sessions, day boundaries, news entities,
  volume semantics, cost models) are **configuration and adapters**, not pipeline topology.
  §4 flags where this claim strains and what we do about it.

Pipeline (one pass per candidate, every stage appends to the same decision record):

```
DataSource adapters ──► instrument registry ──► strategy plugins (per-class detectors)
                                                   │  candidate {signal, features}
                                                   ▼
                                        grade (strategy-local label)
                                                   ▼
                              regime & news adjustment (per-class calendar/model)
                                                   ▼
                              RISK GATE (global, code-enforced, single choke point)
                                 caps · drawdown halts · factor-bucket exposure ·
                                 correlation · data-trust/staleness · cost gate
                                                   ▼
                                router: paper sim  |  live adapter (per venue)
                                                   ▼
                          decision + trade + audit records (SQLite, append-only)
```

### 3.2 What we take from where

| Component | Source | Action |
|---|---|---|
| Rails choke point, shadow trades, spread-window stop eval | SLC `engine.py` | keep, extend |
| Strategy plugin registry | SLC `strategies/__init__.py` | keep; add `asset_classes` to plugin contract |
| `DataSource` / `OrderRouter` protocols + dataclasses | legacy `marketdata/base.py`, `execution/base.py` | port into `trading-bot/`, formalize duck-typed extras |
| Decision funnel + structured CheckResult + ShadowTracker | legacy `strategy/engine.py`, `shadow.py` | port; persist to SQLite instead of JSONL |
| Sizing + time invariants | legacy `paper.py` | port into the shared paper router |
| Pearson correlation module | legacy `correlation.py` | port; runs beside the new factor ledger |
| Bounded-tuning pattern (evidence gates, one-step, bounds) | SLC `agent.py` | keep pattern; rebuild write layer (§3.7) |
| Data-trust gate | `hallucination_check.py` | generalize: per-class freshness, structured change audit, WARN exit code |
| EA command queue + tighten-only SL stack | SLC server/EA | keep as the MT5 execution adapter |
| News decision logging, command whitelist | SLC news agent | keep; add calendar/blackout component (§3.6) |

### 3.3 Broker/data adapter layer

`DataSource` and `OrderRouter` become the only ways markets are touched:

- **MT5 adapter** (forex, metals, indices — *pending Q1 confirmation of what Vantage enables*):
  the existing EA bridge *is* the adapter. Data continues to be EA-push; execution remains the
  command queue. Two hardening changes required before any new class trades live through it:
  the EA must additionally push `volume_min/volume_step/volume_max` and contract size per
  symbol (sizing currently assumes 0.01-lot granularity — `engine.py:181`), and the command
  queue must gain an `expires_at` honored server-side plus a `sent` state so an unacked command
  can't be re-served into a duplicate live order (`storage.py:256-263`).
- **Crypto adapter** (native exchange API — *exchange choice is Q2, not assumed*): market data
  via public REST/websocket (works credential-less for paper), execution via authenticated API
  with keys in macOS Keychain / env only. Spot first; perps only if you later choose them
  (funding-rate cost model is extra work, §4).
- The paper router is **venue-independent** (one simulator consuming normalized bars/quotes from
  any adapter) so paper trading for MT5-routed instruments runs even when the VPS is down.

### 3.4 Instrument registry (new, load-bearing)

Today, symbol knowledge is scattered across at least five inconsistent places (dashboard
`pipSize()`, `news_evaluator._parse_symbol`, `news_agent` slicing that mangles `US500` into
base `US5`/quote `00`, `tv_context.SYMBOL_MAP`, legacy `_currencies_for`). One `instruments`
table becomes the single source of truth: asset class, venue + venue symbol, tick size/value,
volume constraints, display precision, session calendar id, day/week boundary rule, news
entities, **factor exposures** (§3.5), enabled/watch state. Everything (strategies, news,
risk, dashboard) reads it. `tv_context.SYMBOL_MAP` (45 symbols across all four classes) seeds it.

### 3.5 Risk rails — global, code-enforced

All enforced in the one choke point; all limits are DB-backed but **bounded by code constants**
that no runtime setting (and no agent) can exceed:

1. **Per-trade risk cap** (per class, ≤ global ceiling; grade multipliers per §2 lesson 2).
2. **Daily/weekly drawdown halts** — kept, plus two fixes: computed per venue-day/week rule from
   the instrument registry (UTC-midnight is wrong for broker-time FX days and arbitrary for
   crypto — `engine.py:227-230`), and *including open-position drawdown*, not just realized
   (both bots currently count realized only).
3. **Concurrency caps**: global, per asset class, per strategy, one-per-symbol.
4. **Factor-bucket exposure ledger** (lesson 1): each instrument declares signed factor
   exposures, e.g. `EURUSD buy = {EUR:+1, USD:−1}`, `XAUUSD buy = {USD:−1, GOLD:+1}`,
   `DAX buy = {RISK:+1, EUR:+1}`, `BTCUSD buy = {CRYPTO:+1, USD:−1, RISK:+1}`. The gate sums
   **open risk (R-weighted) per bucket** across ALL asset classes and blocks/downsizes above the
   cap. Long EUR/USD + long DAX + long Gold now visibly stack in the USD and RISK buckets.
   The empirical Pearson gate (ported) remains as an independent second check; disagreement
   between the two is itself logged.
5. **Consecutive-loss governor**: playbook rule 3 (halve risk after 3 straight losses until 2
   wins) — currently implemented **nowhere**; becomes code, with persisted counters.
6. **Fail-safe on uncertainty**: per-instrument staleness from the registry's calendar (fixes
   the global 12h freshness constant that false-warns over FX weekends and is far too lax for
   crypto), venue-disconnect and API-error states halt *new entries* for that venue's
   instruments by default; DB-integrity doubt halts everything (and `storage.query` returning
   `[]` during corruption recovery — `storage.py:172` — is fixed to raise, because "no rows" is
   how `loss_limits_hit` sees a clean slate).
7. **Risk state persisted** (legacy's restart-resets-the-daily-cap gap, `RiskState` rebuilt
   fresh at `server.py:541-550`, is not carried forward).

### 3.6 Regime & news layer (Phase 3 detail, architecture fixed now)

- **Regime**: keep the playbook's ATR(14)/ATR(100) ladder as the shared regime primitive
  (already implemented in `strategy.py`), computed per instrument; regime and session labels
  are stamped on **every decision record** so regime/session studies are queries, not projects.
- **Sessions**: per-class calendar from the registry — forex/metals: current session model
  (`legacy session.py` logic, plus DST handling); indices: exchange hours + holidays; crypto:
  always-open, with *time-of-day/weekend risk factors* instead of open/closed gating (§4).
- **News**: two explicit mechanisms, both per-asset-class and both logged with the exact
  headline/event behind every action: (a) **scheduled-event blackouts** from an economic
  calendar — a new component; nothing like it exists today (the legacy ForexFactory scraper is
  the closest precedent); entry blocking N min around events matching the instrument's news
  entities; (b) **breaking-headline response** — the existing news agent generalized: lexicon
  and entity matching driven by the registry (crypto tokens and index/macro drivers are not
  3-letter currency codes), BE-buffer expressed in ATR/ticks not pips, and the
  `currencies=[] matches everything` bug (`legacy news.py:120`) fixed by design.

### 3.7 Bounded adaptation — no silent tuning (lesson 3)

- One **`set_param()` write layer** used by *every* writer (agent, sanity auto-tune, dashboard,
  human): enforces per-key whitelist + bounds at write time; anything else raises.
- Structured **`param_changes`** table: `{t, origin, strategy, asset_class, key, old, new,
  bounds, trigger_data (the stats that justified it), ack}`. The data-trust gate audits this
  table directly instead of regex-parsing prose.
- **Surfacing**: dashboard change-feed with unacknowledged badge; parameters whose current value
  came from an agent are visibly marked in Settings; agent changes always notify (not gated on
  an unseeded flag).
- **Human precedence**: a human-set value pins the parameter (agent may not counter-flip it
  without a new, larger evidence sample — ends the tug-of-war).
- Per-strategy/per-class parameter namespaces (`slc.fx.min_rr`, not global `min_rr`) so one
  strategy's tuning can never touch another — required by CLAUDE.md before strategy #2 ships.

### 3.8 Audit trail — every decision, including "no"

The legacy funnel model, persisted: a `decisions` table where every evaluated candidate — and
every stand-aside (stale data, regime shock, session closed, news blackout) — records stage,
per-check `{name, passed, value, threshold}`, feature values, pattern/grade, regime, session,
news context, and outcome (executed/skipped/shadow). SLC's current in-memory-only pre-signal
reasoning (`_last_info`) moves into it. ShadowTracker prices rejected candidates so every gate's
cost stays measurable. JSONL mirrors remain for `tail -f` ergonomics.

### 3.9 Promotion gate — computed, per strategy × asset class, never automatic

New `promotion` module computes per cell: closed-trade count, expectancy (R), profit factor,
max DD, **data-trust verdict on the underlying data** (gate is invalid unless
`hallucination_check` GREEN), rails-verified checklist (kill switches / BE-at-TP1 / stop
management observed working in paper). Going live additionally requires a **manual sign-off row**
(`promotion_signoffs`: who/when/sample snapshot) — the dashboard shows gate status but the
final step is you. Defaults: ≥50 closed paper trades and positive expectancy (CLAUDE.md),
per cell; live starts at minimum size. The live/paper switch itself keeps the double gate:
server-side confirmed two-step (§3.11) AND the EA's `AllowTradeExecution` master switch
(`SLCDataBridge.mq5:73`, default false, with `MaxLotsPerTrade`/`MaxOpenPositions` backstops)
for MT5 venues; an equivalent env-level arming flag for the crypto adapter.

### 3.10 Host topology

```
┌────────────────── Mac (always on) ─────────────────────┐
│ engine + strategies + risk gate + paper router          │
│ SQLite (source of truth) · dashboard (localhost)        │
│ news/calendar agent · research/backtests                │
│ crypto adapter: public WS/REST data; LIVE crypto        │
│ orders natively from here (keys in Keychain)            │
└──────────────┬──────────────────────────────────────────┘
               │ existing EA HTTP contract (feed push / command poll)
               │ over Tailscale/VPN — never the open internet
┌──────────────▼──────────────────┐
│ Windows VPS (live MT5 only)     │
│ MT5 terminal + SLCDataBridge EA │   ← the EA already IS the thin execution client
│ AllowTradeExecution gate        │
└─────────────────────────────────┘
```

The Windows VPS is needed **only when MT5-routed instruments go live** (paper for those runs
entirely on the Mac from EA data pushed by any MT5 terminal, including your current setup). The
"kept in sync via the database" requirement is already how the system works: the command queue
and feed mirror live in SQLite. No MetaTrader5 Python package anywhere.

### 3.11 Dashboard (Phase 4 detail, architecture fixed now)

New **FastAPI** app in this repo, importing the analysis library directly (the library is built
in Phases 2–3 as importable functions precisely so the dashboard reimplements nothing). Views:
system/bot health (per venue, staleness, halts), per-asset-class performance & open positions,
the four study views as visuals (regime/session performance, correlation/exposure snapshot with
factor buckets, drawdown forensics, promotion-gate status), filterable decision/audit log
(date/instrument/strategy/stage). Controls: per-strategy/asset enable, risk params **within
pre-approved bounds only**, and the live switch. Security fixes over the current state (which
violates your constraints today): binds to `127.0.0.1` (SLC currently `0.0.0.0`,
`config.yaml:9`); live toggle is a server-side two-step (request → distinct confirm token →
apply, isolated router module) instead of client-side `confirm()` (`index.html:512-518`);
control endpoints require a local auth token; `GET /api/state` stops returning credentials (it
currently exposes the Telegram token to any LAN client); secrets in Keychain/env, with the DB
retained only for non-secret settings.

---

## 4. Where the shared pipeline genuinely strains (flagged, per your ask)

1. **Crypto has no sessions.** We do NOT force the forex session model onto it: for crypto the
   calendar contributes *risk factors and study labels* (hour-of-day, weekend-thinness — the
   playbook already prescribes half-risk/skip weekends, §9), never an open/closed gate. Session
   *studies* bucket by UTC hour/weekend rather than London/NY.
2. **"Day" and "week" are venue-relative.** Daily/weekly loss windows, PDH/PDL liquidity
   references, and equity buckets each need the registry's day-boundary rule (broker day for
   MT5, UTC day for crypto). SLC's UTC-midnight kill-switch window is wrong today for FX too.
3. **Volume semantics differ.** MT5 FX volume is tick count (a proxy); exchange crypto volume is
   real. The SLC volume-confirmation gate and legacy volume-profile carry a per-class validity
   flag; volume-derived features don't transfer across that line.
4. **Cost models differ.** Spread-only paper fills overstate expectancy everywhere (known open
   item); crypto adds taker fees and (if perps) funding — the paper router gets a per-venue cost
   model, and promotion gates evaluate net-of-cost expectancy.
5. **News is not one model.** Currency-sentiment scoring does not describe BTC or NAS100;
   per-class entity models are separate configs behind one interface (§3.6).
6. **Liquidity references shift.** SLC's PDH/PDL pools become registry-driven (crypto: weekly/
   monthly opens and prior weekly H/L, playbook §9) — a strategy-level per-class config, not a
   fork of the strategy.
7. **What stays truly global, always:** account-level drawdown halts, factor-bucket exposure
   caps, concurrency ceilings, the audit trail, the promotion gate, and the paper-first rule.

## 5. Data-schema approach

Stay on **one SQLite (WAL) database** — it is the current source of truth, the sync mechanism
with the EA, and nothing in scope needs more. Additive migrations, no rewrites of existing
tables (the running bot keeps working):

```sql
-- migration 001 (Phase 2)
ALTER TABLE trades  ADD COLUMN strategy TEXT;         -- promotion gate needs attribution
ALTER TABLE trades  ADD COLUMN asset_class TEXT;
ALTER TABLE signals ADD COLUMN strategy TEXT;
CREATE TABLE instruments (
  symbol TEXT PRIMARY KEY, asset_class TEXT, venue TEXT, venue_symbol TEXT,
  tick_size REAL, tick_value REAL, volume_min REAL, volume_step REAL, volume_max REAL,
  contract_size REAL, display_digits INTEGER, session_calendar TEXT, day_boundary TEXT,
  factor_exposures TEXT,      -- JSON {"USD":-1,"EUR":1}
  news_entities TEXT,         -- JSON ["EUR","USD"] / ["BTC","crypto"] / ["USD","tech"]
  enabled INTEGER, watch INTEGER);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY, t INTEGER, strategy TEXT, symbol TEXT, tf TEXT, stage TEXT,
  action TEXT,                -- executed|skipped|shadow|stand_aside
  grade TEXT, checks TEXT, features TEXT, regime TEXT, session TEXT, news_ctx TEXT,
  reason TEXT, signal_id INTEGER, trade_id INTEGER);
CREATE TABLE param_changes (
  id INTEGER PRIMARY KEY, t INTEGER, origin TEXT,     -- agent|sanity|human|system
  strategy TEXT, asset_class TEXT, key TEXT, old TEXT, new TEXT,
  bounds TEXT, trigger_data TEXT, ack INTEGER DEFAULT 0);
CREATE TABLE risk_state (
  scope TEXT PRIMARY KEY,     -- global|<asset_class>|<venue>
  day TEXT, week TEXT, realized_day REAL, realized_week REAL,
  fills_day INTEGER, consec_losses INTEGER, halted INTEGER, halt_reason TEXT);
-- migration 002 (Phase 3): news_events(t, kind sched|headline, source, entities, impact,
--   payload, hash) ; news_actions(event_id, action, symbol/ticket, reason)
-- migration 003 (Phase 5): promotion_signoffs(strategy, asset_class, t, sample_n,
--   expectancy_r, data_trust, signed_by, note)
```

Existing `settings` gains namespaced keys (§3.7); `agent_log` remains for back-compat reads but
new writers use `param_changes`. Legacy JSON shapes (journal/shadow) are already superseded by
SQLite equivalents in the canonical build (`mode='shadow'` trades) — we extend those, we do not
run two persistence regimes.

## 6. Constraint compliance map (your 11 non-negotiables)

| # | Constraint | Where enforced | Existing violations to fix on the way |
|---|---|---|---|
| 1 | Paper-first + gated promotion | §3.9 registry `promotion_state`, sign-off rows | gate is currently prose-only |
| 2 | Hard ceilings (risk/DD/concurrency/exposure) | §3.5 rails, code-bounded | no cross-class exposure model anywhere today; DD ignores open P&L |
| 3 | No silent tuning | §3.7 write layer + change feed | `_change()` unguarded; `notify_agent` never seeded; 2 tuners; regex audit |
| 4 | Fail safe on uncertainty | §3.5(6) staleness/disconnect/integrity halts | `query()`→`[]` on corrupt DB; 12h global freshness; command re-serve dup risk |
| 5 | Explicit logged news handling | §3.6 calendar + headline logs | no calendar exists; index/crypto entity bugs |
| 6 | Full audit incl. no-trades | §3.8 decisions table | SLC pre-signal reasoning is in-memory only |
| 7 | Two-step live toggle, code-isolated | §3.11 | live confirm is client-side JS only |
| 8 | Secrets in env/Keychain | §3.3/§3.11 | `/api/state` leaks Telegram token; secrets in DB today (better than legacy's yaml, still shown to LAN) |
| 9 | Localhost/auth dashboard | §3.11 | both bots bind 0.0.0.0, no auth, CORS `*` (legacy) |
| 10 | No fabricated results | reporting only from stored/live runs; backtests replay live code (`backtest.py` already does this) | paper fills lack costs → optimistic; fixed via §4(4) |
| 11 | Tests for risk/circuit-breaker code | Phase 2+ test suite; legacy invariants ported | SLC has 2 test files; rails untested |

## 7. Phase 2 recommendation

**Start with crypto via a native exchange API** (exchange = your call, Q2):

- It is the strongest test of the adapter seam — a second, genuinely different venue prevents
  the MT5 shape from ossifying into the "abstraction". Indices via MT5 would mostly re-exercise
  the existing EA path and teach us little.
- Paper trading needs **no credentials**: public market data (WS/REST) lets us run the full
  pipeline end-to-end on the Mac against live data immediately, satisfying "verified against
  real stored or live data" from day one.
- It forces the hard generalizations (24/7 calendar, UTC day boundary, real volume, fee model)
  early, while the stakes are paper.
- Meanwhile indices (Phase 2b or 3) only need registry entries + session calendars once Q1
  confirms what Vantage enables — cheap to add after the seam exists.

Phase 2 scope: port/formalize the adapter protocols; instrument registry + migration 001;
crypto `DataSource` (paper `OrderRouter` shared); decisions table + funnel; `set_param` write
layer; risk-rail extensions (factor buckets, per-class staleness); **pytest suite for every
rail and circuit breaker** (constraint 11); SLC keeps trading untouched throughout.

## 8. Verified vs. assumed

**Verified (read in code, cited):** everything in §1–§2 and the violation lists above.
**Assumed (needs you):** Q1–Q4 below; that current SLC paper data still shows Grade A negative
(re-check on Mac before any grade-multiplier decisions); that the Mac-side tools/studies not in
this repo exist as described; VPS specifics (provider, Tailscale availability).
**Not run:** nothing was executed against your live DBs or feeds from here; no performance
number in this document is a measurement.

## 9. Sign-off questions (blocking Phase 2)

1. **Vantage MT5 account:** which instrument classes are actually enabled — forex, metals,
   indices, energies, crypto CFDs? (Determines what the MT5 adapter may target.)
2. **Crypto exchange:** which exchange should the native adapter integrate? (Binance, Coinbase,
   Kraken, Bybit, other — and spot vs. perps.)
3. **Phase 2 first asset class:** agree with crypto-first (§7), or prefer indices-first through
   the existing MT5 path?
4. **Mac-only artifacts:** do `db_health.py`, `verify_live_trade.py`, the correlation-audit
   code, the lessons log, and any regime/session study scripts exist on your Mac in newer form?
   If yes, push them to this repo (or share them) before Phase 2 so we generalize the real
   versions, not reconstructions.
5. **Sign-off** on: shared-pipeline decision (§3.1), factor-bucket exposure model (§3.5),
   schema approach (§5), and the host split (§3.10).
