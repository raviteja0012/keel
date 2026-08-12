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
  * price provenance is BOUND to the execution venue: a signal priced by a
    venue is never submitted to MT5, and a live entry with no wired route is
    refused rather than sent somewhere it can be filled
  * the venue poll runs off the cycle: a wedged exchange never delays a stop
  * an unstamped quote is stamped at the write layer, and a quote with no
    clock fails CLOSED rather than reading as permanently fresh
  * adapter exception text never reaches a persisted reason or a notification
  * a held position the loop could not evaluate is recorded and announced
  * the MT5 drop-copy guard applies only to positions actually held at MT5

Run:  cd trading-bot && python3 tests/test_feed_decoupling.py
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate all DB writes BEFORE importing modules that touch storage
import storage
storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
storage.init()

import decisions
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

    def __init__(self, ticks=None, raises=None, delay=0.0):
        self.ticks = ticks or []
        self.raises = raises
        self.delay = delay
        self.calls = 0

    def stream_prices(self, symbols):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
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


def _install_venues(rows, adapters=None):
    """Register several venues at once (routing-ambiguity cases)."""
    ads = adapters or {}
    engine._VENUES_AVAILABLE = True
    engine._venues = type("V", (), {
        "list_venues": staticmethod(lambda: [dict(r) for r in rows]),
        "adapter": staticmethod(lambda n: ads.get(n) or _FakeVenue()),
    })


def _no_venues():
    engine._venues = type("V", (), {
        "list_venues": staticmethod(lambda: []),
        "adapter": staticmethod(lambda n: None),
    })


# every notification the engine emitted since the last _reset_feed
_notes = []


def _reset_feed():
    engine.stop_venue_poller()
    engine.feed_state["prices"] = {}
    engine.feed_state["sources"] = {}
    engine.feed_state["px_window"] = {}
    engine.feed_state["open_positions"] = []
    engine.feed_state["closed_today"] = []
    engine.feed_state["last_feed_t"] = 0
    engine.feed_state["account"] = {}
    engine.feed_state.pop("venue_poll", None)
    engine._recent_keys.clear()
    engine._venue_meta_cache.clear()
    engine._unmanaged.clear()
    engine._last_info.clear()
    decisions._last_state.clear()
    del _notes[:]
    engine.set_notifier(_notes.append)
    storage.set_setting("ea_last_feed_t", 0)
    storage.set_setting("mt5_enabled", None)
    # left ON deliberately: the leak tests below need the skip notification to
    # actually be emitted so its CONTENT can be asserted
    storage.set_setting("notify_signals", True)
    _wipe("trades")
    _wipe("signals")
    _wipe("decisions")
    _wipe("commands")


def _wipe(table):
    storage.execute("DELETE FROM %s" % table)


def _pending_commands():
    return storage.query("SELECT * FROM commands WHERE status='pending'")


def _last_decision(stage=None):
    if stage:
        return storage.query_one(
            "SELECT * FROM decisions WHERE stage=? ORDER BY id DESC LIMIT 1",
            (stage,))
    return storage.query_one("SELECT * FROM decisions ORDER BY id DESC LIMIT 1")


def _last_signal_reason():
    row = storage.query_one("SELECT reason FROM signals ORDER BY id DESC LIMIT 1")
    return (row["reason"] or "") if row else ""


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
    # the guard still applies where it should — and no longer silently
    d = _last_decision("manage")
    assert d and "drop copy stale" in (d["reason"] or ""), d
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
    """CHANGED EXPECTATION: this used to assert `"connection reset" in
    errs[VENUE]`, i.e. it asserted that the adapter's own exception text
    propagates. That text is third-party and unbounded — ccxt puts the request
    it sent in the message — and it flows into signals.reason (persisted) and
    notify(). The old expectation was pinning the leak, so it is replaced by
    its opposite: the raw text must NOT survive, only a classification."""
    _reset_feed()
    _mt5_push()                                     # MT5 healthy
    ad = _install_venue(adapter=_FakeVenue(raises=RuntimeError("connection reset")))
    errs = engine.refresh_venue_prices([CRYPTO])
    assert VENUE in errs, errs
    assert "connection reset" not in errs[VENUE], errs
    assert errs[VENUE] == "adapter error (RuntimeError)", errs
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


