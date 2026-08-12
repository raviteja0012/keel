"""Safety wiring — the pager, the reconciler and the dead-man, CONNECTED.

alerts.py and reconcile.py were finished, tested and imported by nothing
outside their own suites. These tests are about the wiring, not the modules:
every assertion here is about a condition in the world producing an alert row
of the right severity, a halt that actually lands in settings, or an endpoint
that refuses to describe an unknown as a clean.

Covers:
  S1  kill switch fired            -> P1, in paper and live alike
      balance/valuation UNKNOWN    -> alerted, never reported as "clear"
      live mode enabled            -> P1, recorded by a process with no
                                      notifier and delivered by the relay owner
                                      (the live_switch bug, from the other end)
      DB integrity latched suspect -> P1
      venue holding a position dark-> P1; dark while flat -> P2, not a page
  S2  material drift               -> new entries halted + P1, nothing healed
      unreachable venue            -> UNKNOWN halts; it is not "we are flat"
      flat node with no sources    -> does NOT latch a halt forever
  S3  no heartbeat / stale one     -> P1 by itself, no dashboard required
      dashboard process            -> sees a dead server process
  S4  /api/alerts, /api/reconcile  -> reads open, ack token-gated, and the
                                      report view never renders "no report"
                                      as a clean book

Run:  cd trading-bot && python3 tests/test_safety_wiring.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate every DB write BEFORE importing anything that touches storage
import storage
storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
storage.init()

os.environ["DASHBOARD_TOKEN"] = "test-token-safety"

import alerts
import brokers
import engine
import instruments
import notifier
import params_store
import reconcile
import server
import venues
import dashboard_api
from fastapi.testclient import TestClient

instruments.seed_defaults()
client = TestClient(dashboard_api.app)          # no lifespan -> no threads
TOKEN = {"X-Dashboard-Token": "test-token-safety"}


class _AngryVenue:
    """A venue whose position read fails with a message that echoes the key
    that signed the request — which is what an exchange returning the rejected
    request looks like, and how a credential gets into a Telegram message."""

    def __init__(self, cfg):
        self.name = cfg["name"]
        self.read_only = bool(cfg.get("read_only", True))
        self._key = cfg.get("api_key", "")

    def health(self):
        return brokers.VenueHealth(name=self.name, reachable=False,
                                   authenticated=False, read_only=self.read_only)

    def symbol_meta(self, symbol):
        raise NotImplementedError

    def balances(self):
        return []

    def positions(self):
        raise brokers.VenueError(
            "401 unauthorized: rejected request apiKey=%s" % self._key,
            venue=self.name)

    def place_order(self, order):
        raise AssertionError("reconcile placed an order")

    def cancel(self, venue_order_id, symbol=""):
        raise AssertionError("reconcile cancelled an order")

    def stream_prices(self, symbols):
        return iter(())


brokers.register("angry", _AngryVenue)


class Relay:
    """Stand-in for notifier.send. `broken=True` throws like a dead Telegram."""

    def __init__(self, broken=False):
        self.sent = []
        self.broken = broken

    def __call__(self, msg):
        if self.broken:
            raise RuntimeError("telegram unreachable")
        self.sent.append(msg)

    def text(self):
        return "\n".join(self.sent)


# ------------------------------------------------------------------ fixtures
def _wipe(table):
    storage.execute("DELETE FROM %s" % table)


def _reset(started_ago=10000):
    alerts._ensure()
    for t in ("alerts", "trades", "commands", "param_changes", "agent_log"):
        _wipe(t)
    storage.execute(
        "DELETE FROM settings WHERE key IN "
        "('engine_heartbeat_t','safety_monitor_t','safety_last_seen',"
        "'recon_last_report','halt_new_entries','mt5_enabled','ea_last_feed_t',"
        "'venues','alerts_relay_owner','alerts_last_digest_day',"
        "'alerts_quiet_hours')")
    venues._cache.clear()                        # the "venues" row was deleted
    venues._cache_stamp.clear()
    storage.set_setting("trading_mode", "paper")
    storage.set_setting("paper_balance", 10000.0)
    storage.set_setting("daily_stop_pct", 2.0)
    storage.set_setting("weekly_stop_pct", 5.0)
    alerts._INSTANCE = "test-server:%d" % time.time_ns()
    server._safety.update({"started_t": time.time() - started_ago,
                           "last_reconcile_t": 0.0, "errors": 0,
                           "last_error": "", "ticks": 0})
    dashboard_api._watch.update({"started_t": time.time() - started_ago,
                                 "ticks": 0})
    engine.feed_state["prices"].clear()
    engine.feed_state["sources"] = {}
    engine.feed_state["open_positions"] = []
    engine.feed_state["account"] = {}
    engine.feed_state["last_feed_t"] = 0


def _feed(open_positions=None, balance=10000.0, age_s=0.0):
    """Push a drop copy through the REAL EA entry point, then age it."""
    engine.ingest_feed({"account": {"balance": balance, "equity": balance},
                        "open_positions": open_positions or [],
                        "prices": [], "terminal": {}})
    if age_s:
        engine.feed_state["last_feed_t"] -= age_s
        engine.feed_state["sources"]["mt5"]["last_t"] -= age_s


def _mk_trade(mode="live", **kw):
    tr = {"mode": mode, "trade_mode": "swing", "symbol": "EURUSD",
          "side": "buy", "status": "open", "grade": "A",
          "entry_time": int(time.time()), "entry": 1.1000, "sl": 1.0950,
          "initial_sl": 1.0950, "tp1": 1.1050, "tp2": 1.1100, "lots": 0.10,
          "risk_pct": 1.0, "risk_amount": 100.0, "setup": "{}", "signal_id": 0}
    tr.update(kw)
    return storage.insert_trade(tr)


def _pos(ticket=7001, symbol="EURUSD", side="buy", lots=0.10, sl=1.0950):
    return {"ticket": ticket, "symbol": symbol, "side": side, "lots": lots,
            "entry": 1.1000, "sl": sl, "unrealized_pnl": 0.0,
            "magic": server.MT5_MAGIC}


def _rows(kind=None):
    if kind:
        return storage.query("SELECT * FROM alerts WHERE kind=? ORDER BY id",
                             (kind,))
    return storage.query("SELECT * FROM alerts ORDER BY id")


def _one(kind):
    rows = _rows(kind)
    assert len(rows) == 1, "expected exactly one %s alert, got %d: %s" % (
        kind, len(rows), [r["message"][:60] for r in rows])
    return rows[0]


# ==================================================================== S1
def test_s1_kill_switch_fired_is_a_page():
    """-3% realised on a 2% daily stop. Invariant 9 is a hard stop in paper
    AND live, so paper pages too."""
    _reset()
    _mk_trade(mode="paper", status="closed", pnl=-300.0, r_multiple=-3.0,
              exit_time=int(time.time()))
    assert server.check_kill_switches(engine.params()) == "fired"
    row = _one("kill_switch_fired")
    assert row["severity"] == alerts.P1, row["severity"]
    assert "daily stop" in row["message"], row["message"]
    assert "paper" in row["message"], "the alert must say which book fired"


def test_s1_unvaluable_position_is_never_reported_as_clear():
    """engine.loss_limits_hit returns None both when nothing is wrong and when
    the balance is unreadable, and it reports "cannot value" when a position
    has no quote. Neither may surface here as "kill switches clear"."""
    _reset()
    _mk_trade(mode="paper", symbol="ZZZUSD")        # open, and no quote for it
    assert server.check_kill_switches(engine.params()) == "unknown"
    row = _one("kill_switch_unknown")
    assert "cannot value" in row["message"], row["message"]
    assert row["severity"] == alerts.P2, "a paper valuation gap is not a page"

    # the same gap with real money open is a page
    _reset()
    storage.set_setting("trading_mode", "live")
    _feed(open_positions=[])
    _mk_trade(mode="live", symbol="ZZZUSD", ticket=4242)
    assert server.check_kill_switches(engine.params()) == "unknown"
    assert _one("kill_switch_unknown")["severity"] == alerts.P1

    # and a live account whose balance cannot be read at all is not "no limit"
    _reset()
    storage.set_setting("trading_mode", "live")
    _feed(balance=0.0)
    _mk_trade(mode="live", ticket=4243)
    assert server.check_kill_switches(engine.params()) == "unknown"
    assert _one("kill_switch_unknown")["severity"] == alerts.P1


def test_s1_live_mode_enable_pages_and_reaches_a_notifier_less_process():
    """The live_switch bug from the other end.

    live_switch.confirm_live flips the mode with storage.set_setting and queues
    its notice into notifier from the dashboard process, which never calls
    notifier.start() — so the one message announcing real money at risk is
    enqueued into a process with no worker and never leaves the box. Here the
    flip is observed as STATE, recorded by a process with no notifier at all,
    and delivered by whoever holds the relay lease.
    """
    _reset()
    server.check_trading_mode()                      # baseline: paper
    dashboard = "dashboard_api:%d" % time.time_ns()
    alerts._INSTANCE = dashboard

    storage.set_setting("trading_mode", "live")      # exactly what confirm does
    server.check_trading_mode()

    row = _one("trading_mode")
    assert row["severity"] == alerts.P1, (
        "the registry tiers trading_mode P2, which quiet hours may hold; the "
        "paper->live edge must be a page: %s" % row["severity"])
    assert "LIVE" in row["message"], row["message"]
    assert row["delivered_t"] is None, "recording must not attempt delivery"
    assert notifier._started is False, (
        "this process is standing in for dashboard_api and must have no "
        "notifier worker — the whole point of the relay split")

    alerts._INSTANCE = "server:%d" % time.time_ns()  # the relay owner
    r = Relay()
    out = alerts.relay_once(send=r)
    assert out["sent"] == 1, out
    assert "LIVE" in r.text(), r.sent
    assert alerts.undelivered_count() == 0


def test_s1_mode_transition_is_reported_once_not_every_cycle():
    _reset()
    server.check_trading_mode()
    storage.set_setting("trading_mode", "live")
    server.check_trading_mode()
    for _ in range(5):
        server.check_trading_mode()
    row = _one("trading_mode")
    assert row["dup_count"] == 0, (
        "still-live is not a new event; a pager that repeats itself every "
        "cycle is a pager that gets muted (dup_count=%s)" % row["dup_count"])


def test_s1_halt_flag_transition_is_reported():
    _reset()
    server.check_trading_mode()
    server.apply_safety_halt("test")
    server.check_trading_mode()
    assert _one("halt")["severity"] == alerts.P2


def test_s1_db_integrity_suspect_is_a_page():
    _reset()
    storage._mark_suspect("database disk image is malformed")
    try:
        assert server.check_db_integrity(engine.params())
        row = _one("db_integrity_live")
        assert row["severity"] == alerts.P1
        assert "malformed" in row["message"]
    finally:
        storage._clear_suspect()
    assert server.check_db_integrity(engine.params()) == ""


def test_s1_venue_holding_a_position_going_dark_is_a_page():
    _reset()
    _feed(open_positions=[_pos()], age_s=600)        # EA silent for 10 minutes
    _mk_trade(mode="live", ticket=7001)
    assert server.check_mt5_reachable() == "unreachable_holding_positions"
    row = _one("feed_loss_live")
    assert row["severity"] == alerts.P1
    assert "live position" in row["message"], row["message"]


def test_s1_venue_going_dark_while_flat_is_not_a_page():
    """A rail that pages when nothing is at stake is a rail that gets muted."""
    _reset()
    _feed(age_s=600)
    assert server.check_mt5_reachable() == "unreachable"
    assert _rows("feed_loss_live") == [], "no position, no page"
    assert _one("feed_stale")["severity"] == alerts.P2


def test_s1_no_mt5_deployment_is_not_a_dead_venue():
    _reset()
    assert server.check_mt5_reachable() == "not configured"
    assert _rows() == [], "a node with no EA must not alert about the EA"


# ==================================================================== S2
def test_s2_material_drift_halts_entries_and_pages():
    _reset()
    _feed(open_positions=[])                     # venue: we hold nothing
    _mk_trade(mode="live", ticket=7001)          # book: we hold EURUSD
    out = server.reconcile_tick(force=True)
    assert out["halt"] is True, out
    assert out["counts"].get(reconcile.LOCAL_ONLY) == 1, out["counts"]
    assert storage.get_setting("halt_new_entries") is True, \
        "a material discrepancy must stop new entries, not just complain"
    row = _one("recon_drift")
    assert row["severity"] == alerts.P1
    assert "LOCAL_ONLY" in row["message"], row["message"]


def test_s2_halt_is_written_through_the_write_layer():
    """params_store, not storage.set_setting: the halt lands in param_changes
    with its old/new values like every other parameter write."""
    _reset()
    _feed(open_positions=[])
    _mk_trade(mode="live", ticket=7001)
    server.reconcile_tick(force=True)
    rows = storage.query("SELECT * FROM param_changes WHERE key=? ORDER BY id",
                         ("halt_new_entries",))
    assert len(rows) == 1 and rows[0]["accepted"] == 1, rows
    assert rows[0]["new"] == "true", rows[0]["new"]
    assert "reconciliation" in (rows[0]["trigger_data"] or ""), rows[0]

    # and the origin tells the truth as soon as the write layer allows it to
    assert server._halt_origin() == "human", (
        "params_store has no automated origin for this key today")
    params_store.WHITELISTS["system"] = {"halt_new_entries"}
    try:
        assert server._halt_origin() == "system", (
            "the moment params_store whitelists an automated origin, the "
            "audit row must stop claiming a human did this")
    finally:
        params_store.WHITELISTS.pop("system", None)


def test_s2_reconciler_never_heals():
    """It classifies and reports. Placing, closing or re-stopping anything
    would make it a second execution path beside engine.py."""
    _reset()
    _feed(open_positions=[])
    tid = _mk_trade(mode="live", ticket=7001)
    before = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    server.reconcile_tick(force=True)
    after = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    for col in ("status", "sl", "lots", "entry", "tp1", "tp2", "exit_time",
                "exit_price", "pnl"):
        assert before[col] == after[col], "reconcile changed trades.%s" % col
    assert storage.query_one("SELECT COUNT(*) n FROM commands")["n"] == 0, \
        "reconcile queued a command for the EA"

    # and it never lifts its own halt: the book agreeing again is not consent
    _feed(open_positions=[_pos(ticket=7001)])
    out = server.reconcile_tick(force=True)
    assert out["halt"] is False, out
    assert storage.get_setting("halt_new_entries") is True, \
        "auto-resume would make the halt advisory; resuming is a human act"


def test_s2_unreachable_venue_is_not_read_as_flat():
    """"the venue did not answer" and "the venue confirms we are flat" are the
    same silence over the wire, and only one of them is safe to trade on."""
    _reset()
    storage.set_setting("mt5_enabled", True)     # configured, and silent
    _mk_trade(mode="live", ticket=7001)
    out = server.reconcile_tick(force=True)
    assert out["halt"] is True, out
    assert out["counts"].get(reconcile.UNKNOWN), out["counts"]
    assert reconcile.LOCAL_ONLY not in out["counts"], \
        "a blind source must degrade to UNKNOWN, never to 'it is gone'"
    assert storage.get_setting("halt_new_entries") is True
    row = _one("venue_unreachable")
    assert row["severity"] == alerts.P1, "unreachable while holding = page"


def test_s2_stale_drop_copy_is_blindness_not_evidence():
    """A dead feed's last frame must not masquerade as the venue's book: the
    positions it still lists would otherwise reconcile perfectly forever."""
    _reset()
    _feed(open_positions=[_pos(ticket=7001)], age_s=600)
    _mk_trade(mode="live", ticket=7001)
    out = server.reconcile_tick(force=True)
    assert out["halt"] is True, out
    assert out["counts"].get(reconcile.UNKNOWN), out["counts"]


def test_s2_flat_paper_node_with_no_sources_does_not_latch_a_halt():
    """The other failure mode: a rail that fires forever is a rail somebody
    switches off. A paper node with no EA and no venues has nothing to
    reconcile, and must not halt itself into silence."""
    _reset()
    out = server.reconcile_tick(force=True)
    assert out["halt"] is False, out
    assert storage.get_setting("halt_new_entries", False) is False
    assert _rows("recon_drift") == []


def test_s2_paper_book_is_not_reconciled_against_a_venue():
    """Paper trades were simulated inside engine.py and never sent anywhere."""
    _reset()
    _feed(open_positions=[])
    _mk_trade(mode="paper", ticket=0)
    out = server.reconcile_tick(force=True)
    assert out["halt"] is False, out
    assert storage.get_setting("halt_new_entries", False) is False


def test_s2_a_venue_error_cannot_carry_a_credential_into_a_page():
    """venues.positions_all() stores str(e) from the adapter, and from here
    that text travels into an alert message (Telegram) and a persisted report.
    Constraint 3: credentials live in the runtime DB and nowhere else."""
    _reset()
    secret = "SECRET-EXCHANGE-KEY-123456"
    venues.upsert({"name": "angryx", "kind": "angry", "api_key": secret,
                   "read_only": False})
    try:
        _mk_trade(mode="live", ticket=7001)
        out = server.reconcile_tick(force=True)
        assert out["halt"] is True, out
        blob = (json.dumps(storage.get_setting("recon_last_report"))
                + json.dumps(_rows(), default=str))
        assert secret not in blob, "a venue credential reached the pager"
        assert "401 unauthorized" in blob, (
            "over-masking: the operator still needs to know what the venue "
            "said, or the page is a shrug")
    finally:
        venues.remove("angryx")


def test_s2_report_is_stored_and_keeps_its_verdict():
    _reset()
    _feed(open_positions=[])
    _mk_trade(mode="live", ticket=7001)
    server.reconcile_tick(force=True)
    stored = storage.get_setting("recon_last_report")
    assert isinstance(stored, dict) and stored.get("t"), stored
    assert reconcile.should_halt_entries(stored) is True, \
        "the trimmed report must answer the gate exactly as the full one did"


def test_s2_respects_its_own_cadence():
    _reset()
    server.reconcile_tick(force=True)
    assert server.reconcile_tick()["ran"] is False, "a network read per 15s tick"


# ==================================================================== S3
def test_s3_missing_heartbeat_is_not_alive():
    """The absence of a heartbeat must produce the alert by itself. `hb or
    now` here would make a process that never started look eternally healthy."""
    _reset()
    assert storage.get_setting("engine_heartbeat_t", None) is None
    assert server.check_engine_heartbeat() == "never"
    row = _one("engine_deadman")
    assert row["severity"] == alerts.P1
    assert "EVER" in row["message"]


def test_s3_zero_heartbeat_is_missing_not_epoch():
    _reset()
    storage.set_setting("engine_heartbeat_t", 0)
    assert server.check_engine_heartbeat() == "never"
    assert _one("engine_deadman")["severity"] == alerts.P1


def test_s3_stale_heartbeat_pages_and_a_fresh_one_does_not():
    _reset()
    now = time.time()
    storage.set_setting("engine_heartbeat_t", int(now))
    assert server.check_engine_heartbeat(now) == "alive"
    assert _rows("engine_deadman") == []
    storage.set_setting("engine_heartbeat_t", int(now - 1000))
    assert server.check_engine_heartbeat(now) == "stalled"
    row = _one("engine_deadman")
    assert row["severity"] == alerts.P1
    assert "1000s old" in row["message"], row["message"]


def test_s3_deadman_severity_is_explicit_not_inherited():
    """engine_deadman is not in alerts.KINDS, so the registry would tier it P2
    — deliverable, but holdable through quiet hours. It is passed P1
    explicitly, and this is the test that notices if that is ever dropped."""
    _reset()
    assert alerts.tier_for("engine_deadman") == alerts.P2
    server.check_engine_heartbeat()
    assert _one("engine_deadman")["severity"] == alerts.P1


def test_s3_a_just_started_process_does_not_page_about_a_cold_engine():
    _reset(started_ago=0)
    assert server.check_engine_heartbeat() == "warming up"
    assert _rows("engine_deadman") == []


def test_s3_dashboard_process_sees_a_dead_server_process():
    """The arm the in-process watchdog cannot be: a separate process saying
    'server.py is gone'."""
    _reset()
    now = time.time()
    storage.set_setting("engine_heartbeat_t", int(now - 1000))
    storage.set_setting("safety_monitor_t", int(now - 1000))
    r = Relay()
    out = dashboard_api.deadman_tick(now=now, send=r)
    assert out["engine"] == "stalled" and out["monitor"] == "stalled", out
    assert _one("engine_deadman")["severity"] == alerts.P1
    assert _one("safety_monitor_down")["severity"] == alerts.P1
    assert r.sent, "the standby relay must deliver when nobody else holds it"
    assert alerts.undelivered_count() == 0


def test_s3_dashboard_reports_liveness_without_guessing():
    _reset()
    st = dashboard_api.deadman_state()
    assert st["engine_heartbeat_age_s"] is None and st["engine_alive"] is False, \
        "no heartbeat is not an age of zero and is not alive"
    storage.set_setting("engine_heartbeat_t", int(time.time()))
    assert dashboard_api.deadman_state()["engine_alive"] is True


def test_s3_standby_relay_does_not_steal_a_live_lease():
    """Two relays means two Telegram messages per event — alert fatigue
    arriving through its own fix."""
    _reset()
    storage.set_setting("engine_heartbeat_t", int(time.time()))
    storage.set_setting("safety_monitor_t", int(time.time()))
    storage.set_setting(alerts._LEASE_KEY,
                        {"instance": "server-process:live", "t": time.time()})
    alerts.raise_alert("halt", "something to deliver")
    r = Relay()
    out = dashboard_api.deadman_tick(send=r)
    assert out["relay"]["sent"] == 0 and "lease" in out["relay"]["skipped"], out
    assert r.sent == []
    # ...and it does take over once the owner stops renewing
    storage.set_setting(alerts._LEASE_KEY,
                        {"instance": "server-process:dead",
                         "t": time.time() - alerts.RELAY_LEASE_S - 1})
    assert dashboard_api.deadman_tick(send=r)["relay"]["sent"] >= 1


# ==================================================================== S4
def test_s4_alerts_endpoint_is_open_and_lists_rows():
    _reset()
    alerts.raise_alert("kill_switch_fired", "daily stop (-2.0%) hit",
                       severity=alerts.P1)
    alerts.raise_alert("rail_skip", "max concurrent (2) reached")
    body = client.get("/api/alerts").json()
    kinds = {a["kind"] for a in body["alerts"]}
    assert {"kill_switch_fired", "rail_skip"} <= kinds, kinds
    assert body["health"]["unacknowledged_pages"] == 1, body["health"]
    p1 = client.get("/api/alerts?severity=P1").json()["alerts"]
    assert [a["kind"] for a in p1] == ["kill_switch_fired"], p1


def test_s4_alert_ack_is_token_gated():
    _reset()
    aid = alerts.raise_alert("kill_switch_fired", "daily stop hit",
                             severity=alerts.P1)
    assert client.post("/api/alerts/%d/ack" % aid).status_code == 401
    assert client.post("/api/alerts/%d/ack" % aid,
                       headers={"X-Dashboard-Token": "wrong"}).status_code == 401
    row = storage.query_one("SELECT * FROM alerts WHERE id=?", (aid,))
    assert row["acknowledged_t"] is None, "401 must not have acknowledged it"
    assert client.post("/api/alerts/%d/ack" % aid, headers=TOKEN).json()["ok"]
    assert alerts.health()["unacknowledged_pages"] == 0


def test_s4_reconcile_endpoint_never_reads_as_clean_without_a_report():
    _reset()
    body = client.get("/api/reconcile").json()
    assert body["report"] is None and body["state"] == "never run", body
    assert body["halt_if_consumed_now"] is True, (
        "a verdict we do not have is not a clean one: %s" % body)


def test_s4_reconcile_endpoint_renders_the_stored_report():
    _reset()
    _feed(open_positions=[])
    _mk_trade(mode="live", ticket=7001)
    server.reconcile_tick(force=True)
    body = client.get("/api/reconcile").json()
    assert body["state"] == "current" and body["age_s"] is not None, body
    assert body["halt_if_consumed_now"] is True
    assert "LOCAL_ONLY" in body["halt_reason"], body["halt_reason"]
    assert body["halt_new_entries"] is True


def test_s4_reconcile_endpoint_flags_a_stale_report():
    _reset()
    storage.set_setting("recon_last_report",
                        {"t": int(time.time()) - 3600, "discrepancies": [],
                         "halt": False})
    body = client.get("/api/reconcile").json()
    assert body["state"] == "stale", body


def test_s4_health_carries_the_pager_backlog():
    _reset()
    alerts.raise_alert("recon_drift", "QTY_MISMATCH ticket 7",
                       severity=alerts.P1)
    h = client.get("/api/health").json()
    assert h["alerts"]["undelivered_alerts"] == 1, h["alerts"]
    assert h["deadman"]["engine_alive"] is False


def test_s4_alert_endpoints_leak_no_credentials():
    _reset()
    storage.set_setting("telegram_bot_token", "SECRET-TG-XYZ")
    try:
        alerts.raise_alert("venue_unreachable", "binance is unreachable",
                           detail={"venue": "binance"})
        blob = client.get("/api/alerts").text + client.get("/api/reconcile").text
        assert "SECRET-TG-XYZ" not in blob
    finally:
        storage.set_setting("telegram_bot_token", "")


# ================================================================ the tick
def test_tick_runs_every_check_and_delivers_what_it_found():
    """End to end: a book in trouble, one pass of the monitor, and a human
    hears about it."""
    _reset()
    storage.set_setting("engine_heartbeat_t", int(time.time() - 1000))
    _mk_trade(mode="paper", status="closed", pnl=-300.0, r_multiple=-3.0,
              exit_time=int(time.time()))
    _feed(open_positions=[])
    _mk_trade(mode="live", ticket=7001)
    r = Relay()
    out = server.safety_tick(send=r)
    assert out["kill_switch"] == "fired", out
    assert out["engine"] == "stalled", out
    assert out["reconcile"]["halt"] is True, out
    kinds = {row["kind"] for row in _rows()}
    assert {"kill_switch_fired", "engine_deadman", "recon_drift"} <= kinds, kinds
    text = r.text()
    for expected in ("kill_switch_fired", "engine_deadman", "recon_drift"):
        assert expected in text, "%s never reached the relay: %s" % (expected, text)
    assert alerts.undelivered_count() == 0
    assert storage.get_setting("safety_monitor_t"), \
        "the monitor must publish its own liveness for the other process"


def test_tick_records_even_when_the_relay_is_down():
    _reset()
    storage.set_setting("engine_heartbeat_t", int(time.time() - 1000))
    server.safety_tick(send=Relay(broken=True))
    assert _one("engine_deadman")["delivered_t"] is None
    assert alerts.undelivered_count() >= 1, "a failed send is a retry, not a loss"
    r = Relay()
    server._safety["last_reconcile_t"] = 0.0
    server.safety_tick(send=r)
    assert "engine_deadman" in r.text()


def test_tick_never_invents_parameters_for_the_kill_switch():
    """If the parameter set cannot be read, the daily/weekly stops cannot be
    evaluated. Substituting defaults would produce a "clear" verdict computed
    against stops nobody chose."""
    _reset()
    real = engine.params
    engine.params = lambda: (_ for _ in ()).throw(RuntimeError("settings gone"))
    try:
        out = server.safety_tick(send=Relay())
    finally:
        engine.params = real
    assert "kill_switch" not in out, "the kill-switch check must be skipped"
    err = _one("safety_monitor_error")
    assert err["severity"] == alerts.P1, err["severity"]
    assert "did not run" in err["message"], err["message"]


def test_tick_survives_a_check_that_throws():
    """One broken check must not take the pager down with it, and must not be
    silent about having broken."""
    _reset()
    storage.set_setting("engine_heartbeat_t", int(time.time() - 1000))
    real = server.check_db_integrity
    server.check_db_integrity = lambda p: (_ for _ in ()).throw(
        RuntimeError("boom"))
    try:
        out = server.safety_tick(send=Relay())
    finally:
        server.check_db_integrity = real
    assert out["engine"] == "stalled", "later checks must still have run"
    assert _one("engine_deadman")
    err = _one("safety_monitor_error")
    assert "db_integrity" in err["message"] and "boom" in err["message"], err
    assert server._safety["errors"] == 1


def test_the_modules_are_actually_imported_by_production_code():
    """The regression this whole change exists to prevent: alerts.py and
    reconcile.py imported by nothing but their own tests."""
    assert server.alerts is alerts and server.reconcile is reconcile
    assert dashboard_api.alerts is alerts and dashboard_api.reconcile is reconcile


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
