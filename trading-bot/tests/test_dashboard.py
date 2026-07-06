"""Phase 4 tests: analysis library + FastAPI dashboard.

The paper dataset here is written through the REAL engine execution path
(engine.try_execute -> storage) and closed through the real paper-close path,
so every analysis function is exercised against data shaped exactly like
production rows — not hand-crafted fixtures.

Covers:
  * analysis: regime/session buckets, exposure snapshot, drawdown forensics
    (max DD, streaks, wick-outs), promotion gate checks incl. rails_exercised
  * dashboard API: read endpoints respond; control endpoints REQUIRE the
    token (401 without, works with); bounded param writes reject out-of-bounds
  * localhost-only default binding config

Run:  cd trading-bot && python3 tests/test_dashboard.py
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

import analysis
import engine
import instruments

instruments.seed_defaults()
engine.sessions.is_market_open = lambda symbol, now=None: (True, "")
storage.set_setting("news_calendar_last_ok", int(time.time()))

os.environ["DASHBOARD_TOKEN"] = "test-token-123"
import dashboard_api
from fastapi.testclient import TestClient
client = TestClient(dashboard_api.app)


# ------------------------------------------------ build a real paper dataset
def _price(symbol, bid, ask, tick=0.0001, tick_value=1.0):
    engine.feed_state["prices"][symbol] = {
        "symbol": symbol, "bid": bid, "ask": ask,
        "tick_value": tick_value, "tick_size": tick, "spread": 1, "point": tick}


def _open_paper(symbol, side, entry, sl, key):
    tp = entry + 2.5 * (entry - sl) if side == "buy" else entry - 2.5 * (sl - entry)
    sig = {"symbol": symbol, "trade_mode": "swing", "side": side, "grade": "A",
           "entry": entry, "sl": sl, "tp1": entry + (entry - sl), "tp": round(tp, 5),
           "rr": 2.5, "regime": 1.1, "setup": {}, "strategy": "slc", "key": key}
    p = engine.params()
    p.update({"trading_mode": "paper", "paper_balance": 10000.0,
              "max_concurrent": 20, "max_concurrent_per_class": 20,
              "max_correlated": 20, "max_bucket_exposure": 50.0,
              "risk_pct": 1.0, "min_rr": 2.0, "max_spread_frac": 0.10,
              "halt_new_entries": False})
    engine.try_execute(sig, p)
    trs = [t for t in storage.open_trades("paper") if t["symbol"] == symbol]
    assert trs, "fixture trade failed to open for %s" % symbol
    return trs[-1]


def _close_at(tr, price, exit_reason="tp"):
    engine._close_paper(dict(tr), price, exit_reason)


def _seed_dataset():
    """8 closed paper trades (5 winners / 3 losers, one wick-out, one
    spread-induced stop) across forex + crypto, opened and closed through
    the real engine code paths."""
    specs = [
        ("EURUSD", "buy", 1.1000, 1.0950, 1.1125, "take profit (TP2)"),   # +2.5R
        ("EURUSD", "buy", 1.1050, 1.1000, 1.0999, "stop loss"),           # -1R
        ("GBPUSD", "sell", 1.2700, 1.2750, 1.2575, "take profit (TP2)"),  # +2.5R
        ("USDJPY", "buy", 155.00, 154.50, 156.25, "take profit (TP2)"),
        ("BTCUSD", "buy", 100000.0, 99000.0, 102500.0, "take profit (TP2)"),
        ("BTCUSD", "sell", 101000.0, 102000.0, 102000.0,
         "stop loss [spread-induced, max 40.0p]"),                        # -1R
        ("ETHUSD", "buy", 3000.0, 2950.0, 3125.0, "take profit (TP2)"),
        ("XAUUSD", "buy", 3300.0, 3290.0, 3289.9, "stop loss"),           # -1R wickout
    ]
    for i, (sym, side, entry, sl, exitp, reason) in enumerate(specs):
        tick = 0.01 if sym in ("BTCUSD", "ETHUSD", "XAUUSD") else \
               (0.01 if "JPY" in sym else 0.0001)
        # broker-realistic per-tick values: crypto CFDs quote thousands of
        # USD, so a 0.01 tick is worth far less per lot than an FX tick
        tv = 0.001 if sym in ("BTCUSD", "ETHUSD") else 1.0
        eb, ea_ = (entry, entry + tick) if side == "buy" else (entry - tick, entry)
        _price(sym, eb, ea_, tick, tv)
        tr = _open_paper(sym, side, entry if side == "buy" else entry, sl,
                         key="seed|%d" % i)
        if "wickout" in reason or sym == "XAUUSD":
            storage.update_trade(tr["id"], {"mfe": 0.8})   # went +0.8R first
            tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tr["id"],))
        _close_at(tr, exitp, reason)
    closed = storage.query("SELECT * FROM trades WHERE status='closed' AND mode='paper'")
    assert len(closed) == len(specs), closed


_seed_dataset()


# ---------------------------------------------------------------- analysis
def test_regime_session_perf_buckets():
    r = analysis.regime_session_performance("paper")
    assert r["total"]["n"] == 8
    assert "forex" in r["by_asset_class"] and "crypto" in r["by_asset_class"]
    assert r["by_asset_class"]["crypto"]["n"] == 3
    # regime 1.1 recorded through decisions -> 'normal' band
    assert any("normal" in k for k in r["by_regime"]), r["by_regime"]
    assert r["total"]["expectancy_r"] > 0


def test_exposure_snapshot_empty_book():
    s = analysis.exposure_snapshot("paper")
    assert s["buckets"] == {} and s["positions"] == []


def test_drawdown_forensics():
    d = analysis.drawdown_forensics("paper")
    assert d["n"] == 8
    assert d["max_drawdown_r"] >= 1.0, d["max_drawdown_r"]
    assert d["worst_losing_streak"] >= 1
    assert d["wickout_losers"]["n"] >= 1, "the XAUUSD 0.8R-MFE loser must count"
    assert d["spread_induced_stops"] == 1
    assert d["loss_concentration"]["by_class"], d["loss_concentration"]
    assert len(d["equity_curve_r"]) == 8


def test_promotion_gate_math():
    p = analysis.promotion_status()
    cells = p["cells"]
    assert "slc|forex" in cells and "slc|crypto" in cells, list(cells)
    fx = cells["slc|forex"]
    assert fx["checks"]["sample_size"]["pass"] is False   # n=4 < 50
    assert fx["checks"]["positive_expectancy"]["pass"] is True
    assert fx["checks"]["manual_signoff"]["pass"] is False
    assert fx["gate_open"] is False, "gate must NOT open on a 4-trade sample"


def test_health_process_independent():
    h = analysis.health()
    assert h["trading_mode"] in ("paper", "live", "off")
    assert "engine_alive" in h and "newest_bar_age_s_by_class" in h


def test_health_distinguishes_engine_from_feed():
    """An engine standing aside with no EA feed must read alive-but-no-feed,
    not DOWN (the state a fresh parallel deployment sits in)."""
    storage.set_setting("engine_heartbeat_t", int(time.time()))
    storage.set_setting("ea_last_feed_t", 0)
    h = analysis.health()
    assert h["engine_alive"] is True
    assert h["ea_connected"] is False and h["ea_feed_age_s"] is None
    storage.set_setting("ea_last_feed_t", int(time.time()))
    h = analysis.health()
    assert h["ea_connected"] is True
    storage.set_setting("engine_heartbeat_t", 0)
    storage.set_setting("ea_last_feed_t", 0)


def test_health_survives_string_timestamps_in_real_dbs():
    """Regression: a real user DB contained equity rows whose t column was
    TEXT; strings sort above numbers so MAX(t) returned a string and
    health() crashed with TypeError. Poison both tables and verify every
    reader stays numeric."""
    storage.execute("INSERT INTO equity(t,mode,balance,equity) "
                    "VALUES('2026-06-01T10:00:00',?,?,?)", ("paper", 1.0, 1.0))
    storage.execute("INSERT OR REPLACE INTO bars(symbol,tf,t,o,h,l,c,v) "
                    "VALUES('EURUSD','1h','garbage',1,1,1,1,1)")
    storage.execute("INSERT INTO equity(t,mode,balance,equity) VALUES(?,?,?,?)",
                    (int(time.time()), "paper", 2.0, 2.0))
    try:
        h = analysis.health()                      # must not raise
        assert h["engine_alive"] is True, \
            "the NUMERIC max (just written) must drive liveness, not the string row"
        d = analysis.drawdown_forensics("paper")   # must not raise either
        assert d["n"] >= 1
    finally:
        storage.execute("DELETE FROM equity WHERE typeof(t)='text'")
        storage.execute("DELETE FROM bars WHERE typeof(t)='text'")


# ---------------------------------------------------------------- API
def test_read_endpoints_respond():
    for url in ("/api/health", "/api/performance?mode=paper",
                "/api/studies/regime_session", "/api/studies/exposure",
                "/api/studies/drawdown", "/api/studies/promotion",
                "/api/decisions?limit=10", "/api/param_changes",
                "/api/instruments", "/api/news_calendar", "/api/trades?limit=5",
                "/api/settings"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:200])


def test_secrets_never_in_settings():
    storage.set_setting("telegram_bot_token", "SECRET-TOKEN-XYZ")
    r = client.get("/api/settings").json()
    assert "SECRET-TOKEN-XYZ" not in json.dumps(r), "credential leaked"
    storage.set_setting("telegram_bot_token", "")


def test_controls_require_token():
    assert client.post("/api/params", json={"min_rr": 2.2}).status_code == 401
    assert client.post("/api/halt").status_code == 401
    r = client.post("/api/params", json={"min_rr": 2.2},
                    headers={"X-Dashboard-Token": "wrong"})
    assert r.status_code == 401


def test_bounded_param_write_via_api():
    h = {"X-Dashboard-Token": "test-token-123"}
    r = client.post("/api/params", json={"min_rr": 2.2}, headers=h).json()
    assert r["ok"] and r["changed"]["min_rr"] == 2.2, r
    r = client.post("/api/params", json={"risk_pct": 5.0}, headers=h).json()
    assert not r["ok"] and "risk_pct" in r["rejected"], \
        "risk_pct above the code ceiling must be rejected by the API"
    assert storage.get_setting("risk_pct") != 5.0


def test_halt_resume_via_api():
    h = {"X-Dashboard-Token": "test-token-123"}
    assert client.post("/api/halt", headers=h).json()["ok"]
    assert storage.get_setting("halt_new_entries") is True
    assert client.post("/api/resume", headers=h).json()["ok"]
    assert storage.get_setting("halt_new_entries") is False


def test_pair_toggle_via_api():
    h = {"X-Dashboard-Token": "test-token-123"}
    storage.set_setting("enabled_pairs", ["EURUSD"])
    r = client.post("/api/toggle", json={"kind": "pair", "name": "US30",
                                         "enabled": True}, headers=h).json()
    assert "US30" in r["enabled_pairs"]
    r = client.post("/api/toggle", json={"kind": "pair", "name": "US30",
                                         "enabled": False}, headers=h).json()
    assert "US30" not in r["enabled_pairs"]


def test_dashboard_binds_localhost_by_default():
    cfg = dashboard_api._load_cfg()
    assert cfg["host"] == "127.0.0.1", cfg


def test_index_serves_page():
    r = client.get("/")
    assert r.status_code == 200 and "multi-asset" in r.text


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
