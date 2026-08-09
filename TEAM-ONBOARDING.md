# Team onboarding

For an engineer who has never seen this repo. It assumes you can read Python and does not assume
you know anything about trading. Read this before your first change; it is the shortest path to
not breaking something expensive.

## The one rule

> **A strategy decides what it wants. The engine decides what actually happens.**

Everything else in this document is a consequence of that sentence. A strategy is a *proposal
generator*. It emits "I would like to buy EURUSD, stop here, target there" and its authority ends
at that punctuation mark. It does not size the position, does not know the account balance, does
not know whether the system is in paper or live, does not talk to a venue, and cannot widen a
stop. `trading-bot/engine.py` decides all of that, for every strategy, at one choke point.

The practical form of the rule: **a change that moves a risk check out of `engine.py` and into a
strategy or a venue adapter turns one rail into N rails and will be rejected on sight.** If you
find yourself writing a risk check anywhere else, you have found a bug in your design, not a
limitation in the engine.

## Vocabulary, kept strictly apart

| Term | What it is |
|---|---|
| **Keel** | the platform. The engine, the rails, the store, the dashboards |
| **SLC** | *Structure · Liquidity · Confirmation*. **Strategy #1**. A signal source, nothing more |
| **MT5** | MetaTrader 5. **Venue #1**, reached over HTTP through the `SLCDataBridge` EA |
| **venue** | somewhere an order can go. MT5, a CCXT exchange, 3Commas as a router |
| **speed / trade_mode** | `intraday` or `swing`. Which timeframe set a signal was generated on |
| **mode** | `paper` \| `live` \| `off` \| `shadow`. What actually happens to a signal |
| **shadow** | a rejected-but-valid setup, recorded as a zero-size trade so you can measure what the rail cost or saved. Never notifies, never counted as live |
| **cell** | a `strategy × asset_class` pair. The unit the promotion gate operates on |

People conflate these three constantly in conversation: Keel is not SLC, SLC is not MT5, and MT5
is not "the bot". If a doc or a comment blurs them, that is a defect worth fixing.

## Twenty minutes to oriented

```bash
git clone <repo> && cd <repo>/trading-bot
pip install -r requirements.txt
for t in tests/test_*.py; do python3 "$t"; done     # should be all green, seconds
```

The suites need no network, no MT5 and no venue. If they are green you have a working checkout.

Read in this order:

1. `CLAUDE.md` — the safety invariants. Non-negotiable, and they win over every other document
   including this one.
2. `CONTRIBUTING.md` — house rules, the fail-closed principle, the files that need a second look.
3. `trading-bot/engine.py`, function `try_execute` — the whole risk model is one readable
   function. Everything before the first `INSERT` is a rail.
4. `docs/ARCHITECTURE-V2.md` — the decisive design doc. §1 is what to do this week, §3 is the
   verified defect register, §9 is the phased plan, §10 is the list of things deliberately *not*
   being built. It is long; §1, §9 and §10 are the load-bearing parts.
5. `SLC-Price-Action-Playbook.md` — only if you are touching strategy #1's logic.

Then `SETUP-GUIDE.md` if you need data actually flowing.

## Where the rails live

All of them are in `try_execute` in `trading-bot/engine.py`, in this order. Every one that calls
`skip()` records a `decisions` row under the `stage` name below, so the reason a trade did not
happen is queryable rather than lost.

| Stage | What it refuses |
|---|---|
| dedup | the same setup zone twice — 30 min after a skip, 12 h after an execution. **The one rail that records nothing:** it returns bare, with no signals row and no decisions row. That is a known defect (`docs/ARCHITECTURE-V2.md` D20), not the pattern to copy |
| `fail_safe` | DB integrity suspect, manual halt active, or no balance data yet |
| `mode` | `trading_mode` is `off` |
| `session` | the market for that instrument is closed (never blocks crypto) |
| `news` | inside a scheduled-event blackout window for that asset class |
| `concurrency` | portfolio `max_concurrent`, one trade per symbol, `max_concurrent_per_class` |
| `exposure` | `max_correlated` same-direction positions; factor-bucket exposure (EUR + DAX + gold longs are one stacked bet) |
| `loss_limit` | the daily −2% or weekly −5% kill switch has fired |
| `rails` | no live price, price moved through the stop, spread > 10% of stop distance, price drifted >25% of stop from the analysed entry, RR at the actual fill below `min_rr × 0.9` |
| `sizing` | no tick metadata, or the broker's minimum lot would risk >1.5× the intended amount |

Two things about that list are the design, not an accident:

- **Risk factors may only reduce risk.** The loss governor and the session factor are applied
  behind `if factor < 1.0` guards. A "factor" that can exceed 1.0 is a bug, and so is a floor
  under a product of reducing factors — a floor raises risk exactly when every de-risking signal
  is firing at once.
- **Rejections that were otherwise-valid setups are recorded as shadow trades** (`track=True`),
  at zero size, with the skip reason in the setup JSON. That is how you answer "what are the
  rails costing me" with numbers instead of an opinion.

## The six files that need a second look

Changes to these reach money. Expect scrutiny, write a test, and say in the PR which invariant
you checked the change against.

| File | Why |
|---|---|
| `trading-bot/engine.py` | the choke point; every rail |
| `trading-bot/params_store.py` | per-origin whitelists, bounds, hard code ceilings, audit rows |
| `trading-bot/live_switch.py` | the only path to `trading_mode = live` |
| `trading-bot/analysis.py` | the promotion gate |
| `trading-bot/storage.py` | schema, migrations, command queue |
| `SLCDataBridge.mq5` | EA-side stop refusal and the `AllowTradeExecution` gate |

CI labels any PR touching them (`.github/workflows/guard.yml`), and `lefthook.yml` prompts
locally when one changes with no test alongside it.

## The principles you will be held to

**Fail closed.** Stale data, an unreachable venue, a suspect DB, a missing sign-off: stand aside.
Standing aside costs an opportunity. Failing open costs money. This is why `on_stale: halt` is the
news-calendar default and why an unclassified parameter still voids a promotion sign-off.

**Enforce at the write layer, not at the call site.** If a rule can be bypassed by forgetting to
call something, it is not implemented yet. Every parameter write goes through
`params_store.set_param` with an origin and a reason; nothing writes settings directly.

**Record the decisions not taken.** Every evaluation gets a `decisions.record`, including the
ones that end in "no". The diagnostic value lives in the refusals.

**Derive state from the trades table; do not keep counters.** Counters do not survive a restart.
`loss_governor` is the pattern to copy.

**Schema changes are additive only.** `storage.py` holds a `_MIGRATIONS` list of
`(table, column, decl)` tuples and `_apply_migrations` adds any column the live DB is missing.
Add a tuple. Never drop a column, never rewrite one, never rebuild a table in place.

**Secrets live in the runtime DB and nowhere else.** Not in source, not in `config.yaml`, not in
a committed file. This applies to exchange API keys exactly as it applies to the Telegram token.
`trading-bot/data/` and `trading-bot/state/` are gitignored and untracked, and CI fails a PR that
tracks anything under them.

**New strategies go behind the registry, default to shadow, and clear their own gate.** Never
fork the rails per strategy, and never let one strategy's tuning move another's parameters.

## The promotion gate

`analysis.promotion_status()` computes it, per **cell** (`strategy × asset_class`), from closed
**paper** trades. All five checks must pass:

| Check | Requirement |
|---|---|
| `sample_size` | ≥ **50** closed paper trades in that cell (`GATE_MIN_TRADES`) |
| `positive_expectancy` | `expectancy_r > 0` **and** n ≥ 50 — expectancy over a handful of trades is noise, so it carries the same floor |
| `data_trust` | latest `hallucination_check.py` verdict is `GROUNDED` **and** less than 24 h old |
| `rails_exercised` | at least one of `loss_limit`, `exposure`, `concurrency` has actually fired. Present is not the same as proven |
| `manual_signoff` | a human sign-off row that is ≥ 1 h old (no sign-and-go), ≤ 30 days old, and not predating the newest accepted *behavioural* parameter change |

`gate_open` only permits **requesting** live. It never flips anything. The switch itself is
`live_switch.py`: `POST /api/live/request` issues a one-time token with a 60-second TTL, and
`POST /api/live/confirm` requires that token plus the phrase `GO LIVE` typed back exactly, and
re-validates the blockers and the gate at confirm time because the world may have changed in
sixty seconds. **And** `AllowTradeExecution = true` must be set in the EA inputs. Both halves are
required; neither alone places a live order. `POST /api/live/paper` de-escalates with no ceremony
and is never gated — de-escalation is always easy.

Today every cell is at **0 / 50**. Nothing is close.

## Honest current state

| | |
|---|---|
| Trading mode | `paper` |
| EA `AllowTradeExecution` | `false` |
| Market data | none flowing |
| Closed trades | 0 |
| Gate | 0 / 50 on every cell |
| Venues configured | none |
| Strategies | one (SLC) |
| Venues wired into the engine | one (MT5, via the EA command queue — **not** via the adapter interface) |