# ------------------------- 5. provenance is bound to the execution venue
def _live_ready():
    """A deployment where a live MT5 order COULD be placed: an EA has reported
    and there is a balance. Without this the routing test would pass for the
    wrong reason."""
    storage.set_setting("mt5_enabled", True)
    engine.feed_state["account"] = {"balance": 10000.0, "equity": 10000.0}


def test_live_entry_on_a_venue_symbol_never_reaches_the_mt5_queue():
    """The proved defect: BTCUSD routed to a venue produced a live entry sized
    off the venue's book and emitted type=open_trade to the MT5 command queue.
    On a box with an EA attached that is a real order at the wrong broker."""
    _reset_feed()
    _live_ready()
    _venue_push(bid=60000.0, ask=60001.0)           # quoted by binance-test
    assert engine.price_state(CRYPTO)["source"] == VENUE
    engine.try_execute(_sig(CRYPTO, key="route|live"),
                       _params(trading_mode="live"))
    assert storage.open_trades("live") == [], \
        "a live entry with no wired route to its venue must not open"
    assert _pending_commands() == [], \
        "no command may be queued for a venue the EA does not hold"
    d = _last_decision("routing")
    assert d and d["action"] == "skipped", d
    assert "no live execution path" in (d["reason"] or ""), d["reason"]
    assert "no live execution path" in _last_signal_reason(), _last_signal_reason()


def test_mt5_quote_cannot_fill_a_symbol_declared_on_a_venue():
    """The mirror image: the EA quotes a symbol the registry says belongs to a
    venue. Price and execution venue must be the same one."""
    _reset_feed()
    _live_ready()
    _install_venue(symbols=(CRYPTO,))               # BTCUSD belongs to the venue
    _mt5_push(symbol=CRYPTO, bid=60000.0, ask=60001.0,
              tick_size=0.01, tick_value=0.01)
    assert engine.price_state(CRYPTO)["source"] == engine.MT5_SOURCE
    engine.try_execute(_sig(CRYPTO, key="route|mismatch"),
                       _params(trading_mode="live"))
    assert storage.open_trades("live") == [] and _pending_commands() == []
    assert "declared on %s but quoted by mt5" % VENUE in _last_signal_reason(), \
        _last_signal_reason()


def test_two_venues_declaring_one_symbol_refuse_to_guess():
    _reset_feed()
    ad = _FakeVenue([_Tick(CRYPTO, 60000.0, 60001.0, time.time())])
    _install_venues([{"name": VENUE, "kind": "ccxt", "symbols": [CRYPTO]},
                     {"name": "kraken-test", "kind": "ccxt", "symbols": [CRYPTO]}],
                    adapters={VENUE: ad, "kraken-test": ad})
    engine.refresh_venue_prices([CRYPTO])
    engine.try_execute(_sig(CRYPTO, key="route|ambig"), _params())
    assert storage.open_trades("paper") == [], \
        "an ambiguous route is not a route"
    assert "more than one venue" in _last_signal_reason(), _last_signal_reason()


def test_paper_entry_on_the_declaring_venue_is_allowed():
    """The rail must refuse wrong routes without refusing right ones: paper is
    filled by the engine itself against the venue that quoted it."""
    _reset_feed()
    _venue_push(bid=60000.0, ask=60001.0)
    engine.try_execute(_sig(CRYPTO, key="route|ok"), _params())
    trs = storage.open_trades("paper")
    assert len(trs) == 1 and trs[0]["symbol"] == CRYPTO, trs
    assert _pending_commands() == [], "paper never queues a broker command"
    _wipe("trades")


