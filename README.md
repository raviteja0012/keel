# Keel

A multi-strategy, multi-venue automated trading platform. Strategies propose, one engine
disposes, and every venue is reached through an adapter that sits behind the same set of risk
rails.

> **A strategy decides what it wants. The engine decides what actually happens.**

Three words that are not interchangeable, and the rest of this repo only makes sense once they
are kept apart:

- **Keel** is the platform — the engine, the rails, the store, the dashboards.
- **SLC** (*Structure · Liquidity · Confirmation*) is **strategy #1**: a price-action playbook
  run at intraday and swing speeds. It is a signal source and nothing else.
- **MetaTrader 5** is **venue #1**: a place an order can go, reached over HTTP through the
  `SLCDataBridge` Expert Advisor.

Adding a strategy does not add a rail. Adding a venue does not add a rail. Every rail lives in
`trading-bot/engine.py`, which is the sole execution choke point.

> ⚠️ **Educational software, not financial advice.** The promotion gate in code requires 50
> closed paper trades with positive expectancy per strategy × asset class before live is even
> requestable. See [`LICENSE.md`](LICENSE.md).
>
> 🔐 **No secrets are committed, and this is checkable rather than promised.** `git ls-files`
> returns zero paths under `trading-bot/data/` and `trading-bot/state/`; both are excluded by
> [`.gitignore`](.gitignore) (lines 36–37), as is `hallucination_check.jsonl`. Telegram tokens,
> Discord webhooks, MT5 logins, exchange API keys and LAN addresses exist only in the runtime
> DB, entered through the dashboard on each deployment. Read [`SECURITY.md`](SECURITY.md)
> before sharing the repo.

---

## Where this actually is today

| | |
|---|---|
| Trading mode | **paper** |
| EA `AllowTradeExecution` | **false** (EA-side default) |
| Market data | **none flowing** |
| Closed trades | **0** |
| Promotion gate | **0 / 50** closed paper trades, on every cell |
| Venues configured | **none** — the adapters exist, nothing is armed |

Nothing here is close to live. The binding constraint is calendar time on a paper sample that
has not started accumulating; `docs/ARCHITECTURE-V2.md` §1 is the argument for what to spend
that time on.

## Architecture

```
strategies/          SLC (#1)  ·  /api/tv_webhook external intake (off by default)
                     they PROPOSE. None of them can size, stop, or place anything.
      │
      ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│ engine.py   the sole execution choke point — every rail lives here:            │
│             fail-safe (DB integrity, manual halt) · mode · session calendar ·  │
│             scheduled-news blackout · concurrency (portfolio, per symbol,      │
│             per asset class) · correlation · daily/weekly kill switches ·      │
│             spread and drift · RR-at-fill · loss governor · factor-bucket      │
│             exposure · sizing floor.  Plus the paper broker and the live       │
│             command queue.                                                     │
└────────────────────────────────────────────────────────────────────────────────┘
      │                            │                              │
      ▼                            ▼                              ▼
 MT5 (venue #1)              storage.py                    notifier.py
 server.py :8766             SQLite WAL, data/trading.db   Telegram + Discord
 EA pushes every 5s and      additive migrations,          telegram_notifier.py
 polls the command queue     command queue, settings
 SLCDataBridge.mq5           (credentials live here)

 venues.py + brokers/    credential store and adapter registry, reachable from
   ccxt     103 exchanges    the control dashboard on :8767. Read-only until
   3commas  execution router explicitly armed. The engine does not route orders
                             through it yet — see "The venue layer" below.

 news_agent.py (separate process)   RSS sentiment → SL management + market alerts
 dashboard/index.html      :8766    legacy operator UI
 dashboard/multiasset.html :8767    control dashboard, 127.0.0.1 only
```

## The venue layer

`trading-bot/venues.py` is the credential store and registry; `trading-bot/brokers/` holds the
adapter contract and the two implementations.

