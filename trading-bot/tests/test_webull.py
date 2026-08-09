"""Webull adapter tests — offline, no network, no venue, no MT5.

The Webull OpenAPI application has not been submitted, so nothing here can talk
to Webull and nothing here pretends to. What is tested is the part that is
ours: the contract, the idempotency path, the read-only refusal, and that the
absence of credentials is a health RESULT and not an exception. Every HTTP call
goes through a fake transport that records what the adapter would have sent.

Covers:
  * registry: kinds() carries webull, brokers.build and venues.adapter make one
  * health(): no credentials -> result, not a raise, and no socket touched
  * health(): transport failure -> result, not a raise
  * symbol_meta(): refuses rather than guessing tick size / lot step / min qty,
    and still refuses when the venue answer is missing one of them
  * place_order()/cancel(): read-only refusal
  * idempotency: probe before submit, recover after a dropped response, refuse
    to blind-retry when the order cannot be found
  * client_order_id: required, never truncated to fit Webull's 32-char cap
  * fail-closed refusals: attached stop, unsigned equity short, no
    instrument_type, unreadable balance, unreadable position
  * signing: deterministic, covers body and params, secret never transmitted

Run:  cd trading-bot && python3 tests/test_webull.py
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate DB writes BEFORE importing anything that touches storage
import storage
storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
storage.init()

import brokers
from brokers import webull
from brokers import (Balance, Order, OrderResult, Position, SymbolMeta,
                     VenueError, VenueHealth, VenueReadOnly)

CREDS = {"name": "wb", "api_key": "APPKEY123", "api_secret": "s3cr3t-app-secret",
         "account_id": "20150320010101001", "instrument_type": "EQUITY"}


# ------------------------------------------------------------- fake transport
class FakeHTTP:
    """Stands in for WebullVenue._transport. Records every request and answers
    from a routing table keyed on (method, path)."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append({"method": method, "path": parts.path, "query": query,
                           "body": json.loads(body) if body else None,
                           "headers": dict(headers)})
        handler = self.routes.get((method, parts.path))
        if handler is None:
            raise urllib.error.HTTPError(url, 404, "no route", {}, None)
        out = handler(query, json.loads(body) if body else None)
        if isinstance(out, Exception):
            raise out
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        return json.dumps(out), {"Date": date}

    def paths(self, method=None):
        return [c["path"] for c in self.calls
                if method is None or c["method"] == method]


def _venue(fake=None, **overrides):
    cfg = dict(CREDS)
    cfg.update(overrides)
    v = webull.WebullVenue(cfg)
    if fake is not None:
        v._transport = fake
    return v


def _exploding_transport(*a, **kw):
    raise AssertionError("adapter touched the network when it must not")


def _raises(fn, exc=VenueError):
    try:
        fn()
    except exc as e:
        return e
    except Exception as e:                       # wrong exception type is a fail
        raise AssertionError("expected %s, got %r" % (exc.__name__, e))
    raise AssertionError("expected %s, nothing raised" % exc.__name__)


ACCOUNTS_OK = {"accounts": [{"account_id": "20150320010101001"}]}
BALANCE_OK = {"account_currency_assets": [
    {"currency": "USD", "total_amount": "10000.00", "cash_balance": "4000.00"}]}


# -------------------------------------------------------------- registration
def test_adapter_is_registered():
    assert "webull" in brokers.kinds(), brokers.kinds()


def test_brokers_build_makes_one():
    a = brokers.build("webull", dict(CREDS))
    assert isinstance(a, webull.WebullVenue)
    assert a.name == "wb"


def test_satisfies_the_broker_adapter_contract():
    a = brokers.build("webull", dict(CREDS))
    assert isinstance(a, brokers.BrokerAdapter)
    for m in ("health", "symbol_meta", "balances", "positions", "place_order",
              "cancel", "stream_prices"):
        assert callable(getattr(a, m)), m


def test_venues_can_build_and_health_it():
    import venues
    venues.upsert({"name": "wb-test", "kind": "webull"})
    a = venues.adapter("wb-test")
    assert isinstance(a, webull.WebullVenue)
    h = venues.health("wb-test")          # must not raise, must not look healthy
    assert h["reachable"] is False and h["authenticated"] is False
    assert h["read_only"] is True, "a new venue must arrive disarmed"
    venues.remove("wb-test")


def test_read_only_is_the_default():
    assert webull.WebullVenue({"name": "wb"}).read_only is True


