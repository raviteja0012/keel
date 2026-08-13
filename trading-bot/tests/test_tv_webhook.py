"""TradingView webhook tests: pure parser/auth + routing through the engine
rails + the gated Flask endpoint. No MT5 needed (flask required for the
endpoint test).

    cd "$BOT_DIR" && python3 tests/test_tv_webhook.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage
storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")

import tv_webhook
import engine
import sessions

# Pin the session calendar open: these tests exercise the webhook->rails
# path with EURUSD at arbitrary wall-clock times (weekends included); the
# session gate has its own time-injected tests in test_risk_rails.py.
engine.sessions.is_market_open = lambda symbol, now=None: (True, "")


# ---------------------------------------------------------- pure parser / auth
def test_parse_buy_alert():
    raw = json.dumps({"token": "x", "ticker": "OANDA:EURUSD", "action": "buy",
                      "entry": 1.1000, "sl": 1.0950, "tp": 1.1150})
    out = tv_webhook.parse_payload(raw)
    assert out["ok"]
    f = out["fields"]
    assert f["side"] == "buy" and f["ticker"] == "OANDA:EURUSD"
    assert f["entry"] == 1.10 and f["sl"] == 1.095 and f["tp"] == 1.115


def test_parse_aliases_and_short():
    out = tv_webhook.parse_payload({"passphrase": "p", "symbol": "XAUUSD",
                                    "side": "short", "price": 2400, "stop": 2410})
    f = out["fields"]
    assert f["token"] == "p" and f["side"] == "sell"
    assert f["entry"] == 2400 and f["sl"] == 2410


def test_parse_bad_json():
    out = tv_webhook.parse_payload("{not json")
    assert not out["ok"] and "JSON" in out["error"]


def test_map_symbol():
    assert tv_webhook.map_symbol("OANDA:EURUSD") == "EURUSD"
    assert tv_webhook.map_symbol("BINANCE:BTCUSDT") == "BTCUSD"
    assert tv_webhook.map_symbol("BTCUSDT.P") == "BTCUSD"
    assert tv_webhook.map_symbol("GOLD") == "XAUUSD"
    assert tv_webhook.map_symbol("FX:UNKNOWNPAIR") is None
    assert tv_webhook.map_symbol("") is None
    assert tv_webhook.map_symbol("MYBROKER:WTF", {"WTF": "XTIUSD"}) == "XTIUSD"


def test_check_token():
    assert tv_webhook.check_token("abc", "abc") is True
    assert tv_webhook.check_token("abc", "xyz") is False
    assert tv_webhook.check_token("abc", "") is False     # unconfigured -> nothing authenticates
    assert tv_webhook.check_token("", "abc") is False
    assert tv_webhook.check_token(None, "abc") is False


# --------------------------------------------------- routing through the rails
def _fresh_db_and_feed():
    storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
    storage._conn = None
    storage.init()
    for k, v in {"trading_mode": "paper", "modes": ["swing"], "risk_pct": 1.0,
                 "b_setup_risk_factor": 0.5, "min_rr": 2.5, "atr_buffer": 0.35,
                 "vol_mult": 0.0, "max_concurrent": 5, "max_correlated": 5,
                 "daily_stop_pct": 2.0, "weekly_stop_pct": 5.0, "min_grade": "B",
                 "regime_max": 2.5, "regime_b_ban": 1.5, "max_spread_frac": 0.10,
                 "enabled_pairs": ["EURUSD"], "watch_pairs": [],
                 "agent_disabled_pairs": [], "agent_disabled_modes": [],
                 "paper_balance": 10000.0}.items():
        storage.set_setting(k, v)
    # mark the news calendar fresh so its stale-halt fail-safe (tested in
    # test_news_regime.py) doesn't block entries in this fresh DB
    storage.set_setting("news_calendar_last_ok", int(time.time()))
    # synthetic live feed: tight spread + tick sizing so calc_lots works
    engine.feed_state["prices"] = {"EURUSD": {
        "symbol": "EURUSD", "bid": 1.09998, "ask": 1.10002,
        "tick_value": 1.0, "tick_size": 0.0001,
        "src": engine.MT5_SOURCE, "src_t": time.time()}}
    engine.feed_state["account"] = {"balance": 10000.0, "equity": 10000.0}
    engine.feed_state["last_feed_t"] = time.time()
    engine._recent_keys.clear()


def test_ingest_buy_opens_paper_trade():
    _fresh_db_and_feed()
    fields = {"side": "buy", "trade_mode": "swing", "entry": 1.10002,
              "sl": 1.0950, "tp": 1.1150, "strategy": "tv-test"}
    res = engine.ingest_external_signal("EURUSD", fields, engine.params())
    assert res["accepted"], res
    opens = storage.open_trades("paper")
    assert len(opens) == 1, opens
    assert opens[0]["symbol"] == "EURUSD" and opens[0]["side"] == "buy"
    # paper mode -> NO live command queued (a webhook is never a raw order)
    assert storage.next_command() is None


def test_ingest_rejects_wrong_side_stop():
    _fresh_db_and_feed()
    # long with a stop ABOVE entry -> invalid, rejected, no trade
    res = engine.ingest_external_signal(
        "EURUSD", {"side": "buy", "entry": 1.10, "sl": 1.11, "tp": 1.12}, engine.params())
    assert not res["accepted"] and "stop" in res["reason"]
    assert storage.open_trades("paper") == []


def test_ingest_requires_stop():
    _fresh_db_and_feed()
    res = engine.ingest_external_signal(
        "EURUSD", {"side": "buy", "entry": 1.10}, engine.params())
    assert not res["accepted"] and "stop" in res["reason"]


# --------------------------------------------------------- gated Flask endpoint
def test_endpoint_gated_and_authed():
    _fresh_db_and_feed()
    import server
    server.app.config["TESTING"] = True
    c = server.app.test_client()
    body = {"token": "secret", "ticker": "OANDA:EURUSD", "action": "buy",
            "entry": 1.10002, "sl": 1.0950, "tp": 1.1150}

    # disabled -> 403
    storage.set_setting("tradingview_webhook_enabled", False)
    storage.set_setting("tradingview_webhook_token", "secret")
    assert c.post("/api/tv_webhook", json=body).status_code == 403

    # enabled, bad token -> 401
    storage.set_setting("tradingview_webhook_enabled", True)
    assert c.post("/api/tv_webhook", json={**body, "token": "wrong"}).status_code == 401

    # enabled, good token -> 200 accepted, trade opened
    r = c.post("/api/tv_webhook", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["result"]["accepted"]
    assert len(storage.open_trades("paper")) == 1

    # unknown ticker -> 422
    assert c.post("/api/tv_webhook", json={**body, "ticker": "FX:NOPE"}).status_code == 422


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print("\n%d passed" % len(fns))
