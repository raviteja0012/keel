"""Alert-taxonomy tests — the code that must not shout (ARCHITECTURE-V2 §7).

Covers:
  * tier routing: registered kinds, unknown kinds, explicit override
  * P4 rail skips are recorded and NEVER pushed (the 32-messages/hour bug)
  * dedupe: a flapping condition is one row plus a count
  * rate limiting: many distinct keys of one kind collapse to one message
  * nothing is dropped — rate-limited rows are folded or stay queued
  * digest: P3 items batched into a single message, P4 counts summarised
  * one relay owner: a foreign fresh lease makes relay_once a no-op
  * an alert recorded by a process with no notifier is still delivered
  * raise_alert never throws, and a broken relay defers instead of losing

Run:  cd trading-bot && python3 tests/test_alerts.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate all DB writes BEFORE importing modules that touch storage
import storage
storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
storage.init()

import alerts

alerts._ensure()          # the module creates its own table on first write


class Relay:
    """Stand-in for notifier.send. `broken=True` is a relay that throws."""

    def __init__(self, broken=False):
        self.sent = []
        self.broken = broken

    def __call__(self, msg):
        if self.broken:
            raise RuntimeError("telegram unreachable")
        self.sent.append(msg)


def _reset():
    storage.execute("DELETE FROM alerts")
    storage.execute("DELETE FROM settings WHERE key IN (?,?,?)",
                    (alerts._LEASE_KEY, alerts._DIGEST_DAY_KEY,
                     "alerts_quiet_hours"))
    alerts._INSTANCE = "test:%d" % time.time_ns()


def _rows(**where):
    sql = "SELECT * FROM alerts"
    args = ()
    if where.get("kind"):
        sql += " WHERE kind=?"
        args = (where["kind"],)
    return storage.query(sql + " ORDER BY id", args)


# ------------------------------------------------------------ tier routing
def test_tier_routing_matches_section_7():
    assert alerts.tier_for("recon_drift") == alerts.P1
    assert alerts.tier_for("bad_ticket") == alerts.P1, "the D1 canary stays a page"
    assert alerts.tier_for("trading_mode") == alerts.P2
    assert alerts.tier_for("news_calendar_stale") == alerts.P2
    assert alerts.tier_for("agent_change_refused") == alerts.P3
    assert alerts.tier_for("rail_skip") == alerts.P4


def test_unknown_kind_is_delivered_not_swallowed():
    _reset()
    r = Relay()
    alerts.raise_alert("something_nobody_registered", "unmapped event")
    row = _rows()[0]
    assert row["severity"] == alerts.DEFAULT_TIER == alerts.P2, row["severity"]
    alerts.relay_once(send=r)
    assert len(r.sent) == 1, "an unregistered kind must never be silently dropped"


def test_explicit_severity_overrides_registry():
    _reset()
    alerts.raise_alert("rail_skip", "normally P4", severity=alerts.P1)
    assert _rows()[0]["severity"] == alerts.P1
    _reset()
    alerts.raise_alert("rail_skip", "bad tier falls back to the registry",
                       severity="LOUD")
    assert _rows()[0]["severity"] == alerts.P4


def test_p4_rail_skips_are_logged_never_pushed():
    _reset()
    r = Relay()
    for i in range(20):
        alerts.raise_alert("rail_skip", "max concurrent (2) reached",
                           dedupe_key="EURUSD|%d" % i)
    alerts.raise_alert("kill_switch_fired", "daily stop (-2.0%) hit")
    alerts.relay_once(send=r)
    assert len(r.sent) == 1, r.sent
    assert "kill_switch_fired" in r.sent[0], r.sent[0]
    assert len(_rows(kind="rail_skip")) == 20, "skips must still be queryable"
    assert all(x["delivery"] == "log" for x in _rows(kind="rail_skip"))
    assert alerts.undelivered_count() == 0, "P4 rows must not sit in the queue"


# ------------------------------------------------------------------ dedupe
def test_dedupe_folds_a_flapping_condition_into_one_row():
    _reset()
    r = Relay()
    for _ in range(200):
        alerts.raise_alert("frozen_quote", "EURUSD quote frozen 90s",
                           dedupe_key="EURUSD")
    rows = _rows()
    assert len(rows) == 1, "200 flaps must be one row, got %d" % len(rows)
    assert rows[0]["dup_count"] == 199, rows[0]["dup_count"]
    alerts.relay_once(send=r)
    assert len(r.sent) == 1, r.sent
    assert "+199 more" in r.sent[0], r.sent[0]


def test_dedupe_is_scoped_per_kind_and_key():
    _reset()
    alerts.raise_alert("frozen_quote", "EURUSD", dedupe_key="EURUSD")
    alerts.raise_alert("frozen_quote", "GBPUSD", dedupe_key="GBPUSD")
    alerts.raise_alert("venue_unreachable", "mt5", dedupe_key="EURUSD")
    assert len(_rows()) == 3, "different keys and kinds must not collide"


def test_dedupe_window_expires_so_a_page_can_recur():
    _reset()
    first = alerts.raise_alert("clock_skew", "terminal 41s behind")
    storage.execute("UPDATE alerts SET created_t=? WHERE id=?",
                    (int(time.time()) - alerts.dedupe_window_s("clock_skew") - 10,
                     first))
    second = alerts.raise_alert("clock_skew", "terminal 41s behind")
    assert second != first, "an unresolved page must recur after its window"
    assert len(_rows()) == 2


# ----------------------------------------------------------- rate limiting
def test_rate_limit_collapses_a_burst_to_one_message():
    _reset()
    r = Relay()
    for i in range(50):
        alerts.raise_alert("recon_drift", "QTY_MISMATCH ticket %d" % i,
                           dedupe_key="t%d" % i)
    assert len(_rows()) == 50, "each distinct key is its own row"
    out = alerts.relay_once(send=r)
    assert len(r.sent) == 1, "50 events, 1 message; got %d" % len(r.sent)
    assert "x50" in r.sent[0], r.sent[0]
    assert out["delivered"] == 50 and out["sent"] == 1, out
    assert alerts.undelivered_count() == 0, "a rolled-up row is delivered, not lost"
    kinds = {x["delivery"] for x in _rows()}
    assert kinds == {"rollup", "folded"}, kinds


def test_rate_limit_is_per_kind_not_global():
    _reset()
    r = Relay()
    alerts.raise_alert("recon_drift", "drift")
    alerts.raise_alert("halt", "manual halt set")
    alerts.relay_once(send=r)
    assert len(r.sent) == 2, "two different kinds are two messages"


def test_exhausted_allowance_defers_rather_than_drops():
    _reset()
    r = Relay()
    now = int(time.time())
    limit = alerts.rate_limit_per_hour("halt")
    for i in range(limit):
        alerts.raise_alert("halt", "halt %d" % i, dedupe_key="h%d" % i)
        alerts.relay_once(send=r, now=now)
    assert len(r.sent) == limit, r.sent
    alerts.raise_alert("halt", "one too many", dedupe_key="over")
    out = alerts.relay_once(send=r, now=now)
    assert len(r.sent) == limit, "the hourly ceiling must hold"
    assert out["deferred"] == 1 and alerts.undelivered_count() == 1, out
    # an hour later the same row goes out — deferred, never discarded
    alerts.relay_once(send=r, now=now + 3601)
    assert len(r.sent) == limit + 1, r.sent
    assert alerts.undelivered_count() == 0


# ------------------------------------------------------------------ digest
def test_digest_batches_warns_into_one_message():
    _reset()
    r = Relay()
    for i in range(5):
        alerts.raise_alert("agent_change_refused",
                           "atr_buffer 0.9 out of bounds (#%d)" % i,
                           dedupe_key="agent%d" % i)
    alerts.raise_alert("promotion_progress", "slc|forex 12 of 50")
    assert alerts.relay_once(send=r)["sent"] == 0, "P3 never leaves via the relay"
    out = alerts.digest_once(send=r, force=True)
    assert out["sent"] == 1 and out["batched"] == 6, out
    assert len(r.sent) == 1, "six P3 items must be one digest, got %d" % len(r.sent)
    assert "12 of 50" in r.sent[0] and "atr_buffer" in r.sent[0]
    assert all(x["delivery"] == "digest" for x in _rows()), _rows()


def test_digest_summarises_skip_counts():
    _reset()
    r = Relay()
    for i in range(9):
        alerts.raise_alert("rail_skip", "max concurrent (2) reached",
                           dedupe_key="mc%d" % i)
    for i in range(3):
        alerts.raise_alert("rail_skip", "spread too wide", dedupe_key="sp%d" % i)
    alerts.digest_once(send=r, force=True)
    assert "9 x max concurrent (2) reached" in r.sent[0], r.sent[0]
    assert "3 x spread too wide" in r.sent[0], r.sent[0]


def test_digest_schedule_runs_once_a_day():
    _reset()
    r = Relay()
    lt = time.localtime()
    before = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                          alerts.DIGEST_HOUR - 1, 0, 0, 0, 0, -1))
    after = before + 3600
    assert not alerts.digest_due(before)
    assert alerts.digest_due(after)
    alerts.digest_once(send=r, now=after)
    assert not alerts.digest_due(after), "one digest per day"
    assert alerts.digest_once(send=r, now=after)["skipped"] == "not due"


def test_quiet_hours_hold_p2_but_never_a_page():
    _reset()
    r = Relay()
    storage.set_setting("alerts_quiet_hours", [0, 24])   # always quiet
    try:
        alerts.raise_alert("trading_mode", "paper -> live")
        alerts.raise_alert("bad_ticket", "ticket -1 in the feed")
        alerts.relay_once(send=r)
        assert len(r.sent) == 1 and "bad_ticket" in r.sent[0], r.sent
        assert alerts.undelivered_count() == 1, "the P2 waits, it is not dropped"
    finally:
        storage.execute("DELETE FROM settings WHERE key='alerts_quiet_hours'")


# ------------------------------------------------------------- one relay
def test_process_without_a_notifier_still_gets_its_alert_delivered():
    """The live_switch bug: dashboard_api never calls notifier.start(), so
    the message announcing real money at risk was queued into a process with
    no worker. Recording is enough; the relay owner delivers."""
    _reset()
    dashboard = "dashboard_api:%d" % time.time_ns()
    server = "server:%d" % time.time_ns()

    alerts._INSTANCE = dashboard                     # no notifier in this process
    aid = alerts.raise_alert("trading_mode",
                             "LIVE TRADING ENABLED via two-step switch")
    assert aid and alerts.undelivered_count() == 1

    alerts._INSTANCE = server                        # the one relay owner
    r = Relay()
    assert alerts.own_relay()
    out = alerts.relay_once(send=r)
    assert out["sent"] == 1, out
    assert "LIVE TRADING ENABLED" in r.sent[0], r.sent[0]
    assert alerts.undelivered_count() == 0


def test_only_one_process_relays():
    _reset()
    owner, intruder = "server:1", "news_agent:1"
    alerts._INSTANCE = owner
    assert alerts.own_relay()
    alerts.raise_alert("halt", "manual halt set")

    alerts._INSTANCE = intruder
    r = Relay()
    out = alerts.relay_once(send=r)
    assert out["sent"] == 0 and "lease" in out["skipped"], out
    assert r.sent == [], "a second relay means duplicate Telegram messages"
    assert alerts.digest_once(send=r, force=True)["sent"] == 0

    alerts._INSTANCE = owner
    assert alerts.relay_once(send=r)["sent"] == 1


def test_lease_passes_to_a_new_owner_when_the_old_one_dies():
    _reset()
    alerts._INSTANCE = "server:dead"
    assert alerts.own_relay()
    storage.set_setting(alerts._LEASE_KEY,
                        {"instance": "server:dead",
                         "t": time.time() - alerts.RELAY_LEASE_S - 1})
    alerts._INSTANCE = "server:restarted"
    assert alerts.own_relay(), "an expired lease must be claimable"
    assert alerts.relay_owner()["is_me"]


# ------------------------------------------------------------- never throw
def test_raise_never_throws_when_storage_is_broken():
    _reset()
    real = storage.execute

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    storage.execute = boom
    try:
        assert alerts.raise_alert("kill_switch_fired", "daily stop hit") == 0
    finally:
        storage.execute = real
    # and it still works afterwards
    assert alerts.raise_alert("kill_switch_fired", "daily stop hit") > 0


def test_raise_swallows_a_broken_relay():
    _reset()
    broken = Relay(broken=True)
    aid = alerts.raise_alert("recon_drift", "QTY_MISMATCH ticket 7")
    assert aid > 0, "recording must not depend on the relay being up"
    out = alerts.relay_once(send=broken)          # must not propagate
    assert out["sent"] == 0 and out["deferred"] == 1, out
    assert alerts.undelivered_count() == 1, "a failed send is a retry, not a loss"
    good = Relay()
    assert alerts.relay_once(send=good)["sent"] == 1
    assert alerts.undelivered_count() == 0


def test_digest_survives_a_broken_relay():
    _reset()
    alerts.raise_alert("agent_change_refused", "risk_pct 2.0 refused")
    out = alerts.digest_once(send=Relay(broken=True), force=True)
    assert out["sent"] == 0 and "retry" in out["skipped"], out
    assert alerts.undelivered_count() == 1, "an undelivered digest keeps its rows"
    assert alerts.digest_once(send=Relay(), force=True)["batched"] == 1


# ------------------------------------------------------------------ misc
def test_schema_is_additive_and_idempotent():
    cols = [r["name"] for r in storage.query("PRAGMA table_info(alerts)")]
    for required in ("severity", "kind", "message", "dedupe_key", "created_t",
                     "delivered_t", "acknowledged_t"):
        assert required in cols, required
    alerts._ready = False
    alerts._ensure()                       # re-running must not lose anything
    assert [r["name"] for r in storage.query("PRAGMA table_info(alerts)")] == cols


def test_acknowledge_and_health():
    _reset()
    aid = alerts.raise_alert("clock_skew", "terminal 41s behind")
    h = alerts.health()
    assert h["undelivered_alerts"] == 1 and h["unacknowledged_pages"] == 1, h
    assert alerts.acknowledge(aid)
    assert alerts.health()["unacknowledged_pages"] == 0


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