`engine.py` imports neither `venues` nor `brokers`. The venue layer (`venues.py`,
`brokers/ccxt_venue.py`, `brokers/threecommas.py`) is a credential store, a health surface and a
position feed for reconciliation, reachable from the control dashboard on 8767. Every venue is
read-only until explicitly armed, and arming is a separate logged act from adding credentials.
CCXT covers the 103 exchanges the installed ccxt (4.5.71) lists, one adapter for all of them;
3Commas is wired as an execution router, not an exchange. Which exchanges actually answer depends
on where the bot runs — `binance.com` returns HTTP 451 from the US and works from India, while
`binanceus`, `kucoin`, `okx`, `kraken` and `coinbase` all answer. That is geography, not
configuration.

Routing orders through the adapter interface is Phase E in `docs/ARCHITECTURE-V2.md`, gated on a
live cell running for three months. Do not build ahead of it.

## Tests

`trading-bot/tests/` holds the risk-rail, circuit-breaker and promotion-gate suites plus the
integration ones around them. Conventions, all of which your new test must follow:

- Runs as a plain script: `python3 tests/test_x.py`, from the `trading-bot` directory.
- No network, no MT5, no venue, no fixtures framework. Copy the structure of
  `tests/test_risk_rails.py`.
- Prints a pass/fail count and exits non-zero on failure.

`.github/workflows/tests.yml` runs the suites on Python 3.11 and 3.12 on each push and pull
request, plus `compileall` over every module. It names each suite in its own step rather than
globbing, so **a new test file also needs a step added there** or CI will pass without ever
running it.

- **A fix without a test is a hope.** If you close a hole, add the test that would have caught it.
- If an existing test has to change to let your change through, say so explicitly in the PR and
  explain why the old expectation was wrong. A test quietly relaxing is how a rail gets weakened.

## Operating it

- **Legacy dashboard** `http://localhost:8766` — EA protocol routes, performance, chart, trade
  history, per-symbol engine analysis, pairs manager, settings, Telegram panel.
- **Control dashboard** `http://127.0.0.1:8767` — venues, decisions, studies, promotion gate,
  parameter writes, halt/resume, the live switch. Binds loopback only; mutating endpoints need
  the `X-Dashboard-Token` header, whose value comes from `DASHBOARD_TOKEN` or
  `trading-bot/state/dashboard_token` (0600).
- **Self-tuning agent** — evaluates every 4 h once ≥15 trades have closed, may only nudge a small
  whitelist, and can never touch `risk_pct`, stops, `max_concurrent` or `trading_mode`. It also
  never restarts a service and never flips to live.
- **News agent** (`python3 news_agent.py`, separate process) — RSS sentiment feeding stop
  management on this bot's own positions (`magic_filter: 770001`). Restart it after changing
  notification settings. Note that it is currently a *second execution authority* outside the
  choke point; routing it back through the engine is Phase C work (`docs/ARCHITECTURE-V2.md` §9).
- **`python3 hallucination_check.py`** (repo root, read-only) — DB integrity, feed freshness, and
  whether the agent stayed inside its authority. Appends a verdict to `hallucination_check.jsonl`
  and feeds the `data_trust` gate check.
- **`./recover-db.sh`** — stops the server gracefully, backs up the bad DB, rebuilds with
  `sqlite3 .recover`, integrity-checks and swaps it in. Never act on numbers from a suspect DB.
- **`./watchdog-install.sh`** — launchd jobs (macOS) for `server.py` and `news_agent.py`;
  `status` and `remove` subcommands.

## Things deliberately not being built

`docs/ARCHITECTURE-V2.md` §10 is a kill list with reasons, so decisions survive turnover. The two
you are most likely to reinvent:

- **Grid / DCA as strategy #2.** `CLAUDE.md` invariant 11 forbids averaging down, and mechanically
  it is worse than doctrinal: without a stop distance the sizing function returns zero, the
  R-multiple has no denominator, and expectancy has nothing to average. It is a second risk model
  the rails cannot measure, not a plugin.
- **Auto-discovery of `strategies/*.py`.** One author, one line in the registry. Import-time
  discovery makes "conformance gates registration" false.

## Where to read next

- Safety invariants → `CLAUDE.md`
- House rules and the before-live checklist → `CONTRIBUTING.md`
- Design, defect register, phases, kill list → `docs/ARCHITECTURE-V2.md`
- Venue landscape the adapter layer was designed against → `docs/PLATFORM-REQUIREMENTS-ANALYSIS.md`
- Every endpoint, webhook, port and magic number → `WEBHOOKS-AND-INTEGRATIONS.md`
- Secret handling and rotation → `SECURITY.md`
- Strategy #1's rules → `SLC-Price-Action-Playbook.md`
- How it got here and what is still open → `DEVELOPMENT-HISTORY.md`
- Getting data flowing → `SETUP-GUIDE.md`