def test_execution_route_is_declaration_only():
    _reset_feed()
    _no_venues()
    assert engine.declared_venues(CRYPTO) == [], \
        "no registered venue means no venue declares anything"
    r = engine.execution_route(CRYPTO, engine.MT5_SOURCE, "paper")
    assert r["ok"] and r["kind"] == "mt5", r
    r = engine.execution_route(CRYPTO, None, "paper")
    assert not r["ok"] and "no source" in r["reason"], r
    r = engine.execution_route(CRYPTO, "ghost-venue", "paper")
    assert not r["ok"], r
    _install_venue(symbols=(CRYPTO,))
    assert engine.declared_venues(CRYPTO) == [VENUE]
    assert engine.execution_route(CRYPTO, VENUE, "paper")["ok"] is True
    assert engine.execution_route(CRYPTO, VENUE, "live")["ok"] is False


# --------------------- 6. the venue poll never delays stop management
def test_slow_venue_poll_does_not_delay_stop_management():
    """The original defect was stop management blocked behind a feed. It came
    back as a blocking call rather than a `continue`: refresh_venue_prices sat
    ahead of manage_open_trades, so a 6s adapter delayed a breakeven move by
    exactly 6s. The poll owns a thread now; the cycle must not wait for it."""
    _reset_feed()
    _venue_push(bid=61050.0, ask=61051.0)           # good print already in book
    tid = _open_trade(CRYPTO)
    slow = _install_venue(adapter=_FakeVenue(
        [_Tick(CRYPTO, 61050.0, 61051.0, time.time())], delay=6.0))
    storage.set_setting("enabled_pairs", [CRYPTO])
    storage.set_setting("watch_pairs", [])
    storage.set_setting("modes", ["swing"])

    t0 = time.time()
    th = threading.Thread(target=engine.engine_loop, args=(1,), daemon=True)
    th.start()
    try:
        elapsed = None
        while time.time() - t0 < 12:
            tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
            if tr["tp1_done"] == 1:
                elapsed = time.time() - t0
                break
            time.sleep(0.05)
        assert elapsed is not None, "the stop was never moved at all"
        assert slow.calls >= 1, \
            "the slow poll must actually be in flight, or this proves nothing"
        assert elapsed < 4.0, \
            "breakeven took %.1fs — management is waiting on the venue poll" % elapsed
    finally:
        engine.stop()
        th.join(timeout=5)
        engine.stop_venue_poller(join_s=10)
    _wipe("trades")


# -------------------------- 7. a quote with no clock fails CLOSED
def test_unattributed_write_is_dated_but_never_attributed():
    """This used to assert that the book INFERRED a source for a write that did
    not name one, and that the inferred quote was tradable. That was the defect,
    not the specification: resolving a source from whatever instruments happen to
    declare, then writing a health row for the guess with reachable=True,
    manufactured a healthy feed out of nothing. Verified against the old code -
    a bare price produced source=mt5, fresh=True, reachable=True.

    The two halves are separated now. Dating the record is honest, because we do
    know when it arrived, so it ages like anything else. Naming its source is
    not, so it belongs to nobody and cannot be opened on."""
    _reset_feed()
    _no_venues()
    engine.feed_state["prices"][FX] = {
        "symbol": FX, "bid": 1.1000, "ask": 1.10008,
        "tick_value": 1.0, "tick_size": 0.0001}
    p = engine.feed_state["prices"][FX]
    assert p["src"] is None and p["src_kind"] == "unattributed", p
    assert p["src_t"] > 0, "arrival time is knowable and must be recorded"
    assert engine.MT5_SOURCE not in (engine.feed_state.get("sources") or {}), \
        "an unattributed write must not invent a source, let alone a healthy one"

    st = engine.price_state(FX)
    assert st["source"] is None and st["reachable"] is False, st
    assert engine.price_for_entry(FX)[0] is None, \
        "a quote from nobody is not a quote you may open on"

    engine.feed_state["prices"][FX]["src_t"] = \
        time.time() - engine.MT5_PRICE_MAX_AGE_S - 30
    assert engine.price_state(FX)["fresh"] is False, "it must still age out"


