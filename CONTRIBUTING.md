# Contributing and standards

How to work in this repo so changes stay consistent and no rail gets weakened by accident.
The safety invariants are in `CLAUDE.md` and they win over everything here. Target design is
`docs/ARCHITECTURE.md`, venue landscape is `docs/PLATFORM-REQUIREMENTS-ANALYSIS.md`, setup is
`SETUP-GUIDE.md`, current state and the before-live checklist are in `docs/STATUS.md`.

## Run it

```bash
cd trading-bot
pip install -r requirements.txt
python3 server.py          # engine + EA endpoints + legacy dashboard, http://localhost:8766
python3 dashboard_api.py   # control dashboard, http://127.0.0.1:8767 (localhost only)
python3 news_agent.py      # separate process; restart after changing notification settings
for t in tests/test_*.py; do python3 "$t"; done   # must stay green
```

**Never run the runtime checkout inside a cloud-synced folder** (OneDrive, Dropbox, iCloud).
`data/trading.db` is SQLite in WAL mode; a background sync agent writing to it underneath the
engine is a corruption mechanism aimed at the file the kill switches read. Keep the runtime
copy on plain local disk.

## Locked architecture (do not change without a decision)

Single Python process for the engine, SQLite WAL on local disk as the authoritative store,
MetaTrader 5 reached through the `SLCDataBridge` EA over HTTP. No hosted database in the hot
path: the engine must keep managing stops through loss of internet to any non-venue service.
Remote dashboards are read-only projections over an overlay network, never public hosting of
the control plane.

## The rule everything else follows from

> A strategy decides what it wants. The engine decides what actually happens.

`engine.py` is the sole execution choke point. Risk sizing, kill switches, exposure limits,
concurrency, session calendars and news blackouts live there and nowhere else. A change that
moves any of those into a strategy or a venue adapter turns one rail into N rails and will be
rejected on sight.

## Files that need a second look

Changes to these touch money directly. Expect scrutiny, write a test, and say in the PR which
invariant you checked the change against:

| File | Why |
|---|---|
| `engine.py` | the execution choke point; every rail |
| `params_store.py` | parameter whitelists and hard code ceilings |
| `live_switch.py` | the only path to `trading_mode = live` |
| `analysis.py` | the promotion gate |
| `storage.py` | schema, migrations, command queue |
| `SLCDataBridge.mq5` | EA-side stop refusal and the `AllowTradeExecution` gate |

## Coding conventions

- Rails are enforced at the **write layer**, not by call-site discipline. If a rule can be
  bypassed by forgetting to call something, it is not implemented yet.
- Risk factors may only **reduce** risk. Guard every multiplier with `if factor < 1.0`.
- Fail **closed** on uncertainty. Stale data, unreachable venue, suspect DB, missing sign-off:
  stand aside. Standing aside costs an opportunity; failing open costs money.
- Every parameter write goes through `params_store.set_param` with an origin and a reason.
  Nothing writes settings directly.
- Every decision gets a `decisions.record`, including decisions **not** to trade. The decisions
  not taken are where the diagnostic value lives.
- Prefer deriving state from the trades table over keeping counters. Counters do not survive a
  restart; `loss_governor` is the pattern to copy.
- New strategies are added behind the strategy registry, default to shadow, and clear their own
  promotion gate before they trade. Never fork the rails per strategy.

## Secrets

Credentials live in the runtime DB, entered through the dashboard, and nowhere else. Never in
source, never in `config.yaml`, never in a committed file. `trading-bot/data/` and
`trading-bot/state/` are gitignored and must stay that way. Rotate anything exposed
immediately, see `SECURITY.md`.

This extends to every venue added later: exchange API keys follow the same rule.

## Tests

`trading-bot/tests/` holds the risk-rail, circuit-breaker and promotion-gate suites. CI runs
each as a script on Python 3.11 and 3.12 plus a compile check of every module.

- **A fix without a test is a hope.** If you close a hole, add the test that would have caught it.
- If an existing test has to change to accommodate your change, say so explicitly in the PR and
  explain why the old expectation was wrong. A test changing to let new behaviour through is
  exactly how a rail gets weakened quietly.
- Tests must not need MT5, a network, or a live venue.

## Branches, commits, pull requests

- Develop on a feature branch, never directly on `main`.
- Commit messages are short imperative sentences. Explain why, not what.
- Open pull requests as drafts until CI is green.
- CI (`.github/workflows/tests.yml`) runs the full suite on every push and pull request. Keep it
  green: a red suite is a blocked merge, not a note for later.

## Before going live

The gate is in code and it is not advisory. Per strategy, asset class and venue:

- 50+ closed paper trades with positive expectancy
- GROUNDED data-trust verdict from `hallucination_check.py`
- risk rails demonstrably fired in paper, not merely present
- a human sign-off, aged at least an hour, not superseded by a later parameter change
- the two-step confirm in `live_switch`, **and** `AllowTradeExecution = true` in the EA

Both halves of that last line are required. Neither one alone places a live order.