# --------------------------------------------------------------------- health
def test_health_without_credentials_is_a_result_not_an_exception():
    v = _venue(_exploding_transport, api_key="", api_secret="")
    h = v.health()
    assert isinstance(h, VenueHealth)
    assert h.reachable is False and h.authenticated is False
    assert h.read_only is True
    low = h.detail.lower()
    assert "app key" in low or "key" in low, h.detail
    assert "application" in low and "review" in low, \
        "health must say WHY there are no credentials, not just that there are none"


def test_health_without_credentials_never_calls_out():
    fake = FakeHTTP()
    v = _venue(fake, api_key="", api_secret="")
    v.health()
    assert fake.calls == [], "unconfigured venue must not open a connection"


def test_health_survives_an_unreachable_venue():
    def boom(*a, **kw):
        raise urllib.error.URLError("dns is down")
    v = _venue(boom)
    h = v.health()
    assert h.reachable is False and h.authenticated is False
    assert "unreachable" in h.detail.lower(), h.detail


def test_health_survives_an_http_error():
    def boom(*a, **kw):
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    v = _venue(boom)
    h = v.health()
    assert h.authenticated is False
    assert "401" in h.detail, h.detail


def test_health_reports_accounts_balances_and_skew():
    fake = FakeHTTP({
        ("GET", "/trading/accounts/list"): lambda q, b: ACCOUNTS_OK,
        ("GET", "/trading/assets/balances/get"): lambda q, b: BALANCE_OK,
    })
    h = _venue(fake).health()
    assert h.reachable and h.authenticated
    assert [b.currency for b in h.balances] == ["USD"]
    assert h.balances[0].total == 10000.0 and h.balances[0].free == 4000.0
    assert h.latency_ms is not None
    assert h.clock_skew_s is not None, "Date header should give a skew reading"
    # a whole-timezone reading here would mean the header was parsed as local
    assert abs(h.clock_skew_s) < 60, h.clock_skew_s


def test_health_degrades_when_balances_are_unreadable():
    fake = FakeHTTP({
        ("GET", "/trading/accounts/list"): lambda q, b: ACCOUNTS_OK,
        ("GET", "/trading/assets/balances/get"): lambda q, b: {"unexpected": 1},
    })
    h = _venue(fake).health()
    assert h.authenticated is True
    assert "balances" in h.detail, h.detail


# ---------------------------------------------------------------- symbol_meta
def test_symbol_meta_refuses_to_guess():
    e = _raises(lambda: _venue(FakeHTTP()).symbol_meta("AAPL"))
    low = str(e).lower()
    assert "guess" in low or "unverified" in low, str(e)
    assert "tick size" in low, str(e)


def test_symbol_meta_still_refuses_when_a_field_is_missing():
    fake = FakeHTTP({("GET", "/trading/instruments/stock/list"):
                     lambda q, b: {"instruments": [
                         {"symbol": "AAPL", "tick": "0.01", "min_q": "1"}]}})
    v = _venue(fake, instrument_path="/trading/instruments/stock/list",
               instrument_fields={"tick_size": "tick", "lot_step": "lot",
                                  "min_qty": "min_q"})
    e = _raises(lambda: v.symbol_meta("AAPL"))
    assert "lot_step" in str(e), str(e)


def test_symbol_meta_maps_a_verified_response():
    fake = FakeHTTP({("GET", "/trading/instruments/stock/list"):
                     lambda q, b: {"instruments": [
                         {"symbol": "AAPL", "tick": "0.01", "lot": "0.00001",
                          "min_q": "0.00001", "min_n": "5"}]}})
    v = _venue(fake, instrument_path="/trading/instruments/stock/list",
               instrument_fields={"tick_size": "tick", "lot_step": "lot",
                                  "min_qty": "min_q", "min_notional": "min_n"})
    m = v.symbol_meta("AAPL")
    assert isinstance(m, SymbolMeta)
    assert m.tick_size == 0.01 and m.lot_step == 1e-05
    assert m.min_qty == 1e-05 and m.min_notional == 5.0
    assert m.venue_symbol == "AAPL" and m.asset_class == "equity"


def test_symbol_meta_refuses_a_zero_increment():
    fake = FakeHTTP({("GET", "/i"): lambda q, b: {"instruments": [
        {"symbol": "AAPL", "tick": "0", "lot": "1", "min_q": "1"}]}})
    v = _venue(fake, instrument_path="/i",
               instrument_fields={"tick_size": "tick", "lot_step": "lot",
                                  "min_qty": "min_q"})
    assert "tick_size" in str(_raises(lambda: v.symbol_meta("AAPL")))