def test_a_declared_source_is_believed_but_never_resurrected():
    """The other side of that line. A writer that NAMES its source is making a
    claim, and a claim is evidence the source produced a print, so an unknown
    source becomes known. But a direct write must never revive a source a failed
    poll already marked down, or one quote undoes an outage."""
    _reset_feed()
    _no_venues()
    engine.feed_state["prices"][FX] = {
        "symbol": FX, "bid": 1.1000, "ask": 1.10008,
        "tick_value": 1.0, "tick_size": 0.0001,
        "src": engine.MT5_SOURCE, "src_t": time.time()}
    assert engine.price_state(FX)["reachable"] is True
    assert engine.price_for_entry(FX)[0] is not None

    engine.feed_state["sources"][engine.MT5_SOURCE]["reachable"] = False
    engine.feed_state["prices"][FX] = {
        "symbol": FX, "bid": 1.1001, "ask": 1.10018,
        "tick_value": 1.0, "tick_size": 0.0001,
        "src": engine.MT5_SOURCE, "src_t": time.time()}
    assert engine.price_state(FX)["reachable"] is False, \
        "a direct write resurrected a source a failed poll had marked down"


def test_a_future_stamp_is_clock_skew_not_a_fresh_price():
    """fresh = age <= max_age had no LOWER bound, so a quote stamped in the
    future was fresh forever - the same permanently-tradable state this rail
    exists to prevent, reached from the other direction."""
    _reset_feed()
    _no_venues()
    engine.feed_state["prices"][FX] = {
        "symbol": FX, "bid": 1.1000, "ask": 1.10008,
        "tick_value": 1.0, "tick_size": 0.0001,
        "src": engine.MT5_SOURCE, "src_t": time.time() + 3600}
    st = engine.price_state(FX)
    assert st["fresh"] is False, st
    assert "FUTURE" in st["reason"], st["reason"]
    assert engine.price_for_entry(FX)[0] is None

    # a fraction of a second is tolerated: clocks are never exactly equal
    engine.feed_state["prices"][FX]["src_t"] = time.time() + 0.2
    assert engine.price_state(FX)["fresh"] is True


def test_a_quote_with_no_clock_fails_closed():
    """The rail itself, with the write layer bypassed: no timestamp means the
    age is unknown, and unknown may never come back as fresh."""
    _reset_feed()
    _no_venues()
    dict.__setitem__(engine.feed_state["prices"], FX,
                     {"symbol": FX, "bid": 1.1000, "ask": 1.10008,
                      "tick_value": 1.0, "tick_size": 0.0001})
    st = engine.price_state(FX)
    assert st["fresh"] is False and st["reachable"] is False, st
    assert st["price"] is None and st["age"] is None, st
    assert "no timestamp" in st["reason"], st["reason"]
    engine.try_execute(_sig(FX, entry=1.10008, sl=1.0950, key="clock|none"),
                       _params())
    assert storage.open_trades("paper") == [], \
        "an unclocked quote must never fill an entry"


def test_a_source_nobody_has_heard_from_is_not_reachable():
    _reset_feed()
    _no_venues()
    dict.__setitem__(engine.feed_state["prices"], CRYPTO,
                     {"symbol": CRYPTO, "bid": 60000.0, "ask": 60001.0,
                      "tick_value": 0.01, "tick_size": 0.01,
                      "src": "ghost", "src_t": time.time()})
    st = engine.price_state(CRYPTO)
    assert st["fresh"] is True, st
    assert st["reachable"] is False, "an unregistered source is unknown, not ok"
    assert engine.price_for_entry(CRYPTO)[0] is None, st


# ------------------- 8. adapter text never reaches a reason or a message
_LEAK = ('binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions"} '
         'apiKey=AKIAV7QZEXAMPLEKEY9 secret=s3cr3tZZ9 '
         'signature=deadbeefcafe url=https://api.binance.com/api/v3/account')
_LEAK_BITS = ("AKIAV7QZEXAMPLEKEY9", "s3cr3tZZ9", "deadbeefcafe", "apiKey")


class _CcxtAuthenticationError(Exception):
    """Shaped like the real thing: ccxt puts the request it sent in the text."""


def _assert_no_leak(where, blob):
    for bit in _LEAK_BITS:
        assert bit not in blob, "%s leaked %r" % (where, bit)