**CCXT — one adapter, many exchanges.** `brokers/ccxt_venue.py` normalises markets, balances,
orders and OHLCV, so Binance, Kraken and Coinbase are configuration rather than three
hand-written integrations. The installed ccxt (4.5.71) lists **103 exchanges**, all of which
the control dashboard offers. Where CCXT leaks, the adapter does the work instead of
pretending: `min_notional` is looked for in every place exchanges hide it, precision is handled
both as a decimal-places count and as a tick size, and `clientOrderId` is only trusted for
idempotency on the exchanges known to honour it — elsewhere the adapter looks before it places,
because assuming idempotency you do not have is how double fills happen.

`ccxt` is **not** in `trading-bot/requirements.txt`. Install it separately
(`pip install ccxt`); a missing dependency disables that venue kind rather than the engine —
`brokers.kinds()` returns `['3commas']` instead of `['3commas', 'ccxt']`.

**3Commas — an execution router, not an exchange.** `brokers/threecommas.py` wires it as a
destination and a source of account state: the engine decides, 3Commas transmits to the
underlying exchange. It publishes no usable instrument filters, so `symbol_meta` refuses rather
than guessing a tick size — size against the exchange adapter and route execution here. It has
no `clientOrderId`, so the idempotency key rides in the `note` field, which is the only thing
that round-trips. The `manages_positions` flag records the operator's declaration that 3Commas'
own DCA/grid bots also trade the account; when it is set the adapter refuses to open a
position, because two risk engines fighting over one stop is not a configuration this platform
supports.

**Credentials live in the runtime DB**, entered through the dashboard, never in source, never
in `config.yaml`. Reads return a masked marker plus a six-character fingerprint of the secret,
so you can tell *which* key is loaded without exposing it, and re-saving a venue with a blank
key keeps the stored one.

**Every venue is read-only until it is explicitly armed.** Adding credentials lets you *see* an
account. `venues.set_trading_enabled` is a separate act with its own logged entry, and the
adapters raise `VenueReadOnly` on any write before it. Nobody pastes an API key and
accidentally arms an execution path.

**Reachability is a property of where the bot runs, not of configuration.** Verified:
`binance.com` returns **HTTP 451** from the US and answers normally from India; `binanceus`,
`kucoin`, `okx`, `kraken` and `coinbase` all answer. The dashboard offers every exchange ccxt
supports and makes no promise about which of them will respond from your host — use *Test
reachability only* before storing a key.

