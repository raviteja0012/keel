"""End-to-end paper execution through the shared rails: a valid candidate
must open a paper trade carrying strategy + asset_class attribution, write an
'executed' decision with the exposure snapshot, and consume bucket budget
that then blocks a stacked same-bet candidate.

Run:  cd trading-bot && python3 tests/test_execution_flow.py
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

instruments.seed_defaults()
# pin the session gate open (time-injected session tests live in test_risk_rails)
engine.sessions.is_market_open = lambda symbol, now=None: (True, "")
# mark the news calendar fresh so its stale-halt fail-safe (tested in
# test_news_regime.py) doesn't block these execution-flow tests
storage.set_setting("news_calendar_last_ok", int(time.time()))


def _price(symbol, bid, ask, tick=0.0001):
    engine.feed_state["prices"][symbol] = {
        "symbol": symbol, "bid": bid, "ask": ask,
        "tick_value": 1.0, "tick_size": tick, "spread": 1, "point": tick}


def _sig(symbol, side="buy", entry=1.1000, sl=1.0950, key=None):
    tp = entry + 2.5 * (entry - sl) if side == "buy" else entry - 2.5 * (sl - entry)
    return {"symbol": symbol, "trade_mode": "swing", "side": side,
            "grade": "A", "entry": entry, "sl": sl,
            "tp1": entry + (entry - sl), "tp": round(tp, 5), "rr": 2.5,
            "regime": 1.0, "setup": {"poi": {"lo": sl, "hi": entry}},
            "strategy": "slc", "key": key or "t|%s|%s" % (symbol, side)}


def _params(**over):
    p = engine.params()
    p.update({"trading_mode": "paper", "paper_balance": 10000.0,
              "max_concurrent": 5, "max_concurrent_per_class": 5,
              "max_correlated": 5, "max_bucket_exposure": 2.0,
              "risk_pct": 1.0, "min_rr": 2.0, "max_spread_frac": 0.10,
              "halt_new_entries": False})
    p.update(over)
    return p


def test_1_paper_trade_opens_with_attribution():
    _price("EURUSD", 1.1000, 1.10008)
    engine.try_execute(_sig("EURUSD"), _params())
    trs = storage.open_trades("paper")
    assert len(trs) == 1, [t["symbol"] for t in trs]
    tr = trs[0]
    assert tr["strategy"] == "slc" and tr["asset_class"] == "forex", tr
    assert 0 < tr["risk_pct"] <= 1.0
    d = storage.query_one(
        "SELECT * FROM decisions WHERE action='executed' ORDER BY id DESC LIMIT 1")
    assert d and d["symbol"] == "EURUSD" and d["trade_id"] == tr["id"]
    snap = json.loads(d["checks"])
    assert "projected" in snap and snap["projected"]["USD"] == -1.0, snap
    assert d["session"], "session label must be stamped on the decision"


def test_2_stacked_bet_blocked_and_audited():
    # open GBPUSD too -> USD bucket at -2.0 (cap). XAUUSD long (also short-USD)
    # must be blocked by the bucket gate and audited with the snapshot.
    _price("GBPUSD", 1.2700, 1.27008)
    engine.try_execute(_sig("GBPUSD", entry=1.27008, sl=1.2650), _params())
    assert len(storage.open_trades("paper")) == 2, \
        [t["symbol"] for t in storage.open_trades("paper")]

    _price("XAUUSD", 3300.0, 3300.3, tick=0.01)
    engine.try_execute(_sig("XAUUSD", entry=3300.3, sl=3290.0,
                            key="t|XAUUSD|buy"), _params())
    trs = storage.open_trades("paper")
    assert len(trs) == 2, "XAUUSD long must be blocked by the USD bucket cap"
    d = storage.query_one(
        "SELECT * FROM decisions WHERE stage='exposure' AND action='skipped' "
        "ORDER BY id DESC LIMIT 1")
    assert d and d["symbol"] == "XAUUSD" and "USD" in d["reason"], d
    # blocked-but-valid setups are shadow-tracked so the gate's cost is priced
    assert any(t["symbol"] == "XAUUSD" for t in storage.open_trades("shadow"))


def test_3_loss_governor_applies_to_sizing():
    # 3 consecutive losses -> next fill risks half
    t0 = int(time.time()) - 100
    for i in range(3):
        storage.insert_trade({
            "mode": "paper", "trade_mode": "swing", "symbol": "USDJPY",
            "side": "buy", "status": "closed", "grade": "A",
            "entry_time": t0 + i, "exit_time": t0 + i + 1, "entry": 1, "sl": 1,
            "initial_sl": 1, "tp1": 1, "tp2": 1, "lots": 0.1,
            "risk_pct": 1.0, "risk_amount": 100, "pnl": -10,
            "setup": "{}", "signal_id": 0})
    _price("AUDUSD", 0.6600, 0.66004)
    engine.try_execute(_sig("AUDUSD", entry=0.66004, sl=0.6570,
                            key="t|AUDUSD|buy"),
                       _params(max_bucket_exposure=4.0))
    tr = next(t for t in storage.open_trades("paper") if t["symbol"] == "AUDUSD")
    assert tr["risk_pct"] <= 0.51, \
        "risk must be halved after 3 consecutive losses (got %s)" % tr["risk_pct"]
    setup = json.loads(tr["setup"])
    assert any("halved" in n for n in setup.get("risk_notes", [])), setup


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print("\n%d passed" % len(fns))