# ------------------------------------------------------------------ read-only
def test_place_order_refused_while_read_only():
    v = _venue(_exploding_transport, read_only=True)
    o = Order(client_order_id="slc-1", symbol="AAPL", side="buy", qty=1)
    e = _raises(lambda: v.place_order(o), VenueReadOnly)
    assert "read-only" in str(e)


def test_cancel_refused_while_read_only():
    v = _venue(_exploding_transport, read_only=True)
    _raises(lambda: v.cancel("order-1"), VenueReadOnly)


def test_read_only_refusal_happens_before_any_request():
    fake = FakeHTTP()
    v = _venue(fake, read_only=True)
    _raises(lambda: v.place_order(Order("slc-1", "AAPL", "buy", 1)), VenueReadOnly)
    assert fake.calls == [], "a read-only venue must not even probe"


# ---------------------------------------------------------------- idempotency
def _armed(routes, **overrides):
    fake = FakeHTTP(routes)
    return _venue(fake, read_only=False, **overrides), fake


def _not_found(q, b):
    return urllib.error.HTTPError("u", 417, "no such order", {}, None)


def _placed(q, b):
    return {"orders": [{"order_id": "WB-9001",
                        "client_order_id": b["new_orders"][0]["client_order_id"],
                        "status": "PENDING"}]}


def test_place_order_probes_before_submitting():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    r = v.place_order(Order("slc-abc", "AAPL", "buy", 3))
    assert isinstance(r, OrderResult)
    assert fake.paths() == ["/trading/orders/get", "/trading/orders/place"], \
        "the client_order_id probe must come first, every time"
    assert r.venue_order_id == "WB-9001" and r.status == "accepted"
    sent = fake.calls[-1]["body"]["new_orders"][0]
    assert sent["client_order_id"] == "slc-abc"
    assert sent["quantity"] == "3" and sent["side"] == "BUY"
    assert sent["order_type"] == "MARKET" and sent["entrust_type"] == "QTY"


def test_place_order_is_idempotent_on_client_order_id():
    seen = {"order_id": "WB-7", "client_order_id": "slc-abc", "status": "FILLED",
            "filled_quantity": "3", "avg_filled_price": "180.25"}
    v, fake = _armed({("GET", "/trading/orders/get"): lambda q, b: seen,
                      ("POST", "/trading/orders/place"): _placed})
    r = v.place_order(Order("slc-abc", "AAPL", "buy", 3))
    assert fake.paths("POST") == [], "a known client_order_id must never resubmit"
    assert r.venue_order_id == "WB-7" and r.status == "filled"
    assert r.filled_qty == 3.0 and r.avg_price == 180.25
    assert "idempotent" in r.message


def test_probe_is_keyed_on_the_client_order_id():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    v.place_order(Order("slc-key-1", "AAPL", "buy", 1))
    assert fake.calls[0]["query"]["client_order_id"] == "slc-key-1"


def test_dropped_response_is_recovered_not_resubmitted():
    """The double-fill window: the submit lands, the answer does not."""
    state = {"posted": 0}

    def flaky_post(q, b):
        state["posted"] += 1
        return urllib.error.HTTPError("u", 503, "gateway", {}, None)

    def probe(q, b):
        if state["posted"] == 0:
            return urllib.error.HTTPError("u", 417, "no such order", {}, None)
        return {"order_id": "WB-42", "client_order_id": "slc-drop",
                "status": "FILLED", "filled_quantity": "2"}

    v, fake = _armed({("GET", "/trading/orders/get"): probe,
                      ("POST", "/trading/orders/place"): flaky_post})
    r = v.place_order(Order("slc-drop", "AAPL", "buy", 2))
    assert state["posted"] == 1, "must not resubmit after a transport failure"
    assert r.venue_order_id == "WB-42" and r.status == "filled"
    assert "despite transport error" in r.message


def test_lost_order_that_cannot_be_found_fails_closed():
    v, fake = _armed({
        ("GET", "/trading/orders/get"): _not_found,
        ("POST", "/trading/orders/place"):
            lambda q, b: urllib.error.HTTPError("u", 503, "gateway", {}, None)})
    e = _raises(lambda: v.place_order(Order("slc-lost", "AAPL", "buy", 1)))
    assert e.retryable is True
    assert "not found" in str(e), str(e)
    assert len(fake.paths("POST")) == 1, "no blind retry inside the adapter"


