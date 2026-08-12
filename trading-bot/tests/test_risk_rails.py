"""Risk-limit and circuit-breaker tests — the code that must not fail.

Covers (constraint 11 of the multi-asset brief):
  * factor-bucket exposure math + cross-asset-class stacking gate (lesson 1)
  * per-asset-class concurrency counting
  * daily/weekly loss kill switches incl. OPEN drawdown, broker-day boundary
  * consecutive-loss risk governor (halve after 3 losses until 2 wins)
  * session calendars (forex weekend closed, crypto never gated, CFD break)
  * crypto weekend half-risk factor
  * params_store write layer: whitelists, bounds, ceilings, human pin,
    refused writes logged
  * command queue: expiry + no-immediate-replay of served live commands
  * DB integrity-suspect flag blocks new entries in try_execute
  * staleness limits per asset class

Run:  cd trading-bot && python3 tests/test_risk_rails.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate all DB writes BEFORE importing modules that touch storage
import storage
storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
storage.init()

import engine
import exposure
import instruments
import params_store
import sessions

instruments.seed_defaults()

UTC_TUE_1400 = 1751378400   # 2025-07-01 14:00:00 UTC (Tuesday)
UTC_SAT_1200 = 1751716800   # 2025-07-05 12:00:00 UTC (Saturday)
UTC_SUN_2300 = 1751842800   # 2025-07-06 23:00:00 UTC (Sunday, after FX open)
UTC_FRI_2145 = 1751406300   # 2025-07-01 21:45 is Tuesday; compute Friday below
UTC_FRI_2145 = 1751665500   # 2025-07-04 21:45:00 UTC (Friday, inside cutoff)


def _wipe(table):
    storage.execute("DELETE FROM %s" % table)


def _mk_trade(symbol, side, risk_pct=1.0, mode="paper", **kw):
    tr = {"mode": mode, "trade_mode": "swing", "symbol": symbol, "side": side,
          "status": "open", "grade": "A", "entry_time": int(time.time()),
          "entry": 1.0, "sl": 0.99, "initial_sl": 0.99, "tp1": 1.01, "tp2": 1.02,
          "lots": 0.1, "risk_pct": risk_pct, "risk_amount": risk_pct * 100,
          "setup": "{}", "signal_id": 0}
    tr.update(kw)
    return tr


# ---------------------------------------------------------------- exposure
def test_bucket_exposure_math():
    open_trades = [
        _mk_trade("EURUSD", "buy"),      # {EUR:+1, USD:-1}
        _mk_trade("GER40", "buy"),       # {RISK:+1, EUR:+0.3}
        _mk_trade("XAUUSD", "buy"),      # {GOLD:+1, USD:-1}
    ]
    b = exposure.bucket_exposures(open_trades, base_risk_pct=1.0)
    assert b["USD"] == -2.0, b
    assert b["EUR"] == 1.3, b
    assert b["RISK"] == 1.0, b
    assert b["GOLD"] == 1.0, b


def test_bucket_exposure_risk_weighted():
    # a half-risk B trade counts half a unit
    b = exposure.bucket_exposures([_mk_trade("EURUSD", "buy", risk_pct=0.5)],
                                  base_risk_pct=1.0)
    assert b["EUR"] == 0.5 and b["USD"] == -0.5, b


def test_bucket_exposure_ignores_shadow():
    b = exposure.bucket_exposures([_mk_trade("EURUSD", "buy", mode="shadow")])
    assert b == {}, b


def test_cross_class_stack_blocked():
    """The 30-of-47 scenario: EUR/USD + DAX + Gold longs are ONE stacked bet.
    With two of them open, a third that pushes |USD| past the cap must block."""
    open_trades = [_mk_trade("EURUSD", "buy"), _mk_trade("XAUUSD", "buy")]
    ok, reason, snap = exposure.check_candidate(
        "GBPUSD", "buy", 1.0, open_trades, max_bucket_exposure=2.0)
    assert not ok, "USD bucket at -2.0 + another USD short must block"
    assert "USD" in reason, reason
    assert snap["projected"]["USD"] == -3.0, snap


def test_reducing_trade_never_blocked():
    # short EURUSD REDUCES a stacked short-USD book; must not be blocked
    open_trades = [_mk_trade("EURUSD", "buy"), _mk_trade("GBPUSD", "buy"),
                   _mk_trade("XAUUSD", "buy")]
    ok, reason, _ = exposure.check_candidate(
        "EURUSD", "sell", 1.0, open_trades, max_bucket_exposure=2.0)
    assert ok, reason


def test_crypto_counts_against_risk_and_usd():
    # long BTC + long NAS100 + long EURUSD share RISK/USD buckets
    open_trades = [_mk_trade("BTCUSD", "buy"), _mk_trade("NAS100", "buy")]
    b = exposure.bucket_exposures(open_trades)
    assert b["RISK"] > 1.0, b


def test_class_concurrency():
    open_trades = [_mk_trade("EURUSD", "buy"), _mk_trade("GBPUSD", "sell"),
                   _mk_trade("BTCUSD", "buy"), _mk_trade("XAUUSD", "buy"),
                   _mk_trade("ETHUSD", "buy", mode="shadow")]
    by = exposure.class_concurrency(open_trades)
    assert by == {"forex": 2, "crypto": 1, "metals": 1}, by


# ---------------------------------------------------------------- sessions
def test_forex_weekend_closed_crypto_open():
    ok_fx, why = sessions.is_market_open("EURUSD", now=UTC_SAT_1200)
    ok_cr, _ = sessions.is_market_open("BTCUSD", now=UTC_SAT_1200)
    assert not ok_fx and "closed" in why
    assert ok_cr, "crypto must never be session-gated"


def test_forex_friday_cutoff_and_sunday_reopen():
    ok, _ = sessions.is_market_open("EURUSD", now=UTC_FRI_2145)
    assert not ok, "no new FX entries after Friday 21:30 UTC"
    ok, _ = sessions.is_market_open("EURUSD", now=UTC_SUN_2300)
    assert ok, "FX reopens Sunday 22:00 UTC"
    ok, _ = sessions.is_market_open("EURUSD", now=UTC_TUE_1400)
    assert ok


def test_cfd_maintenance_break():
    # 22:05 UTC on a Tuesday: inside the index/energy maintenance break
    tue_2205 = UTC_TUE_1400 + 8 * 3600 + 300
    ok, why = sessions.is_market_open("GER40", now=tue_2205)
    assert not ok and "break" in why, (ok, why)
    ok, _ = sessions.is_market_open("EURUSD", now=tue_2205)
    assert ok, "FX has no maintenance break"


def test_crypto_weekend_half_risk():
    f, why = sessions.entry_risk_factor("BTCUSD", now=UTC_SAT_1200)
    assert f == 0.5 and "weekend" in why
    f, _ = sessions.entry_risk_factor("BTCUSD", now=UTC_TUE_1400)
    assert f == 1.0
    f, _ = sessions.entry_risk_factor("EURUSD", now=UTC_SAT_1200)
    assert f == 1.0, "weekend factor is crypto-only (FX is gated instead)"


def test_day_boundary_broker_vs_utc():
    # broker +3h offset: broker midnight is 21:00 UTC of the previous day
    day_utc, _ = sessions.day_week_start("utc", now=UTC_TUE_1400)
    day_brk, _ = sessions.day_week_start("broker", now=UTC_TUE_1400,
                                         broker_offset_s=3 * 3600)
    assert day_brk == day_utc - 3 * 3600, (day_utc, day_brk)


def test_staleness_limits_per_class():
    assert sessions.staleness_limit_s("BTCUSD", 3600) == 2 * 3600
    assert sessions.staleness_limit_s("EURUSD", 3600) == 2 * 3600
    assert sessions.fresh_limit_s("BTCUSD") < sessions.fresh_limit_s("EURUSD"), \
        "crypto feed-freshness expectation must be stricter than FX (weekends)"


# ---------------------------------------------------------------- loss limits
def test_daily_stop_counts_open_drawdown():
    _wipe("trades")
    p = {"daily_stop_pct": 2.0, "weekly_stop_pct": 5.0}
    # realized -1.5% today + open -0.6% => beyond -2% daily => halt
    storage.insert_trade(dict(_mk_trade("EURUSD", "buy"), status="closed",
                              pnl=-150.0, exit_time=int(time.time())))
    engine.feed_state["prices"]["GBPUSD"] = {
        "symbol": "GBPUSD", "bid": 0.994, "ask": 0.9941,
        "tick_value": 1.0, "tick_size": 0.0001,
        "src": engine.MT5_SOURCE, "src_t": time.time()}
    tid = storage.insert_trade(_mk_trade("GBPUSD", "buy", entry=1.0, lots=1.0))
    try:
        assert engine.loss_limits_hit("paper", 10000.0, p) is not None, \
            "open drawdown must count against the daily stop"
    finally:
        storage.update_trade(tid, {"status": "closed", "pnl": 0})
        _wipe("trades")
        engine.feed_state["prices"].pop("GBPUSD", None)


def test_daily_stop_realized_only_threshold():
    _wipe("trades")
    p = {"daily_stop_pct": 2.0, "weekly_stop_pct": 5.0}
    storage.insert_trade(dict(_mk_trade("EURUSD", "buy"), status="closed",
                              pnl=-100.0, exit_time=int(time.time())))
    assert engine.loss_limits_hit("paper", 10000.0, p) is None, \
        "-1% must not trip a -2% stop"
    storage.insert_trade(dict(_mk_trade("GBPUSD", "buy"), status="closed",
                              pnl=-110.0, exit_time=int(time.time())))
    assert engine.loss_limits_hit("paper", 10000.0, p) is not None
    _wipe("trades")


# ---------------------------------------------------------------- governor
def _close(symbol, pnl, t):
    storage.insert_trade(dict(_mk_trade(symbol, "buy"), status="closed",
                              pnl=pnl, exit_time=t))


def test_loss_governor_halves_after_3_losses():
    _wipe("trades")
    t0 = int(time.time()) - 1000
    for i, pnl in enumerate([-10, -10, -10]):
        _close("EURUSD", pnl, t0 + i)
    assert engine.loss_governor("paper") == 0.5
    # one win is not enough to restore
    _close("EURUSD", +20, t0 + 10)
    assert engine.loss_governor("paper") == 0.5
    # two consecutive wins restore full risk
    _close("EURUSD", +20, t0 + 11)
    assert engine.loss_governor("paper") == 1.0
    _wipe("trades")


def test_loss_governor_win_resets_streak():
    _wipe("trades")
    t0 = int(time.time()) - 1000
    for i, pnl in enumerate([-10, -10, +5, -10]):
        _close("EURUSD", pnl, t0 + i)
    assert engine.loss_governor("paper") == 1.0, \
        "3 losses interrupted by a win are not a 3-loss streak"
    _wipe("trades")


# ---------------------------------------------------------------- params_store
def test_agent_cannot_touch_risk_pct():
    try:
        params_store.set_param("risk_pct", 2.0, origin="agent", reason="x")
        assert False, "agent write to risk_pct must raise"
    except params_store.ParamRejected:
        pass
    row = storage.query_one(
        "SELECT * FROM param_changes WHERE key='risk_pct' AND origin='agent' "
        "ORDER BY id DESC LIMIT 1")
    assert row and row["accepted"] == 0, "refused write must still be logged"


def test_human_ceiling_on_risk_pct():
    try:
        params_store.set_param("risk_pct", 1.5, origin="human", reason="x")
        assert False, "risk_pct above the 1.0 code ceiling must be refused"
    except params_store.ParamRejected:
        pass
    v = params_store.set_param("risk_pct", 0.5, origin="human", reason="x")
    assert v == 0.5
    assert storage.get_setting("risk_pct") == 0.5


def test_bounds_enforced_for_agent_keys():
    try:
        params_store.set_param("atr_buffer", 0.9, origin="agent", reason="x")
        assert False
    except params_store.ParamRejected:
        pass
    v = params_store.set_param("atr_buffer", 0.4, origin="agent", reason="x",
                               trigger_data={"n": 40})
    assert v == 0.4


def test_human_pin_blocks_agent_counterflip():
    params_store.set_param("min_grade", "B", origin="human", reason="user choice")
    try:
        params_store.set_param("min_grade", "A", origin="agent",
                               reason="B losing again")
        assert False, "agent must not counter-flip a recent human change"
    except params_store.ParamRejected as e:
        assert "pinned" in str(e)
    row = storage.query_one(
        "SELECT * FROM param_changes WHERE key='min_grade' ORDER BY id DESC LIMIT 1")
    assert row["accepted"] == 0 and row["origin"] == "agent"


def test_trigger_data_recorded():
    params_store.set_param("min_rr", 2.1, origin="sanity", reason="grid win",
                           trigger_data={"variant": "rr2.1", "n": 44})
    row = storage.query_one(
        "SELECT * FROM param_changes WHERE key='min_rr' AND accepted=1 "
        "ORDER BY id DESC LIMIT 1")
    td = json.loads(row["trigger_data"])
    assert td["data"]["variant"] == "rr2.1", td


def test_trading_mode_not_writable_here():
    for origin in ("agent", "sanity", "human"):
        try:
            params_store.set_param("trading_mode", "live", origin=origin)
            assert False, "trading_mode must be rejected for origin " + origin
        except params_store.ParamRejected:
            pass


# ---------------------------------------------------------------- commands
def test_command_expiry():
    storage.execute("DELETE FROM commands")
    cid = storage.enqueue_command("trail_sl", {"ticket": 1, "new_sl": 1.0},
                                  ttl_s=1)
    storage.execute("UPDATE commands SET expires_at=? WHERE id=?",
                    (int(time.time()) - 5, cid))
    assert storage.next_command() is None, "expired command must not be served"
    row = storage.query_one("SELECT status FROM commands WHERE id=?", (cid,))
    assert row["status"] == "expired", row


def test_command_served_once_then_grace():
    storage.execute("DELETE FROM commands")
    storage.enqueue_command("open_trade", {"symbol": "EURUSD", "lots": 0.1})
    c1 = storage.next_command()
    assert c1 and c1["type"] == "open_trade"
    assert storage.next_command() is None, \
        "a served live command must NOT be replayed on the next poll"
    # after the grace window without an ack it becomes eligible again
    storage.execute("UPDATE commands SET sent_t=? WHERE id=?",
                    (int(time.time()) - storage.RESEND_GRACE_S - 1, int(c1["id"])))
    c2 = storage.next_command()
    assert c2 and c2["id"] == c1["id"]
    assert storage.ack_command(c2["id"])
    assert storage.next_command() is None


# ---------------------------------------------------------------- fail safe
def test_integrity_suspect_blocks_entries():
    _wipe("trades")
    _wipe("signals")
    storage._mark_suspect("test: pretend corruption")
    sig = {"symbol": "EURUSD", "trade_mode": "swing", "side": "buy",
           "grade": "A", "entry": 1.0, "sl": 0.99, "tp1": 1.01, "tp": 1.02,
           "rr": 2.0, "regime": 1.0, "setup": {}, "key": "t|fail|safe"}
    p = engine.params()
    p["trading_mode"] = "paper"
    try:
        engine.try_execute(sig, p)
        assert storage.open_trades("paper") == [], \
            "no trade may open while DB integrity is suspect"
        row = storage.query_one(
            "SELECT * FROM signals ORDER BY id DESC LIMIT 1")
        assert row and "integrity" in (row["reason"] or ""), row
    finally:
        storage._clear_suspect()
        engine._recent_keys.clear()
        _wipe("trades")
        _wipe("signals")


def test_manual_halt_blocks_entries():
    _wipe("trades")
    _wipe("signals")
    sig = {"symbol": "EURUSD", "trade_mode": "swing", "side": "buy",
           "grade": "A", "entry": 1.0, "sl": 0.99, "tp1": 1.01, "tp": 1.02,
           "rr": 2.0, "regime": 1.0, "setup": {}, "key": "t|halt|safe"}
    p = engine.params()
    p["trading_mode"] = "paper"
    p["halt_new_entries"] = True
    engine.try_execute(sig, p)
    assert storage.open_trades("paper") == []
    engine._recent_keys.clear()
    _wipe("trades")
    _wipe("signals")


# ---------------------------------------------------------------- registry
def test_unknown_symbol_still_consumes_exposure():
    meta = instruments.get("WEIRD123")
    assert meta.get("inferred") is True
    assert meta["factor_exposures"], \
        "unknown symbols must carry a default exposure, never bypass the gate"


def test_fx_inference():
    meta = instruments.classify("EURNOK") if False else instruments.classify("EURCHF")
    assert meta["asset_class"] == "forex"
    assert meta["factor_exposures"] == {"EUR": 1.0, "CHF": -1.0}


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