def test_adapter_exception_text_never_leaves_the_engine():
    _reset_feed()
    # a venue that WAS working and then throws: the path where the raw text
    # reached price_state -> skip(reason) -> signals.reason + notify()
    _venue_push(bid=60000.0, ask=60001.0)
    _install_venue(adapter=_FakeVenue(raises=_CcxtAuthenticationError(_LEAK)))
    errs = engine.refresh_venue_prices([CRYPTO])
    _assert_no_leak("returned error", json.dumps(errs))
    assert errs[VENUE] == "auth", errs

    st = engine.source_state(VENUE)
    _assert_no_leak("source record", json.dumps(st))
    # and the doubled prefix is gone: "unreachable: unreachable: <raw>"
    assert st["reason"].count("unreachable:") == 1, st["reason"]
    assert st["reason"] == "%s unreachable: auth" % VENUE, st["reason"]

    engine.try_execute(_sig(CRYPTO, key="leak|1"), _params())
    _assert_no_leak("persisted signal reason", _last_signal_reason())
    rows = storage.query("SELECT * FROM decisions")
    _assert_no_leak("persisted decision", json.dumps(rows, default=str))
    _assert_no_leak("notification", "\n".join(_notes))
    assert _notes, "the skip notification must actually have been emitted"


def test_source_detail_is_bounded_at_the_write_layer():
    """A future call site that forgets to classify must not be able to store
    raw text: the store refuses it, not the caller."""
    _reset_feed()
    engine._set_source(VENUE, "venue", reachable=False, detail=_LEAK)
    st = engine.source_state(VENUE)
    _assert_no_leak("write-layer detail", json.dumps(st))
    assert st["detail"] == "unclassified adapter error", st["detail"]


def test_error_classification_labels():
    class AuthenticationError(Exception):
        pass

    class RequestTimeout(Exception):
        pass

    class ExchangeNotAvailable(Exception):
        pass

    got = [engine.classify_adapter_error(e(_LEAK))
           for e in (AuthenticationError, RequestTimeout, ExchangeNotAvailable)]
    assert got == ["auth", "timeout", "network"], got
    _assert_no_leak("classification", "|".join(got))
    assert engine.classify_adapter_error(ValueError(_LEAK)) == \
        "adapter error (ValueError)"


# ------------- 9. a position we could not evaluate is never silent
def test_unmanaged_position_is_recorded_and_announced():
    """A held symbol with no price used to `continue` silently: no decisions
    row, no notification, nothing in _last_info — while the stop that only this
    loop enforces went unevaluated for the whole outage."""
    _reset_feed()
    _no_venues()
    tid = _open_trade(CRYPTO)
    engine.manage_open_trades(_params())
    d = _last_decision("manage")
    assert d and d["action"] == "unmanaged" and d["symbol"] == CRYPTO, d
    assert "no live price" in (d["reason"] or ""), d["reason"]
    note = engine._last_info.get("manage|%s" % tid)
    assert note and "UNMANAGED" in note["note"], note
    assert any("POSITION UNMANAGED" in n for n in _notes), _notes

    # it must not re-announce every 20s, but it must keep saying so
    n_before = len(_notes)
    engine.manage_open_trades(_params())
    assert len(_notes) == n_before, "one announcement per outage, not per cycle"
    assert engine._last_info["manage|%s" % tid]["note"], "still reported"

    # and when the quote comes back, management resumes and says so
    _venue_push(bid=61050.0, ask=61051.0)
    engine.manage_open_trades(_params())
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["tp1_done"] == 1, "management must resume"
    assert "manage|%s" % tid not in engine._last_info
    assert any("Management resumed" in n for n in _notes), _notes
    _wipe("trades")


def test_shadow_position_is_audited_but_never_notifies():
    """Invariant 6: shadow trades are silent. They still get the audit row."""
    _reset_feed()
    _no_venues()
    _open_trade(CRYPTO, mode="shadow")
    engine.manage_open_trades(_params())
    d = _last_decision("manage")
    assert d and d["action"] == "unmanaged", d
    assert not any("UNMANAGED" in n for n in _notes), _notes
    _wipe("trades")


