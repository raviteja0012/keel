"""Phase 3 tests: scheduled-news blackout gate + per-class news entities.

Covers:
  * blackout window math (before/after edges) per impact level
  * entity matching via the instrument registry (EURUSD blocked by a USD
    event; BTCUSD explicitly NOT blocked — crypto policy is no blackout)
  * instruments with no declared entities never match everything
    (the inverted legacy falsy-filter bug)
  * manual event injection + audit rows via log_action
  * engine integration: a blocked entry writes decisions + news_actions
    with the exact event title
  * news_evaluator entity parsing for indices/crypto + per-class BE buffer

Run:  cd trading-bot && python3 tests/test_news_regime.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage
storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
storage.init()

import engine
import instruments
import news_calendar
import news_evaluator

instruments.seed_defaults()
engine.sessions.is_market_open = lambda symbol, now=None: (True, "")

NOW = int(time.time())
# mark the calendar as freshly fetched so the stale-halt fail-safe (tested
# explicitly below) doesn't mask the window/entity tests
storage.set_setting("news_calendar_last_ok", NOW)


def _wipe():
    storage.execute("DELETE FROM news_events")
    storage.execute("DELETE FROM news_actions")


def test_blackout_window_edges():
    _wipe()
    news_calendar.add_manual(NOW + 600, "US CPI m/m", ["USD"], "high")
    # 10 min before a high-impact USD event: inside the 30-min window
    blk = news_calendar.is_blackout("EURUSD", now=NOW)
    assert blk and "US CPI" in blk["reason"], blk
    # 40 min before: outside
    assert news_calendar.is_blackout("EURUSD", now=NOW + 600 - 40 * 60) is None
    # 29 min after: inside
    assert news_calendar.is_blackout("EURUSD", now=NOW + 600 + 29 * 60)
    # 31 min after: outside
    assert news_calendar.is_blackout("EURUSD", now=NOW + 600 + 31 * 60) is None


def test_entity_matching_per_class():
    _wipe()
    news_calendar.add_manual(NOW, "ECB Rate Decision", ["EUR"], "high")
    assert news_calendar.is_blackout("EURUSD", now=NOW), "EUR leg must match"
    assert news_calendar.is_blackout("GER40", now=NOW), \
        "GER40 declares EUR in news_entities"
    assert news_calendar.is_blackout("USDJPY", now=NOW) is None, \
        "no EUR leg -> no blackout"


def test_crypto_explicitly_never_blacked_out():
    _wipe()
    news_calendar.add_manual(NOW, "FOMC Statement", ["USD"], "high")
    assert news_calendar.is_blackout("EURUSD", now=NOW)
    assert news_calendar.is_blackout("BTCUSD", now=NOW) is None, \
        "crypto policy: no scheduled blackout (weekend risk factor instead)"


def test_medium_impact_not_blocked_by_default():
    _wipe()
    news_calendar.add_manual(NOW, "US Consumer Sentiment", ["USD"], "medium")
    assert news_calendar.is_blackout("EURUSD", now=NOW) is None, \
        "default windows only define high impact"


def test_no_entities_never_matches_everything():
    _wipe()
    news_calendar.add_manual(NOW, "FOMC Statement", ["USD"], "high")
    instruments.upsert("MYSTERY1", {"asset_class": "indices",
                                    "news_entities": []})
    try:
        assert news_calendar.is_blackout("MYSTERY1", now=NOW) is None, \
            "empty entity list must mean NO events apply, not all"
    finally:
        storage.execute("DELETE FROM instruments WHERE symbol='MYSTERY1'")
        instruments.invalidate_cache()


def test_engine_blocks_and_audits_blackout():
    _wipe()
    storage.execute("DELETE FROM trades")
    storage.execute("DELETE FROM decisions")
    news_calendar.add_manual(NOW + 300, "Nonfarm Payrolls", ["USD"], "high")
    engine.feed_state["prices"]["EURUSD"] = {
        "symbol": "EURUSD", "bid": 1.1000, "ask": 1.10008,
        "tick_value": 1.0, "tick_size": 0.0001, "spread": 1, "point": 0.0001,
        "src": engine.MT5_SOURCE, "src_t": time.time()}
    sig = {"symbol": "EURUSD", "trade_mode": "swing", "side": "buy",
           "grade": "A", "entry": 1.10008, "sl": 1.0950, "tp1": 1.105,
           "tp": 1.1125, "rr": 2.5, "regime": 1.0, "setup": {},
           "strategy": "slc", "key": "t|news|blk"}
    p = engine.params()
    p.update({"trading_mode": "paper", "paper_balance": 10000.0,
              "halt_new_entries": False})
    engine.try_execute(sig, p)
    assert storage.open_trades("paper") == [], "entry must be blocked"
    d = storage.query_one(
        "SELECT * FROM decisions WHERE stage='news' ORDER BY id DESC LIMIT 1")
    assert d and "Nonfarm" in d["reason"], d
    ctx = json.loads(d["news_ctx"])
    assert ctx["title"] == "Nonfarm Payrolls", ctx
    na = storage.query_one(
        "SELECT * FROM news_actions ORDER BY id DESC LIMIT 1")
    assert na and na["action"] == "block_entry" and na["symbol"] == "EURUSD"
    assert "Nonfarm" in na["reason"], "the exact event must be in the audit row"
    engine._recent_keys.clear()
    storage.execute("DELETE FROM trades")


def test_dedupe_on_same_event():
    _wipe()
    a = news_calendar.add_manual(NOW, "BoE Rate Decision", ["GBP"], "high")
    b = news_calendar.add_manual(NOW, "BoE Rate Decision", ["GBP"], "high")
    assert a and not b, "identical event must dedupe via hash"


def test_stale_calendar_fails_safe():
    _wipe()
    storage.set_setting("news_calendar_last_ok", NOW - 72 * 3600)  # stale
    try:
        blk = news_calendar.is_blackout("EURUSD", now=NOW)
        assert blk and "stale" in blk["reason"], \
            "stale calendar must halt classes that rely on blackout windows"
        assert news_calendar.is_blackout("BTCUSD", now=NOW) is None, \
            "crypto has no windows -> unaffected by calendar staleness"
        news_calendar.configure({"on_stale": "warn"})
        assert news_calendar.is_blackout("EURUSD", now=NOW) is None, \
            "on_stale=warn keeps trading"
    finally:
        news_calendar.configure({"on_stale": "halt"})
        storage.set_setting("news_calendar_last_ok", NOW)


# ---------------------------------------------------------- evaluator entities
def test_parse_symbol_indices_and_crypto():
    assert news_evaluator._parse_symbol("US500") == ("SPX", "USD")
    assert news_evaluator._parse_symbol("US30") == ("DOW", "USD")
    assert news_evaluator._parse_symbol("GER40") == ("DAX", "EUR")
    assert news_evaluator._parse_symbol("BTCUSD") == ("BTC", "USD")
    assert news_evaluator._parse_symbol("EURUSD") == ("EUR", "USD")


def test_crypto_sentiment_scores():
    r = news_evaluator.score_currency_sentiment(
        "BTC", ["Bitcoin surges to record high as ETF inflows accelerate"])
    assert r.score > 0, r
    r2 = news_evaluator.score_currency_sentiment(
        "BTC", ["SEC crypto crackdown deepens as bitcoin plunges"])
    assert r2.score < 0, r2


def test_be_buffer_per_class():
    pos = {"entry": 100000.0, "current": 101000.0, "sl": 99000.0,
           "side": "buy", "symbol": "BTCUSD"}
    be = news_evaluator.calculate_be_sl(pos, be_buffer_pips=2.0)
    assert be == 100020.0, "BTC BE buffer must be ~20 USD, not 0.0002 (%s)" % be
    pos_idx = {"entry": 40000.0, "current": 40200.0, "sl": 39800.0,
               "side": "buy", "symbol": "US30"}
    be = news_evaluator.calculate_be_sl(pos_idx, be_buffer_pips=2.0)
    assert be == 40002.0, be


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e)
        except Exception as e:
            failed += 1
            print("ERR ", fn.__name__, "-", repr(e))
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    sys.exit(1 if failed else 0)