**What is not built yet:** `engine.py` imports neither `venues` nor `brokers`. The venue layer
today is credential storage, health, balances and a position feed for reconciliation, reachable
from the control dashboard. MT5 execution still goes through the command queue and the EA.
Putting MT5 behind the adapter interface, and routing orders through it, is Phase E in
[`docs/ARCHITECTURE-V2.md`](docs/ARCHITECTURE-V2.md#9-phased-plan).

## Shipped defaults (`trading-bot/config.yaml`)

The runtime DB is not committed, so a fresh clone runs these. Dashboard changes are written to
the DB and win after first run.

| Setting | Value |
|---|---|
| Trading mode | `paper` |
| Speeds | intraday + swing |
| EA bridge | Flask on `0.0.0.0:8766`; legacy dashboard at `http://localhost:8766` |
| Control dashboard | FastAPI on `127.0.0.1:8767`, token-gated for anything that mutates |
| EA | `SLCDataBridge.mq5` **v2.30**; the bot tags its trades with magic **770001** |
| Risk | 1% per A+ trade (B setups ×0.5), min RR **2.0**, ATR buffer 0.35 |
| Kill switches | daily −2%, weekly −5% |
| Concurrency | 2 open trades total, 2 per asset class, 3 same-direction |
| Grade / volume gate | `min_grade = B`, `vol_mult = 1.0` |
| Universe | 8 enabled pairs (EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD, XAGUSD, BTCUSD, ETHUSD) |

## Quick start

```bash
cd trading-bot
pip install -r requirements.txt
python3 server.py          # EA endpoints + legacy dashboard, http://localhost:8766
python3 dashboard_api.py   # control dashboard, http://127.0.0.1:8767 (localhost only)
python3 news_agent.py      # separate process; restart after changing notification settings
```

Then either attach the EA in MT5 or connect an exchange —
[`SETUP-GUIDE.md`](SETUP-GUIDE.md) covers both paths end to end.

Do not run the runtime checkout inside OneDrive, Dropbox or iCloud. `data/trading.db` is SQLite
in WAL mode and a background sync agent writing underneath the engine is a corruption mechanism
aimed at the file the kill switches read.

## Docs

| Doc | Read it for |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | the safety invariants — non-negotiable, they win over everything |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | house rules, fail-closed, the six files that need a second look |
| [`TEAM-ONBOARDING.md`](TEAM-ONBOARDING.md) | orientation for an engineer who has never seen this repo |
| [`SETUP-GUIDE.md`](SETUP-GUIDE.md) | attaching the MT5 EA, and connecting an exchange |
| [`docs/ARCHITECTURE-V2.md`](docs/ARCHITECTURE-V2.md) | the decisive design doc: defect register, phased plan, kill list |
| [`docs/PLATFORM-REQUIREMENTS-ANALYSIS.md`](docs/PLATFORM-REQUIREMENTS-ANALYSIS.md) | the venue landscape this layer was designed against |
| [`SLC-Price-Action-Playbook.md`](SLC-Price-Action-Playbook.md) | what strategy #1 is trying to do |
| [`WEBHOOKS-AND-INTEGRATIONS.md`](WEBHOOKS-AND-INTEGRATIONS.md) | every endpoint, webhook, port and magic number |
| [`SECURITY.md`](SECURITY.md) | where secrets live and how to rotate them |
| [`DEVELOPMENT-HISTORY.md`](DEVELOPMENT-HISTORY.md) | how it got here, and the open items |
| [`CONSOLIDATION.md`](CONSOLIDATION.md) | how the current build and `legacy/` relate |

| Code & runtime | |
|---|---|
| `trading-bot/` | the application — see [`trading-bot/README.md`](trading-bot/README.md) |
| `trading-bot/engine.py` | the choke point. Read this before changing anything that touches money |
| `trading-bot/venues.py`, `trading-bot/brokers/` | venue registry, credential store, CCXT and 3Commas adapters |
| `trading-bot/strategies/` | strategy-plugin registry (SLC = #1) |
| `trading-bot/tests/` | rail, circuit-breaker and gate suites; each runs as a plain script, no network, no MT5 |
| `trading-bot/config.yaml`, `config.example.yaml` | startup defaults; the DB wins after first run |
| `trading-bot/data/`, `trading-bot/state/` | runtime DB, logs, traces — gitignored, created at runtime |
| `SLCDataBridge.mq5` / `.original.mq5` | the MT5 EA (v2.30) and its baseline |
| `hallucination_check.py`, `recover-db.sh`, `watchdog-install.sh` | ops tooling |
| `cowork-skills/slc-bot/` | the consolidated Cowork skill (operate / analyze / develop) |

## Tests and CI

```bash
cd trading-bot
for t in tests/test_*.py; do python3 "$t"; done
```

Every suite runs as a plain script, needs no network, no MT5 and no venue, prints a pass/fail
count and exits non-zero on failure. `.github/workflows/tests.yml` runs them on Python 3.11 and
3.12 on every push and pull request. `.github/workflows/guard.yml` refuses a PR that tracks
anything under `trading-bot/data/` or `trading-bot/state/`, scans the diff for credential
patterns, and labels any change to `engine.py`, `params_store.py`, `live_switch.py`,
`analysis.py`, `storage.py`, `SLCDataBridge.mq5` or `CLAUDE.md` so a person reads it.
`lefthook.yml` runs the same checks locally before the commit exists (`lefthook install`). A red
suite is a blocked merge, not a note for later.
