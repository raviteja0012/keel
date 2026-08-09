SLC bot — strategy-plugin + TradingView webhook foundation
==========================================================

  >>> ALREADY APPLIED. The files below are already in the repo (trading-bot/strategies/,
  >>> tv_webhook.py, tests/, and the engine/server/dashboard changes). This file and
  >>> slc-foundation.patch are kept only as a record of what landed; you do not need to
  >>> re-apply them. The instructions below are the original apply notes.

Extract this at the REPO ROOT (the folder that contains trading-bot/ and
legacy/). It places the files into trading-bot/ — 4 new, 3 modified.

NEW
  trading-bot/strategies/__init__.py            strategy-plugin registry (SLC = plugin #1)
  trading-bot/tv_webhook.py                     TradingView parser/auth (pure)
  trading-bot/tests/test_strategy_registry.py   registry tests (no MT5)
  trading-bot/tests/test_tv_webhook.py          webhook tests incl. gated endpoint (no MT5)

MODIFIED (overwrites your copies with the updated versions)
  trading-bot/engine.py        engine_loop -> registry; + ingest_external_signal (rails)
  trading-bot/server.py        /api/tv_webhook + seed/allow-set settings
  trading-bot/dashboard/index.html   SLC toggle + TradingView webhook panel

Test (both should print "passed"):
  cd trading-bot
  python3 tests/test_strategy_registry.py
  python3 tests/test_tv_webhook.py

Then commit + push from your machine. A patch (slc-foundation.patch) is also
included if you'd rather `git apply` it from the repo root instead of extracting.

Safety: external alerts are CANDIDATES routed through the same try_execute rails
as an SLC signal (mode, stop side, spread, RR, concurrency, correlation, loss
limits, sizing) — never a raw order, never flips trading_mode. The webhook is
OFF until enabled AND a token is set; the token lives in the runtime DB, not in
source. Paper stays the default; live is still the double-gated step.
