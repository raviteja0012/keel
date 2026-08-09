"""Position-reconciliation tests — the engine only believes what the venue confirms.

Covers:
  * clean match against a real venue adapter (through venues.positions_all)
  * every discrepancy class: LOCAL_ONLY, VENUE_ONLY, QTY_MISMATCH, STOP_MISMATCH
  * a venue that cannot be reached is UNKNOWN and UNKNOWN halts new entries
  * no position source at all is UNKNOWN, not "we are flat"
  * foreign positions (read-only venue / other MT5 magic) report but never halt
  * paper/shadow trades are not reconciled against venues they never reached
  * the post-TP1 remainder is not a phantom size mismatch
  * reconcile places no order, cancels no order, and moves no trade out of
    status='open' — a reconciler that trades is a second execution path

Run:  cd trading-bot && python3 tests/test_reconcile.py
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

import brokers
import instruments
import reconcile
import venues

instruments.seed_defaults()

TICK_EURUSD = 1e-5           # instruments display_digits = 5


# ------------------------------------------------------------- fake venue
# A venue whose write methods are booby-trapped: reconcile touching either of
# them fails the suite loudly rather than quietly costing money.
_state = {}                  # venue name -> {"positions": [...], "error": ""}
_writes = {"place_order": 0, "cancel": 0}


class _FakeAdapter:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.read_only = bool(cfg.get("read_only", True))

    def health(self):
        return brokers.VenueHealth(name=self.name, reachable=True,
                                   authenticated=True, read_only=self.read_only)

    def symbol_meta(self, symbol):
        raise NotImplementedError

    def balances(self):
        return []

    def positions(self):
        st = _state.get(self.name, {})
        if st.get("error"):
            raise brokers.VenueError(st["error"], venue=self.name)
        return [brokers.Position(**p) for p in st.get("positions", [])]

    def place_order(self, order):
        _writes["place_order"] += 1
        raise AssertionError("reconcile placed an order")

    def cancel(self, venue_order_id, symbol=""):
        _writes["cancel"] += 1
        raise AssertionError("reconcile cancelled an order")

    def stream_prices(self, symbols):
        return iter(())


brokers.register("fake", _FakeAdapter)


def _venue(name, read_only=False, positions=None, error=""):
    _state[name] = {"positions": positions or [], "error": error}
    venues.upsert({"name": name, "kind": "fake", "read_only": read_only})


def _clear_venues():
    for v in list(venues.list_venues()):
        venues.remove(v["name"])
    _state.clear()


def _pos(symbol, side, qty, venue_id="", entry=1.1):
    return {"symbol": symbol, "side": side, "qty": qty,
            "entry_price": entry, "venue_id": venue_id}


def _wipe(table):
    storage.execute("DELETE FROM %s" % table)


def _mk_trade(symbol="EURUSD", side="buy", mode="live", **kw):
    tr = {"mode": mode, "trade_mode": "swing", "symbol": symbol, "side": side,
          "status": "open", "grade": "A", "entry_time": int(time.time()),
          "entry": 1.1000, "sl": 1.0950, "initial_sl": 1.0950,
          "tp1": 1.1050, "tp2": 1.1100, "lots": 0.10, "risk_pct": 1.0,
          "risk_amount": 100.0, "setup": "{}", "signal_id": 0}
    tr.update(kw)
    return storage.insert_trade(tr)


def _reset():
    _wipe("trades")
    _clear_venues()


def _classes(rep):
    return sorted(d["class"] for d in rep["discrepancies"])


# ---------------------------------------------------------------- clean
def test_clean_match_through_the_venue_registry():
    _reset()
    tid = _mk_trade(ticket=4001)
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.10, "4001")])
    rep = reconcile.reconcile("live")
    assert rep["discrepancies"] == [], rep["discrepancies"]
    assert len(rep["clean"]) == 1 and rep["clean"][0]["trade_id"] == tid, rep
    assert not reconcile.should_halt_entries(rep)
    _reset()


def test_clean_match_on_symbol_when_no_ticket_recorded():
    _reset()
    _mk_trade()                                  # ticket never came back
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.10, "9999")])
    rep = reconcile.reconcile("live")
    assert rep["discrepancies"] == [], rep["discrepancies"]
    assert not reconcile.should_halt_entries(rep)
    _reset()


def test_stablecoin_and_slash_spellings_still_match():
    _reset()
    _mk_trade(symbol="BTCUSD", entry=60000.0, sl=59000.0)
    _venue("vx", positions=[_pos("BTC/USDT:USDT", "long", 0.10, entry=60000.0)])
    rep = reconcile.reconcile("live")
    assert rep["discrepancies"] == [], \
        "BTC/USDT must reconcile against BTCUSD, not halt forever"
    _reset()


# --------------------------------------------------------- discrepancies
def test_local_only():
    _reset()
    tid = _mk_trade()
    _venue("vx", positions=[])                   # reachable, holding nothing
    rep = reconcile.reconcile("live")
    assert _classes(rep) == [reconcile.LOCAL_ONLY], rep["discrepancies"]
    d = rep["discrepancies"][0]
    assert d["trade_id"] == tid and d["material"]
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_venue_only_orphan_halts():
    _reset()
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.10, "7777")])
    rep = reconcile.reconcile("live")
    assert _classes(rep) == [reconcile.VENUE_ONLY], rep["discrepancies"]
    assert rep["discrepancies"][0]["material"], \
        "a position at a trading-enabled venue that we never opened is a double fill"
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_venue_only_on_read_only_venue_reports_but_does_not_halt():
    _reset()
    _venue("watch", read_only=True,
           positions=[_pos("EURUSD", "buy", 0.10, "8888")])
    rep = reconcile.reconcile("live")
    assert _classes(rep) == [reconcile.VENUE_ONLY], rep["discrepancies"]
    assert not rep["discrepancies"][0]["material"], \
        "the engine cannot trade a read-only venue, so its positions are not orphans"
    assert not reconcile.should_halt_entries(rep)
    _reset()


def test_venue_only_is_ours_when_the_id_is_one_we_recorded():
    _reset()
    # the trade closed locally but the position is still standing at the venue
    _mk_trade(status="closed", ticket=5150, exit_time=int(time.time()))
    _venue("watch", read_only=True,
           positions=[_pos("EURUSD", "buy", 0.10, "5150")])
    rep = reconcile.reconcile("live")
    d = rep["discrepancies"][0]
    assert d["class"] == reconcile.VENUE_ONLY and d["material"], d
    assert reconcile.should_halt_entries(rep), \
        "a venue id this engine recorded is ours whatever the registry says"
    _reset()


def test_qty_mismatch():
    _reset()
    _mk_trade(ticket=4002, lots=0.10)
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.04, "4002")])
    rep = reconcile.reconcile("live")
    assert _classes(rep) == [reconcile.QTY_MISMATCH], rep["discrepancies"]
    d = rep["discrepancies"][0]
    assert d["local_qty"] == 0.10 and d["venue_qty"] == 0.04, d
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_flipped_side_on_a_matched_ticket_is_a_size_discrepancy():
    _reset()
    _mk_trade(ticket=4003, side="buy", lots=0.10)
    _venue("vx", positions=[_pos("EURUSD", "sell", 0.10, "4003")])
    rep = reconcile.reconcile("live")
    assert _classes(rep) == [reconcile.QTY_MISMATCH], rep["discrepancies"]
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_tp1_remainder_is_not_a_mismatch():
    _reset()
    _mk_trade(ticket=4004, lots=0.10, tp1_done=1)
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.05, "4004")])
    rep = reconcile.reconcile("live")
    assert rep["discrepancies"] == [], \
        "half is banked at TP1 while trades.lots keeps the entry size"
    _reset()


def test_stop_mismatch_beyond_one_tick():
    _reset()
    _mk_trade(ticket=4005, sl=1.0950)
    feed = [{"ticket": 4005, "symbol": "EURUSD", "side": "buy", "lots": 0.10,
             "entry": 1.1000, "sl": 1.0900, "magic": 770001}]
    rep = reconcile.reconcile("live", positions=reconcile.from_mt5_feed(feed, 770001))
    assert _classes(rep) == [reconcile.STOP_MISMATCH], rep["discrepancies"]
    d = rep["discrepancies"][0]
    assert d["local_sl"] == 1.0950 and d["venue_sl"] == 1.0900, d
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_stop_within_one_tick_is_clean():
    _reset()
    _mk_trade(ticket=4006, sl=1.0950)
    feed = [{"ticket": 4006, "symbol": "EURUSD", "side": "buy", "lots": 0.10,
             "entry": 1.1000, "sl": 1.0950 + TICK_EURUSD / 2, "magic": 770001}]
    rep = reconcile.reconcile("live", positions=reconcile.from_mt5_feed(feed, 770001))
    assert rep["discrepancies"] == [], \
        "rounding inside one tick is not drift"
    _reset()


def test_no_stop_attached_at_the_venue_is_a_stop_mismatch():
    _reset()
    _mk_trade(ticket=4007, sl=1.0950)
    feed = [{"ticket": 4007, "symbol": "EURUSD", "side": "buy", "lots": 0.10,
             "entry": 1.1000, "sl": 0.0, "magic": 770001}]
    rep = reconcile.reconcile("live", positions=reconcile.from_mt5_feed(feed, 770001))
    assert _classes(rep) == [reconcile.STOP_MISMATCH], rep["discrepancies"]
    assert "no stop" in rep["discrepancies"][0]["detail"]
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_venue_that_reports_no_stop_field_is_unverified_not_halted():
    _reset()
    _mk_trade(ticket=4008)
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.10, "4008")])
    rep = reconcile.reconcile("live")
    assert rep["discrepancies"] == [], rep["discrepancies"]
    assert len(rep["stops_unverified"]) == 1, \
        "a venue with no stop field must be surfaced, not silently clean"
    assert not reconcile.should_halt_entries(rep), \
        "spot venues carry no resting stop; halting on that halts forever"
    _reset()


def test_foreign_magic_reports_but_does_not_halt():
    _reset()
    feed = [{"ticket": 909, "symbol": "XAUUSD", "side": "buy", "lots": 1.0,
             "entry": 2400.0, "sl": 2390.0, "magic": 12345}]
    rep = reconcile.reconcile("live", positions=reconcile.from_mt5_feed(feed, 770001))
    assert _classes(rep) == [reconcile.VENUE_ONLY], rep["discrepancies"]
    assert not rep["discrepancies"][0]["material"], \
        "the operator's own manual position is not the bot's orphan"
    assert not reconcile.should_halt_entries(rep)
    _reset()


def test_wrapped_ticket_is_unknown():
    _reset()
    _mk_trade(ticket=4009)
    feed = [{"ticket": -2147480000, "symbol": "EURUSD", "side": "buy",
             "lots": 0.10, "entry": 1.1000, "sl": 1.0950, "magic": 770001}]
    rep = reconcile.reconcile("live", positions=reconcile.from_mt5_feed(feed, 770001))
    assert reconcile.UNKNOWN in _classes(rep), rep["discrepancies"]
    assert reconcile.should_halt_entries(rep), \
        "a 32-bit-wrapped ticket makes every ticket-keyed mechanism a guess"
    _reset()


# ------------------------------------------------------- fail closed
def test_unreachable_venue_halts_entries():
    _reset()
    _mk_trade(ticket=4010)
    _venue("vx", error="connection timed out")
    rep = reconcile.reconcile("live")
    assert reconcile.UNKNOWN in _classes(rep), rep["discrepancies"]
    assert reconcile.LOCAL_ONLY not in _classes(rep), \
        "a venue that did not answer has not told us the position is gone"
    assert reconcile.should_halt_entries(rep)
    assert "vx" in rep["blind"] and not rep["sources"][0]["ok"], rep["sources"]
    _reset()


def test_one_blind_venue_blinds_the_whole_comparison():
    _reset()
    _mk_trade(ticket=4011)
    _venue("up", positions=[])
    _venue("down", error="502")
    rep = reconcile.reconcile("live")
    assert _classes(rep).count(reconcile.LOCAL_ONLY) == 0, \
        "the position may be sitting at the venue we could not read"
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_unreachable_venue_halts_even_with_nothing_open():
    _reset()
    _venue("vx", error="connection refused")
    rep = reconcile.reconcile("live")
    assert reconcile.should_halt_entries(rep), \
        "an orphan can be hiding behind the silence"
    _reset()


def test_no_position_source_with_live_trades_is_unknown():
    _reset()
    _mk_trade(ticket=4012)
    rep = reconcile.reconcile("live")            # no venues configured at all
    assert _classes(rep) == [reconcile.UNKNOWN], rep["discrepancies"]
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_venue_layer_raising_is_unknown_not_clean():
    _reset()
    _mk_trade(ticket=4013)
    boom = venues.positions_all
    venues.positions_all = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        halt, why, rep = reconcile.gate("live")
    finally:
        venues.positions_all = boom
    assert halt and "boom" in why, (halt, why)
    assert set(_classes(rep)) == {reconcile.UNKNOWN}, rep["discrepancies"]
    _reset()


def test_an_injected_source_does_not_blind_the_registry():
    _reset()
    _mk_trade(ticket=4014)
    _venue("vx", error="connection refused")
    feed = [{"ticket": 4014, "symbol": "EURUSD", "side": "buy", "lots": 0.10,
             "entry": 1.1000, "sl": 1.0950, "magic": 770001}]
    rep = reconcile.reconcile("live", positions=reconcile.from_mt5_feed(feed, 770001),
                              venue_names=["mt5"])
    assert reconcile.should_halt_entries(rep), \
        "handing the reconciler one source must not hide a venue it cannot read"
    by_name = {s["venue"]: s["ok"] for s in rep["sources"]}
    assert by_name == {"mt5": True, "vx": False}, \
        "one dead venue must not mark the healthy source dead as well: %s" % by_name
    _reset()


def test_a_declared_source_holding_nothing_is_local_only_not_unknown():
    _reset()
    _mk_trade(ticket=4015)
    rep = reconcile.reconcile("live", positions=[], venue_names=["mt5"])
    assert _classes(rep) == [reconcile.LOCAL_ONLY], rep["discrepancies"]
    assert reconcile.should_halt_entries(rep)
    _reset()


def test_should_halt_entries_fails_closed_on_junk():
    for junk in (None, {}, {"discrepancies": None}, "clean", 0, []):
        assert reconcile.should_halt_entries(junk), \
            "a report we cannot read is an uncertainty, and uncertainty halts"


def test_paper_trades_are_not_reconciled_against_venues():
    _reset()
    _mk_trade(mode="paper")
    _venue("vx", positions=[])
    rep = reconcile.reconcile("paper")
    assert rep["venue_backed"] is False and rep["discrepancies"] == [], rep
    assert not reconcile.should_halt_entries(rep), \
        "paper positions are simulated in engine.py; no venue ever saw them"
    _reset()


def test_shipped_paper_configuration_is_quiet():
    _reset()
    _mk_trade(mode="paper")
    rep = reconcile.reconcile("paper")           # no venues, no live trades
    assert not reconcile.should_halt_entries(rep), \
        "zero venues in paper mode must not halt the shipped configuration"
    _reset()


def test_orphan_still_halts_while_in_paper_mode():
    _reset()
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.10, "6001")])
    rep = reconcile.reconcile("paper")
    assert reconcile.should_halt_entries(rep), \
        "a real position nobody opened matters whatever mode we think we are in"
    _reset()


# ------------------------------------------------------- never trades
def test_reconcile_places_and_cancels_nothing():
    _reset()
    _writes.update({"place_order": 0, "cancel": 0})
    storage.execute("DELETE FROM commands")
    _mk_trade(ticket=4020, lots=0.10, sl=1.0950)          # LOCAL_ONLY
    _mk_trade(symbol="XAUUSD", ticket=4021, entry=2400.0, sl=2390.0)
    _venue("vx", positions=[_pos("GBPUSD", "sell", 0.20, "7001")])  # orphan
    _venue("down", error="gone")                                    # unknown
    rep = reconcile.reconcile("live")
    assert reconcile.should_halt_entries(rep)
    assert _writes == {"place_order": 0, "cancel": 0}, _writes
    assert storage.query("SELECT * FROM commands") == [], \
        "reconcile must never enqueue a command — that is the live order path"
    _reset()


def test_reconcile_leaves_every_live_trade_in_status_open():
    _reset()
    tid = _mk_trade(ticket=4030)
    _venue("vx", positions=[])                   # LOCAL_ONLY: the tempting heal
    rep = reconcile.reconcile("live")
    reconcile.apply_recon_state(rep)
    row = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert row["status"] == "open", \
        "open_trades() is the sole source for the kill switch, max_concurrent " \
        "and the exposure ledger — parking a live position elsewhere unmanages it"
    assert row["exit_time"] is None and row["exit_price"] is None, row
    assert len(storage.open_trades("live")) == 1
    _reset()


def test_module_holds_no_execution_path():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "reconcile.py"), encoding="utf-8").read()
    for token in ("enqueue_command", "place_order", "insert_trade",
                  "_close_paper", "import engine"):
        assert token not in src, "reconcile.py must not reference %s" % token
    writes = [ln.strip() for ln in src.splitlines()
              if any(k in ln for k in ("UPDATE ", "DELETE ", "INSERT "))]
    assert writes == ['storage.execute("UPDATE trades SET recon_state=? WHERE id=?",'], \
        "the only write in this module is the recon_state annotation: %s" % writes
    # schema changes are additive only: one ALTER, adding one column
    assert src.count("ALTER TABLE") == 1 and "ADD COLUMN recon_state" in src
    assert "DROP" not in src


# --------------------------------------------------------- annotation
def test_recon_state_is_additive_and_written():
    _reset()
    a = _mk_trade(symbol="EURUSD", ticket=4040)
    b = _mk_trade(symbol="GBPUSD", ticket=4041, entry=1.27, sl=1.26)
    _venue("vx", positions=[_pos("EURUSD", "buy", 0.10, "4040")])
    rep = reconcile.reconcile("live")
    assert reconcile.apply_recon_state(rep) == 2
    cols = [r["name"] for r in storage.query("PRAGMA table_info(trades)")]
    assert "recon_state" in cols, cols
    rows = {r["id"]: r["recon_state"]
            for r in storage.query("SELECT id, recon_state FROM trades")}
    assert rows[a] == "clean" and rows[b] == "unconfirmed", rows
    # idempotent: a second pass over an unchanged picture writes nothing
    assert reconcile.apply_recon_state(reconcile.reconcile("live")) == 0
    _reset()


def test_recon_state_survives_a_second_migration_pass():
    _reset()
    reconcile._schema_ready["done"] = False      # simulate a fresh process
    tid = _mk_trade(ticket=4050)
    _venue("vx", error="down")
    reconcile.apply_recon_state(reconcile.reconcile("live"))
    row = storage.query_one("SELECT recon_state FROM trades WHERE id=?", (tid,))
    assert row["recon_state"] == "unknown", row
    _reset()


def test_annotation_never_raises_on_a_junk_report():
    for junk in (None, {}, "nope", {"clean": None, "discrepancies": 7}):
        assert reconcile.apply_recon_state(junk) == 0


def test_halt_reason_names_the_finding():
    _reset()
    _mk_trade(ticket=4060)
    _venue("vx", positions=[])
    why = reconcile.halt_reason(reconcile.reconcile("live"))
    assert "LOCAL_ONLY" in why and "EURUSD" in why, why
    assert reconcile.halt_reason({"discrepancies": []}) == ""
    assert reconcile.halt_reason(None), "an unreadable report needs a reason too"
    _reset()


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