def test_business_rejection_is_not_retried():
    v, fake = _armed({
        ("GET", "/trading/orders/get"): _not_found,
        ("POST", "/trading/orders/place"):
            lambda q, b: urllib.error.HTTPError("u", 417, "buying power", {}, None)})
    e = _raises(lambda: v.place_order(Order("slc-rej", "AAPL", "buy", 1)))
    assert e.retryable is False, "a rejection is an answer, not a transport blip"
    assert len(fake.calls) == 2, "a rejection must not trigger a second probe"


def test_client_order_id_is_required():
    v, _ = _armed({})
    e = _raises(lambda: v.place_order(Order("", "AAPL", "buy", 1)))
    assert "client_order_id" in str(e)


def test_overlong_client_order_id_is_refused_not_truncated():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    e = _raises(lambda: v.place_order(Order("x" * 33, "AAPL", "buy", 1)))
    assert "32" in str(e)
    assert fake.calls == [], \
        "truncating would map two orders onto one idempotency key"


def test_generated_client_order_id_fits_the_venue_limit():
    for _ in range(50):
        cid = webull.new_client_order_id()
        assert 0 < len(cid) <= 32, cid
    assert len(set(webull.new_client_order_id() for _ in range(200))) == 200


# --------------------------------------------------------------- fail closed
def test_attached_stop_is_refused_rather_than_dropped():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    e = _raises(lambda: v.place_order(
        Order("slc-sl", "AAPL", "buy", 1, stop_loss=170.0)))
    assert "unprotected" in str(e).lower(), str(e)
    assert fake.calls == [], "must not open a position whose stop was dropped"


def test_attached_target_is_refused_too():
    v, _ = _armed({})
    _raises(lambda: v.place_order(
        Order("slc-tp", "AAPL", "buy", 1, take_profit=200.0)))


def test_equity_short_refused_unless_the_operator_allowed_it():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    e = _raises(lambda: v.place_order(Order("slc-s", "AAPL", "sell", 1)))
    assert "short" in str(e).lower()
    assert fake.calls == []


def test_closing_a_long_is_not_treated_as_a_short():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    v.place_order(Order("slc-c", "AAPL", "sell", 1, reduce_only=True))
    assert fake.calls[-1]["body"]["new_orders"][0]["side"] == "SELL"


def test_short_is_sent_as_SHORT_when_armed():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed},
                     allow_short=True)
    v.place_order(Order("slc-s2", "AAPL", "sell", 1))
    assert fake.calls[-1]["body"]["new_orders"][0]["side"] == "SHORT"


def test_missing_instrument_type_is_refused():
    v = _venue(FakeHTTP(), read_only=False, instrument_type=None)
    e = _raises(lambda: v.place_order(Order("slc-i", "AAPL", "buy", 1)))
    assert "instrument_type" in str(e)


def test_limit_order_without_a_price_is_refused():
    v, _ = _armed({})
    _raises(lambda: v.place_order(
        Order("slc-l", "AAPL", "buy", 1, order_type="limit")))


def test_ambiguous_account_is_refused():
    fake = FakeHTTP({("GET", "/trading/accounts/list"):
                     lambda q, b: {"accounts": [{"account_id": "1"},
                                                {"account_id": "2"}]}})
    v = _venue(fake, account_id=None)
    e = _raises(lambda: v.balances())
    assert "account_id" in str(e), str(e)


def test_small_quantities_are_not_sent_in_exponent_notation():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed},
                     instrument_type="CRYPTO")
    v.place_order(Order("slc-tiny", "BTCUSD", "buy", 0.00000123))
    q = fake.calls[-1]["body"]["new_orders"][0]["quantity"]
    assert "e" not in q.lower(), q
    assert float(q) == 0.00000123


# --------------------------------------------------------------- reads detail
def test_positions_are_parsed():
    fake = FakeHTTP({("GET", "/trading/assets/positions/list"): lambda q, b: {
        "positions": [{"symbol": "AAPL", "quantity": "10", "cost_price": "180.5",
                       "unrealized_profit_loss": "25.5",
                       "position_id": "P1"}]}})
    p = _venue(fake).positions()
    assert len(p) == 1 and isinstance(p[0], Position)
    assert p[0].symbol == "AAPL" and p[0].side == "buy" and p[0].qty == 10.0
    assert p[0].entry_price == 180.5 and p[0].unrealized_pnl == 25.5
    assert p[0].venue_id == "P1" and p[0].raw


def test_negative_quantity_reads_as_a_short():
    fake = FakeHTTP({("GET", "/trading/assets/positions/list"): lambda q, b: {
        "positions": [{"symbol": "AAPL", "quantity": "-4"}]}})
    p = _venue(fake).positions()
    assert p[0].side == "sell" and p[0].qty == 4.0


