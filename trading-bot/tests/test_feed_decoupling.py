"""Feed decoupling — the MT5 EA is one price source, not the engine's pulse.

The defect this pins: `engine_loop` carried `if feed_age > 60: continue`
BEFORE `manage_open_trades(p)`. That is a global loop bypass, not a per-venue
guard. On a host with no MT5 at all — which is now the target
(docs/ARCHITECTURE-V3.md) — the engine did nothing at all, forever, including
managing stops on crypto positions it had opened itself.

Covers:
  * no MT5 configured -> the engine still cycles and still manages
  * MT5 stale -> MT5 symbols stand aside, crypto position management continues
  * a configured venue unreachable -> only that venue's symbols stand aside
  * a stale venue price is never used for an entry, and never masquerades as
    fresh by landing on top of a newer print
  * venue-sourced prices carry provenance and go through the SAME writer
  * every entry rail behaves identically on a venue-sourced price
  * stale-source management is de-risk-only: the stop still fires, TP2 / TP1 /
    the trail do not

Run:  cd trading-bot && python3 tests/test_feed_decoupling.py
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
import instruments

instruments.seed_defaults()
# pin the session gate open; time-injected session tests live in test_risk_rails
engine.sessions.is_market_open = lambda symbol, now=None: (True, "")
storage.set_setting("news_calendar_last_ok", int(time.time()))

VENUE = "binance-test"
CRYPTO = "BTCUSD"
FX = "EURUSD"


# ------------------------------------------------------------------ helpers
class _Tick:
    def __init__(self, symbol, bid, ask, ts):
        self.symbol, self.bid, self.ask, self.ts = symbol, bid, ask, ts


class _FakeMeta:
    tick_size = 0.01
    contract_size = 1.0


class _FakeVenue:
    """Stands in for a ccxt adapter. Only the two methods the price path uses."""

    def __init__(self, ticks=None, raises=None):
        self.ticks = ticks or []
        self.raises = raises
        self.calls = 0

    def stream_prices(self, symbols):
        self.calls += 1
        if self.raises:
            raise self.raises
        return iter(self.ticks)

    def symbol_meta(self, symbol):
        return _FakeMeta()


def _install_venue(name=VENUE, symbols=(CRYPTO,), adapter=None):
    """Register a venue in the registry the engine reads, without credentials
    and without a network. Returns the fake adapter."""
    ad = adapter if adapter is not None else _FakeVenue()
    engine._VENUES_AVAILABLE = True
    engine._venues = type("V", (), {
        "list_venues": staticmethod(
            lambda: [{"name": name, "kind": "ccxt",
                      "symbols": list(symbols)}]),
        "adapter": staticmethod(lambda n: ad),
    })
    return ad


def _no_venues():
    engine._venues = type("V", (), {
        "list_venues": staticmethod(lambda: []),
        "adapter": staticmethod(lambda n: None),
    })


def _reset_feed():
    engine.feed_state["prices"] = {}
    engine.feed_state["sources"] = {}
    engine.feed_state["px_window"] = {}
    engine.feed_state["open_positions"] = []
    engine.feed_state["closed_today"] = []
    engine.feed_state["last_feed_t"] = 0
    engine._recent_keys.clear()
    engine._venue_meta_cache.clear()
    storage.set_setting("ea_last_feed_t", 0)
    storage.set_setting("mt5_enabled", None)
    _wipe("trades")
    _wipe("signals")


def _wipe(table):
    storage.execute("DELETE FROM %s" % table)


def _mt5_push(symbol=FX, bid=1.1000, ask=1.10008, age_s=0.0,
              tick_size=0.0001, tick_value=1.0):
    """Push through the real EA entry point, then age it by rewinding the
    stamps the way a dead EA would."""
    engine.ingest_feed({"account": {"balance": 10000.0, "equity": 10000.0},
                        "prices": [{"symbol": symbol, "bid": bid, "ask": ask,
                                    "tick_value": tick_value,
                                    "tick_size": tick_size,
                                    "point": tick_size, "spread": 1}]})
    if age_s:
        t = time.time() - age_s
        engine.feed_state["prices"][symbol]["src_t"] = t
        engine.feed_state["sources"][engine.MT5_SOURCE]["last_t"] = t
        engine.feed_state["last_feed_t"] = t


def _venue_push(symbol=CRYPTO, bid=60000.0, ask=60001.0, age_s=0.0,
                venue=VENUE):
    ad = _install_venue(name=venue, symbols=(symbol,),
                        adapter=_FakeVenue([_Tick(symbol, bid, ask,
                                                  time.time() - age_s)]))
    engine.refresh_venue_prices([symbol])
    return ad


def _open_trade(symbol, side="buy", mode="paper", **kw):
    tr = {"mode": mode, "trade_mode": "swing", "symbol": symbol, "side": side,
          "status": "open", "grade": "A", "entry_time": int(time.time()),
          "entry": 60000.0, "sl": 59000.0, "initial_sl": 59000.0,
          "tp1": 61000.0, "tp2": 62000.0, "lots": 0.1, "risk_pct": 1.0,
          "risk_amount": 100.0, "setup": "{}", "signal_id": 0,
          "asset_class": instruments.asset_class(symbol)}
    tr.update(kw)
    return storage.insert_trade(tr)


def _sig(symbol, side="buy", entry=60000.0, sl=59000.0, key=None):
    tp = entry + 2.5 * (entry - sl) if side == "buy" else entry - 2.5 * (sl - entry)
    return {"symbol": symbol, "trade_mode": "swing", "side": side, "grade": "A",
            "entry": entry, "sl": sl, "tp1": entry + (entry - sl),
            "tp": round(tp, 5), "rr": 2.5, "regime": 1.0,
            "setup": {"poi": {"lo": sl, "hi": entry}}, "strategy": "slc",
            "key": key or "fd|%s|%s|%d" % (symbol, side, time.time() * 1000)}


def _params(**over):
    p = engine.params()
    p.update({"trading_mode": "paper", "paper_balance": 10000.0,
              "max_concurrent": 5, "max_concurrent_per_class": 5,
              "max_correlated": 5, "max_bucket_exposure": 5.0,
              "risk_pct": 1.0, "min_rr": 2.0, "max_spread_frac": 0.10,
              "halt_new_entries": False, "enabled_pairs": [CRYPTO, FX],
              "watch_pairs": [], "agent_disabled_pairs": [],
              "agent_disabled_modes": [], "modes": ["swing"]})
    p.update(over)
    return p


# -------------------------------------------------- 1. no MT5 configured
def test_no_mt5_configured_is_not_a_halt():
    _reset_feed()
    _no_venues()
    assert engine.mt5_configured() is False, \
        "a box that has never seen an EA push must not believe MT5 exists"
    rep = engine.feed_report()
    assert rep["mt5_configured"] is False and rep["degraded"] == [], rep
    assert "no MT5" in rep["note"], rep["note"]


def test_mt5_configured_once_the_ea_has_pushed():
    _reset_feed()
    _mt5_push()
    assert engine.mt5_configured() is True
    # and it stays configured across a restart: the DB remembers
    engine.feed_state["last_feed_t"] = 0
    engine.feed_state["sources"] = {}
    storage.set_setting("ea_last_feed_t", int(time.time()))
    assert engine.mt5_configured() is True
    storage.set_setting("mt5_enabled", False)
    assert engine.mt5_configured() is False, "an explicit setting wins"
    storage.set_setting("mt5_enabled", None)


def test_engine_cycles_with_no_mt5_and_manages_a_venue_position():
    """The whole defect in one test: no MT5 anywhere, a crypto position open,
    and the engine must still walk a full cycle and move that stop."""
    _reset_feed()
    _venue_push(bid=61050.0, ask=61051.0)          # +1R on a 1000-wide stop
    tid = _open_trade(CRYPTO)
    assert engine.mt5_configured() is False
    engine.manage_open_trades(_params())
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["tp1_done"] == 1, "TP1 must be taken off a venue price"
    assert abs(tr["sl"] - tr["entry"]) < 1e-9, \
        "stop must have moved to breakeven with no MT5 in the deployment"
    _wipe("trades")


# ------------------------------------- 2. MT5 stale is not a global halt
def test_mt5_stale_does_not_suspend_crypto_management():
    _reset_feed()
    _mt5_push(age_s=600)                            # EA dead for ten minutes
    _venue_push(bid=61050.0, ask=61051.0)           # venue perfectly healthy
    assert engine.source_state(engine.MT5_SOURCE)["fresh"] is False
    assert engine.source_state(VENUE)["fresh"] is True
    tid = _open_trade(CRYPTO)
    engine.manage_open_trades(_params())
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["tp1_done"] == 1, \
        "a dead MT5 feed must not suspend management of a crypto position"
    _wipe("trades")


def test_mt5_stale_blocks_only_mt5_symbols():
    _reset_feed()
    _mt5_push(age_s=600)
    _venue_push()
    fx_px, fx_why = engine.price_for_entry(FX)
    cx_px, _ = engine.price_for_entry(CRYPTO)
    assert fx_px is None and "stale" in fx_why, (fx_px, fx_why)
    assert cx_px is not None, "the venue symbol must be unaffected"


def test_stale_mt5_management_is_derisk_only():
    """Stop still fires on a dead feed (the breach already printed); TP2, the
    TP1 partial and the trail do not (all three book something unverifiable)."""
    _reset_feed()
    _mt5_push(symbol=CRYPTO, bid=61050.0, ask=61051.0, age_s=600)
    tid = _open_trade(CRYPTO)
    engine.manage_open_trades(_params())
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["tp1_done"] == 0 and tr["status"] == "open", \
        "TP1 must not be banked off a stale quote"
    assert tr["mfe"] in (None, 0, 0.0), "excursion off a dead quote is not data"

    # same trade, stale quote now through the stop -> it MUST close
    engine.feed_state["prices"][CRYPTO]["bid"] = 58900.0
    engine.feed_state["prices"][CRYPTO]["ask"] = 58901.0
    engine.manage_open_trades(_params())
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["status"] == "closed", "a stop breach must still close on a stale feed"
    assert tr["exit_price"] == 59000.0, tr["exit_price"]
    _wipe("trades")


def test_stale_mt5_does_not_invent_a_live_close():
    """The live branch reads MT5's drop copy. Stale drop copy + missing
    position must NOT be read as 'closed at broker'."""
    _reset_feed()
    _mt5_push(symbol=CRYPTO, bid=60500.0, ask=60501.0, age_s=600)
    tid = _open_trade(CRYPTO, mode="live", ticket=12345)
    engine.feed_state["open_positions"] = []        # stale copy shows nothing
    engine.manage_open_trades(_params(trading_mode="live"))
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["status"] == "open", \
        "a stale drop copy must never be read as a broker close"
    _wipe("trades")


# ----------------------------------------- 3. venue prices and provenance
def test_venue_price_carries_provenance():
    _reset_feed()
    _venue_push()
    p = engine.feed_state["prices"][CRYPTO]
    assert p["src"] == VENUE and p["src_kind"] == "venue", p
    assert p["src_t"] > 0 and p["tick_size"] == 0.01, p
    assert engine.price_state(CRYPTO)["source"] == VENUE


def test_venue_price_feeds_the_same_spread_window():
    """The dynamic-spread stop rail reads px_window. A venue price must land
    in it exactly as an EA push does, or the rail silently stops applying."""
    _reset_feed()
    _venue_push(bid=60000.0, ask=60001.0)
    w = engine.feed_state["px_window"].get(CRYPTO)
    assert w and w["min_bid"] == 60000.0 and w["max_ask"] == 60001.0, w


def test_stale_venue_price_is_not_used_for_entry():
    _reset_feed()
    _venue_push(age_s=engine.VENUE_PRICE_MAX_AGE_S + 30)
    px, why = engine.price_for_entry(CRYPTO)
    assert px is None, "a quote older than the source limit is not tradable"
    assert "stale" in why, why
    _wipe("signals")
    engine.try_execute(_sig(CRYPTO), _params())
    assert storage.open_trades("paper") == [], \
        "no entry may be filled against a stale venue quote"
    row = storage.query_one("SELECT * FROM signals ORDER BY id DESC LIMIT 1")
    assert row and "stale" in (row["reason"] or ""), row
    _wipe("signals")


def test_stale_tick_cannot_overwrite_a_newer_one():
    """The masquerade: an old print landing on top of a good one and
    inheriting its recency."""
    _reset_feed()
    _venue_push(bid=60000.0, ask=60001.0)
    fresh_t = engine.feed_state["prices"][CRYPTO]["src_t"]
    n = engine.merge_prices(
        [{"symbol": CRYPTO, "bid": 1.0, "ask": 2.0,
          "src_t": fresh_t - 300}], VENUE)
    assert n == 0, "an older tick must be refused"
    p = engine.feed_state["prices"][CRYPTO]
    assert p["bid"] == 60000.0 and p["src_t"] == fresh_t, p


def test_unreachable_venue_stands_aside_on_that_venue_only():
    _reset_feed()
    _mt5_push()                                     # MT5 healthy
    ad = _install_venue(adapter=_FakeVenue(raises=RuntimeError("connection reset")))
    errs = engine.refresh_venue_prices([CRYPTO])
    assert VENUE in errs and "connection reset" in errs[VENUE], errs
    assert engine.source_state(VENUE)["reachable"] is False
    assert engine.price_for_entry(CRYPTO)[0] is None, \
        "an unreachable venue is not tradable"
    assert engine.price_for_entry(FX)[0] is not None, \
        "the MT5 symbol must be untouched by a venue outage"
    assert ad.calls == 1


def test_venue_answering_with_nothing_is_unreachable():
    _reset_feed()
    _install_venue(adapter=_FakeVenue([]))
    engine.refresh_venue_prices([CRYPTO])
    st = engine.source_state(VENUE)
    assert st["reachable"] is False and "no quotes" in st["detail"], st


def test_unreachable_venue_still_manages_a_recent_position():
    """Unreachable stops ENTRIES immediately. Management keeps using the last
    good print until it actually ages out — dropping a stop the moment a poll
    fails would be the original defect wearing a different hat."""
    _reset_feed()
    _venue_push(bid=61050.0, ask=61051.0)
    tid = _open_trade(CRYPTO)
    _install_venue(adapter=_FakeVenue(raises=RuntimeError("timeout")))
    engine.refresh_venue_prices([CRYPTO])
    assert engine.price_for_entry(CRYPTO)[0] is None
    assert engine.price_if_fresh(CRYPTO)[0] is not None
    engine.manage_open_trades(_params())
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["tp1_done"] == 1, "a recent print must still move the stop to BE"
    _wipe("trades")


def test_venue_symbol_map_is_declared_never_inferred():
    _reset_feed()
    _install_venue(symbols=("%s:BTC/USDT" % CRYPTO,))
    m = engine.venue_symbol_map([CRYPTO, FX])
    assert m == {VENUE: {"BTC/USDT": CRYPTO}}, m
    _no_venues()
    assert engine.venue_symbol_map([CRYPTO, FX]) == {}, \
        "no configured venue means no symbol is routed anywhere"


# --------------------------------- 4. rails are provenance-blind
def test_rails_identical_on_venue_and_mt5_prices():
    """Same geometry, same rails, two different sources. Both must reach the
    same verdict; the only thing that differs is who quoted it."""
    outcomes = {}
    for src in ("mt5", "venue"):
        _reset_feed()
        _no_venues()
        if src == "mt5":
            # identical sizing metadata to what the venue path derives from
            # symbol_meta, so the only variable left is provenance
            _mt5_push(symbol=CRYPTO, bid=60000.0, ask=60001.0,
                      tick_size=0.01, tick_value=0.01)
        else:
            _venue_push(bid=60000.0, ask=60001.0)
        px = engine.feed_state["prices"][CRYPTO]
        assert (px["tick_size"], px["tick_value"]) == (0.01, 0.01), px
        got = {}

        # (a) a good candidate executes
        engine.try_execute(_sig(CRYPTO, key="rails|%s|ok" % src), _params())
        trs = storage.open_trades("paper")
        got["executed"] = len(trs)
        got["risk_pct"] = round(trs[0]["risk_pct"], 3) if trs else None
        _wipe("trades")

        # (b) spread wider than max_spread_frac of the stop is refused
        engine.feed_state["prices"][CRYPTO]["ask"] = 60900.0
        _wipe("signals")
        engine.try_execute(_sig(CRYPTO, key="rails|%s|spread" % src), _params())
        got["spread_blocked"] = storage.open_trades("paper") == []
        row = storage.query_one("SELECT reason FROM signals ORDER BY id DESC LIMIT 1")
        got["spread_reason"] = (row["reason"] or "")[:6]
        engine.feed_state["prices"][CRYPTO]["ask"] = 60001.0
        _wipe("trades")
        _wipe("signals")

        # (c) manual halt refuses regardless of who quoted the price
        engine.try_execute(_sig(CRYPTO, key="rails|%s|halt" % src),
                           _params(halt_new_entries=True))
        got["halt_blocked"] = storage.open_trades("paper") == []
        _wipe("trades")
        _wipe("signals")

        # (d) DB integrity suspect refuses regardless of source
        storage._mark_suspect("test: pretend corruption")
        try:
            engine.try_execute(_sig(CRYPTO, key="rails|%s|db" % src), _params())
            got["integrity_blocked"] = storage.open_trades("paper") == []
        finally:
            storage._clear_suspect()
        _wipe("trades")
        _wipe("signals")
        outcomes[src] = got

    assert outcomes["mt5"] == outcomes["venue"], outcomes
    assert outcomes["mt5"]["executed"] == 1, outcomes
    assert outcomes["mt5"]["spread_blocked"], outcomes
    assert outcomes["mt5"]["spread_reason"] == "spread", outcomes
    assert outcomes["mt5"]["halt_blocked"] and outcomes["mt5"]["integrity_blocked"], \
        outcomes


def test_venue_price_without_tick_metadata_refuses_to_size():
    """Sizing metadata we could not fetch is left absent, not guessed. The
    correct answer to 'I cannot size this' is no trade."""
    _reset_feed()
    ad = _FakeVenue([_Tick(CRYPTO, 60000.0, 60001.0, time.time())])
    ad.symbol_meta = lambda s: (_ for _ in ()).throw(RuntimeError("no filters"))
    _install_venue(adapter=ad)
    engine.refresh_venue_prices([CRYPTO])
    p = engine.feed_state["prices"][CRYPTO]
    assert "tick_value" not in p, p
    _wipe("signals")
    engine.try_execute(_sig(CRYPTO), _params())
    assert storage.open_trades("paper") == []
    row = storage.query_one("SELECT reason FROM signals ORDER BY id DESC LIMIT 1")
    assert "size" in (row["reason"] or ""), row["reason"]
    _wipe("signals")


# --------------------------------- 5. the loop body, end to end
def test_one_cycle_manages_and_analyses_without_any_mt5():
    """Drive the real loop body once with poll_seconds so small the sleep is
    irrelevant, and prove it did work rather than skipping to the sleep."""
    _reset_feed()
    _venue_push(bid=61050.0, ask=61051.0)
    tid = _open_trade(CRYPTO)
    storage.set_setting("enabled_pairs", [CRYPTO])
    storage.set_setting("watch_pairs", [])
    storage.set_setting("modes", ["swing"])

    import threading
    t = threading.Thread(target=engine.engine_loop, args=(1,), daemon=True)
    t.start()
    try:
        deadline = time.time() + 15
        tr = None
        while time.time() < deadline:
            tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
            if tr["tp1_done"] == 1:
                break
            time.sleep(0.2)
        assert tr and tr["tp1_done"] == 1, \
            "engine_loop must manage open trades with no EA feed in existence"
        assert storage.get_setting("engine_heartbeat_t", 0) > 0
        note = next((i["note"] for i in engine.get_last_info()
                     if i.get("symbol") == "—"), "")
        assert "ok" in note or "no MT5" in note, note
    finally:
        engine.stop()
        t.join(timeout=5)
        assert not t.is_alive(), "engine_loop must honour stop()"
    _wipe("trades")


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
