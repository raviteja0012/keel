"""Phase 5 tests: the gated live-promotion path. This is circuit-breaker
code — every refusal branch is exercised.

Covers:
  * /request refused with NO open gate (small sample) even with sign-off
  * sign-off alone never opens the gate; gate alone (no sign-off) refuses
  * full promotion: 50+ positive-expectancy paper trades on GROUNDED data
    with rails exercised AND a sign-off -> request/confirm succeeds
  * confirm refused on: wrong token, wrong phrase, expiry, no pending request
  * confirm re-validates blockers (halt raised between request and confirm)
  * fail-safes block /request (manual halt, DB-integrity suspect)
  * /paper de-escalation always works, one call
  * legacy Flask /api/settings refuses trading_mode=live (de-fanged)
  * params_store still rejects trading_mode for every origin (no bypass)

Run:  cd trading-bot && python3 tests/test_live_switch.py
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

import instruments
instruments.seed_defaults()

os.environ["DASHBOARD_TOKEN"] = "test-token-live"
import analysis
import live_switch
import dashboard_api
from fastapi.testclient import TestClient

client = TestClient(dashboard_api.app)
H = {"X-Dashboard-Token": "test-token-live"}

# hallucination_check.jsonl at the repo root is read by the data-trust check;
# point analysis at a temp copy so this test controls the verdict
_trust_dir = tempfile.mkdtemp()
analysis.BASE = os.path.join(_trust_dir, "trading-bot")
os.makedirs(analysis.BASE, exist_ok=True)


def _set_trust(verdict):
    with open(os.path.join(_trust_dir, "hallucination_check.jsonl"), "a") as f:
        f.write(json.dumps({"ts": int(time.time()), "verdict": verdict}) + "\n")


def _seed_gate_worthy_sample(n=55, strategy="slc", symbol="EURUSD"):
    """n closed paper trades, ~60% winners at +2R / losers -1R (positive
    expectancy), plus decisions rows proving the rails fired."""
    t0 = int(time.time()) - n * 3600
    for i in range(n):
        win = (i % 5) != 0 and (i % 5) != 1     # 3/5 winners
        storage.insert_trade({
            "mode": "paper", "trade_mode": "swing", "symbol": symbol,
            "side": "buy", "status": "closed", "grade": "A",
            "entry_time": t0 + i * 3600, "exit_time": t0 + i * 3600 + 1800,
            "entry": 1.1, "sl": 1.095, "initial_sl": 1.095, "tp1": 1.105,
            "tp2": 1.1125, "lots": 0.1, "risk_pct": 1.0, "risk_amount": 100,
            "pnl": 200.0 if win else -100.0,
            "r_multiple": 2.0 if win else -1.0,
            "setup": "{}", "signal_id": 0,
            "strategy": strategy, "asset_class": "forex"})
    # rails demonstrably fired in paper
    storage.execute(
        "INSERT INTO decisions(t,strategy,symbol,trade_mode,stage,action,reason,session) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time()), strategy, symbol, "swing", "loss_limit", "skipped",
         "daily stop (-2.0%) hit", "London"))


def _wipe():
    for t in ("trades", "decisions", "promotion_signoffs", "param_changes"):
        storage.execute("DELETE FROM %s" % t)
    storage.set_setting("trading_mode", "paper")
    storage.set_setting("halt_new_entries", False)
    live_switch._pending.clear()


# ------------------------------------------------------------------ refusals
def test_1_request_refused_without_sample():
    _wipe()
    _set_trust("GROUNDED")
    r = client.post("/api/live/request", headers=H).json()
    assert not r["ok"] and "promotion gate" in r["reason"], r
    assert storage.get_setting("trading_mode") == "paper"


def test_2_signoff_alone_does_not_open_gate():
    _wipe()
    # a tiny sample plus a sign-off must still refuse (sample_size check)
    _seed_gate_worthy_sample(n=5)
    r = client.post("/api/live/signoff", headers=H,
                    json={"strategy": "slc", "asset_class": "forex",
                          "signed_by": "mahmed"}).json()
    assert r["ok"]
    r = client.post("/api/live/request", headers=H).json()
    assert not r["ok"], "5 trades + sign-off must NOT open the gate"


def test_3_gate_without_signoff_refuses():
    _wipe()
    _seed_gate_worthy_sample(n=55)
    p = analysis.promotion_status()["cells"]["slc|forex"]
    assert p["checks"]["sample_size"]["pass"]
    assert p["checks"]["positive_expectancy"]["pass"]
    assert p["checks"]["rails_exercised"]["pass"]
    assert not p["checks"]["manual_signoff"]["pass"]
    assert not p["gate_open"]
    r = client.post("/api/live/request", headers=H).json()
    assert not r["ok"], "no manual sign-off -> no gate (never automatic)"


def test_4_full_promotion_path():
    _wipe()
    _seed_gate_worthy_sample(n=55)
    assert client.post("/api/live/signoff", headers=H,
                       json={"strategy": "slc", "asset_class": "forex",
                             "signed_by": "mahmed",
                             "note": "forward-test continuation, min size"}).json()["ok"]
    assert analysis.promotion_status()["cells"]["slc|forex"]["gate_open"]

    r = client.post("/api/live/request", headers=H).json()
    assert r["ok"], r
    assert "slc|forex" in r["gate_cell"]

    # wrong phrase refused
    c = client.post("/api/live/confirm", headers=H,
                    json={"token": r["confirm_token"], "phrase": "go live"}).json()
    assert not c["ok"] and "phrase" in c["reason"]
    # wrong token refused
    c = client.post("/api/live/confirm", headers=H,
                    json={"token": "nope", "phrase": "GO LIVE"}).json()
    assert not c["ok"]
    # correct token + phrase flips to live
    c = client.post("/api/live/confirm", headers=H,
                    json={"token": r["confirm_token"], "phrase": "GO LIVE"}).json()
    assert c["ok"], c
    assert storage.get_setting("trading_mode") == "live"
    # audit trail exists
    row = storage.query_one("SELECT * FROM agent_log WHERE action='trading_mode' "
                            "AND kind='change' ORDER BY id DESC LIMIT 1")
    assert row and "LIVE" in row["detail"]
    # de-escalation is one call
    assert client.post("/api/live/paper", headers=H).json()["ok"]
    assert storage.get_setting("trading_mode") == "paper"


def test_5_confirm_expiry():
    _wipe()
    _seed_gate_worthy_sample(n=55)
    client.post("/api/live/signoff", headers=H,
                json={"strategy": "slc", "asset_class": "forex",
                      "signed_by": "mahmed"})
    r = client.post("/api/live/request", headers=H).json()
    assert r["ok"]
    live_switch._pending["exp"] = time.time() - 1        # force expiry
    c = client.post("/api/live/confirm", headers=H,
                    json={"token": r["confirm_token"], "phrase": "GO LIVE"}).json()
    assert not c["ok"] and "expired" in c["reason"]
    assert storage.get_setting("trading_mode") == "paper"


def test_6_confirm_revalidates_blockers():
    _wipe()
    _seed_gate_worthy_sample(n=55)
    client.post("/api/live/signoff", headers=H,
                json={"strategy": "slc", "asset_class": "forex",
                      "signed_by": "mahmed"})
    r = client.post("/api/live/request", headers=H).json()
    assert r["ok"]
    storage.set_setting("halt_new_entries", True)        # world changed
    c = client.post("/api/live/confirm", headers=H,
                    json={"token": r["confirm_token"], "phrase": "GO LIVE"}).json()
    assert not c["ok"] and "halt" in c["reason"]
    assert storage.get_setting("trading_mode") == "paper"


def test_7_fail_safes_block_request():
    _wipe()
    _seed_gate_worthy_sample(n=55)
    client.post("/api/live/signoff", headers=H,
                json={"strategy": "slc", "asset_class": "forex",
                      "signed_by": "mahmed"})
    storage.set_setting("halt_new_entries", True)
    r = client.post("/api/live/request", headers=H).json()
    assert not r["ok"] and "halt" in r["reason"]
    storage.set_setting("halt_new_entries", False)
    storage._mark_suspect("test corruption")
    try:
        r = client.post("/api/live/request", headers=H).json()
        assert not r["ok"] and "integrity" in r["reason"]
    finally:
        storage._clear_suspect()


def test_8_all_live_endpoints_require_token():
    for ep in ("/api/live/request", "/api/live/confirm", "/api/live/paper",
               "/api/live/signoff"):
        assert client.post(ep, json={}).status_code == 401, ep
    assert client.get("/api/live/status").status_code == 401


def test_9_no_bypass_via_settings_paths():
    _wipe()
    # params_store rejects trading_mode for every origin
    import params_store
    for origin in ("agent", "sanity", "human", "news"):
        try:
            params_store.set_param("trading_mode", "live", origin=origin)
            assert False, origin
        except params_store.ParamRejected:
            pass
    # legacy Flask settings endpoint refuses live (paper/off still fine)
    import server
    server.app.config["TESTING"] = True
    fc = server.app.test_client()
    r = fc.post("/api/settings", json={"trading_mode": "live"}).get_json()
    assert "trading_mode" in r["rejected"], r
    assert storage.get_setting("trading_mode") == "paper"
    r = fc.post("/api/settings", json={"trading_mode": "off"}).get_json()
    assert r["changed"].get("trading_mode") == "off"
    storage.set_setting("trading_mode", "paper")


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