def test_unreadable_position_raises_rather_than_reading_as_flat():
    fake = FakeHTTP({("GET", "/trading/assets/positions/list"): lambda q, b: {
        "positions": [{"symbol": "AAPL", "shares_maybe": "10"}]}})
    e = _raises(lambda: _venue(fake).positions())
    assert "quantity" in str(e)


def test_unreadable_balance_raises_rather_than_reporting_zero():
    fake = FakeHTTP({("GET", "/trading/assets/balances/get"):
                     lambda q, b: {"something_else": []}})
    e = _raises(lambda: _venue(fake).balances())
    assert "sizing" in str(e).lower(), str(e)


def test_balances_are_typed():
    fake = FakeHTTP({("GET", "/trading/assets/balances/get"):
                     lambda q, b: BALANCE_OK})
    bals = _venue(fake).balances()
    assert all(isinstance(b, Balance) for b in bals)


def test_error_envelope_is_not_read_through():
    fake = FakeHTTP({("GET", "/trading/accounts/list"):
                     lambda q, b: {"code": "417", "msg": "bad app", "data": []}})
    e = _raises(lambda: _venue(fake).accounts())
    assert "417" in str(e)


def test_stream_prices_yields_nothing_here():
    assert list(_venue(FakeHTTP()).stream_prices(["AAPL"])) == []


# ------------------------------------------------------------------- signing
def _sig(v, path="/trading/orders/place", params=None, body="", nonce="n1",
         ts="2026-08-09T12:00:00Z"):
    headers = {"host": v._host, "x-app-key": v._key,
               "x-signature-algorithm": "HMAC-SHA1", "x-signature-version": "1.0",
               "x-signature-nonce": nonce, "x-timestamp": ts}
    return v._sign(path, params or {}, body, headers)


def test_signature_is_deterministic():
    v = _venue()
    assert _sig(v) == _sig(v)


def test_signature_covers_body_params_path_and_nonce():
    v = _venue()
    base = _sig(v)
    assert _sig(v, body='{"a":1}') != base, "body must be signed"
    assert _sig(v, params={"account_id": "1"}) != base, "params must be signed"
    assert _sig(v, path="/trading/orders/cancel") != base, "path must be signed"
    assert _sig(v, nonce="n2") != base, "nonce must be signed"
    assert _sig(v, ts="2026-08-09T12:00:01Z") != base, "timestamp must be signed"


def test_signature_depends_on_the_secret():
    assert _sig(_venue()) != _sig(_venue(api_secret="other-secret"))


def test_request_carries_the_documented_auth_headers():
    fake = FakeHTTP({("GET", "/trading/accounts/list"): lambda q, b: ACCOUNTS_OK})
    _venue(fake).accounts()
    h = fake.calls[0]["headers"]
    for name in ("x-app-key", "x-timestamp", "x-signature", "x-signature-nonce",
                 "x-signature-algorithm", "x-signature-version"):
        assert h.get(name), "missing %s" % name
    assert h["x-signature-algorithm"] == "HMAC-SHA1"
    assert h["x-timestamp"].endswith("Z") and "T" in h["x-timestamp"]


def test_the_secret_is_never_transmitted():
    fake = FakeHTTP({("GET", "/trading/accounts/list"): lambda q, b: ACCOUNTS_OK})
    v = _venue(fake)
    v.accounts()
    blob = json.dumps(fake.calls)
    assert v._secret not in blob, "app secret must never leave the process"


def test_nonce_changes_every_request():
    fake = FakeHTTP({("GET", "/trading/accounts/list"): lambda q, b: ACCOUNTS_OK})
    v = _venue(fake)
    v.accounts()
    v.accounts()
    assert (fake.calls[0]["headers"]["x-signature-nonce"]
            != fake.calls[1]["headers"]["x-signature-nonce"])


def test_production_host_is_the_default():
    assert _venue()._host == webull.HOST_PROD
    assert _venue(sandbox=True)._host == webull.HOST_UAT


if __name__ == "__main__":
    # Not a test: brokers/__init__.py owns the import-side-effect registration
    # list and is outside this change. Say so loudly rather than failing a suite
    # another agent is mid-edit on.
    _init = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "brokers", "__init__.py")
    with open(_init) as _f:
        if "import webull" not in _f.read():
            print("WARN  brokers/__init__.py does not import webull: "
                  "venues.py cannot build this kind until it does\n")

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