def test_unmanaged_does_not_flip_flop_on_the_live_path():
    """A live position with a good price but a stale MT5 drop copy is gated
    AFTER the price check. Announcing "unmanaged" then "resumed" every cycle
    for the length of an outage would train a human to ignore both."""
    _reset_feed()
    _mt5_push(symbol=CRYPTO, bid=60500.0, ask=60501.0)
    _open_trade(CRYPTO, mode="live", ticket=555)
    engine.feed_state["open_positions"] = [
        {"ticket": 555, "comment": "SLC#1", "unrealized_pnl": 0.0, "swap": 0.0}]
    engine.manage_open_trades(_params(trading_mode="live"))   # healthy: silent
    assert not any("UNMANAGED" in n for n in _notes), _notes
    # now the EA goes quiet with the last print still inside the price limit
    engine.feed_state["sources"][engine.MT5_SOURCE]["last_t"] = time.time() - 600
    for _ in range(3):
        engine.manage_open_trades(_params(trading_mode="live"))
    assert sum("UNMANAGED" in n for n in _notes) == 1, _notes
    assert not any("resumed" in n for n in _notes), _notes
    _wipe("trades")


def test_zero_risk_distance_is_recorded_not_skipped():
    _reset_feed()
    _venue_push(bid=60000.0, ask=60001.0)
    _open_trade(CRYPTO, entry=60000.0, sl=60000.0, initial_sl=60000.0)
    engine.manage_open_trades(_params())
    d = _last_decision("manage")
    assert d and d["action"] == "unmanaged", d
    assert "risk distance is zero" in (d["reason"] or ""), d["reason"]
    _wipe("trades")


# ------------- 10. the MT5 drop-copy guard is scoped to MT5-held trades
def test_mt5_stale_does_not_suspend_a_venue_held_live_position():
    """The live branch was gated on the MT5 feed for EVERY live trade whatever
    venue held it — the same global-bypass shape this branch exists to delete.
    A venue-held position must be judged on its own terms, and since no venue
    execution path is wired, that judgement has to be said out loud."""
    _reset_feed()
    _mt5_push(age_s=600)                            # EA dead
    _venue_push(bid=60500.0, ask=60501.0)           # venue healthy
    tid = _open_trade(CRYPTO, mode="live")          # no ticket: not an MT5 fill
    engine.manage_open_trades(_params(trading_mode="live"))
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["status"] == "open", tr["status"]
    d = _last_decision("manage")
    assert d and d["action"] == "unmanaged", d
    assert "not held at MT5" in (d["reason"] or ""), d["reason"]
    assert "drop copy stale" not in (d["reason"] or ""), \
        "the MT5 guard must not be what refused a venue-held position"
    _wipe("trades")


# --------------------------------- 11. the loop body, end to end
def test_one_cycle_manages_and_analyses_without_any_mt5():
    """Drive the real loop body once with poll_seconds so small the sleep is
    irrelevant, and prove it did work rather than skipping to the sleep."""
    _reset_feed()
    _venue_push(bid=61050.0, ask=61051.0)
    tid = _open_trade(CRYPTO)
    storage.set_setting("enabled_pairs", [CRYPTO])
    storage.set_setting("watch_pairs", [])
    storage.set_setting("modes", ["swing"])

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
        engine.stop_venue_poller(join_s=5)
        assert not t.is_alive(), "engine_loop must honour stop()"
    _wipe("trades")


def _say(*parts):
    """The engine's notifications carry emoji, so an assertion message can hold
    a character the console encoding cannot. A test runner that dies printing a
    failure reports 0 failures, which is the worst possible answer."""
    line = " ".join(str(x) for x in parts)
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            _say("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            _say("FAIL", fn.__name__, "-", e)
        except Exception as e:
            failed += 1
            _say("ERR ", fn.__name__, "-", repr(e))
    _say("\n%d passed, %d failed" % (len(fns) - failed, failed))
    sys.exit(1 if failed else 0)
