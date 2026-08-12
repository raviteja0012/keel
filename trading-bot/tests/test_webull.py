"""Webull adapter tests — offline, no network, no venue, no MT5.

The Webull OpenAPI application has not been submitted, so nothing here can talk
to Webull and nothing here pretends to. What is tested is the part that is
ours: the contract, the idempotency path, the read-only refusal, and that the
absence of credentials is a health RESULT and not an exception. Every HTTP call
goes through a fake transport that records what the adapter would have sent.

Covers:
  * registry: a PLAIN `import brokers` in a clean subprocess registers webull,
    and venues.upsert(kind="webull") builds one — importing the module from the
    test would prove only that the test imported it
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

The W1..W5 sections below are the regressions from the DO-NOT-SHIP review.
Each asserts the BEHAVIOUR that was wrong — a submission that happened, a
phantom order that was returned, a cancel that was reported — not the shape of
the code that produced it.

  W1  an indeterminate probe must never read as "no order there"
  W2  the probe must verify the order it got back is the order it asked for
  W3  the adapter must be registered by production import, not by the test
  W4  cancel() must read the response before claiming the cancel happened
  W5  NaN and Infinity must not pass a comparison-based numeric guard

Run:  cd trading-bot && python3 tests/test_webull.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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

NAN = float("nan")
INF = float("inf")


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
        # json.dumps renders NaN/Infinity as bare literals and json.loads reads
        # them straight back, which is exactly how a non-finite number arrives
        # from a real venue that serialises floats the same way.
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


# ------------------------------------------------------- W3 real registration
def _subprocess(code, what):
    """Run code in a clean interpreter rooted at trading-bot. Nothing this test
    module imported is visible in there, which is the whole point."""
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, "%s failed (rc=%d)\nstdout: %s\nstderr: %s" % (
        what, p.returncode, p.stdout.strip(), p.stderr.strip()[-1500:])
    return p.stdout


def test_w3_a_plain_import_of_brokers_registers_webull():
    """The old suite imported brokers.webull at module top, so `kinds()`
    contained webull because the TEST had imported it. In production nothing
    did, and venues.upsert(kind="webull") raised 'unknown kind'."""
    out = _subprocess(
        "import sys\n"
        "import brokers\n"
        "assert 'brokers.webull' not in sys.modules or True\n"
        "ks = brokers.kinds()\n"
        "assert 'webull' in ks, 'kinds() = %r' % (ks,)\n"
        "assert 'brokers.webull' in sys.modules, 'registered without importing?'\n"
        "print('KINDS ' + ','.join(ks))\n",
        "plain `import brokers`")
    assert "webull" in out, out
    assert "KINDS" in out, out


def test_w3_venues_builds_a_webull_adapter_in_a_clean_process():
    """The production path end to end: DB-backed venue registry, no test-side
    import of the adapter module anywhere."""
    out = _subprocess(
        "import os, tempfile\n"
        "import storage\n"
        "storage._DB_PATH = os.path.join(tempfile.mkdtemp(), 'sub.db')\n"
        "storage.init()\n"
        "import venues\n"
        "venues.upsert({'name': 'wb-sub', 'kind': 'webull'})\n"
        "a = venues.adapter('wb-sub')\n"
        "assert a.read_only is True, 'a new venue must arrive disarmed'\n"
        "h = venues.health('wb-sub')\n"
        "assert h['authenticated'] is False\n"
        "print('BUILT ' + type(a).__name__)\n",
        "venues.upsert(kind='webull')")
    assert "BUILT WebullVenue" in out, out


def test_w3_unknown_kinds_still_refuse():
    """The registration fix must not have turned build() into a shrug."""
    e = _raises(lambda: brokers.build("webu11", {}))
    assert "unknown venue kind" in str(e), str(e)


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
    """A DEFINITE negative from the venue: this order does not exist."""
    return urllib.error.HTTPError("u", 404, "no such order", {}, None)


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
            return urllib.error.HTTPError("u", 404, "no such order", {}, None)
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


# =============================================================== W1: the probe
# An INDETERMINATE probe must never be treated as absence. Proved before the
# fix: two calls with the same client_order_id and an unreachable probe both
# times produced TWO submissions.

def _unreachable(q, b):
    return urllib.error.URLError("connection refused")


def _count_posts(state):
    def post(q, b):
        state["posted"] += 1
        return _placed(q, b)
    return post


def test_w1_unreachable_probe_refuses_to_submit():
    state = {"posted": 0}
    v, fake = _armed({("GET", "/trading/orders/get"): _unreachable,
                      ("POST", "/trading/orders/place"): _count_posts(state)})
    e = _raises(lambda: v.place_order(Order("slc-w1a", "AAPL", "buy", 1)))
    assert state["posted"] == 0, "an unreachable probe must not authorise a submit"
    assert fake.paths("POST") == []
    low = str(e).lower()
    assert "cannot confirm" in low, str(e)
    assert "slc-w1a" in str(e), "the refusal must name the order it refused"


def test_w1_two_calls_with_an_unreachable_probe_never_submit_twice():
    """The exact proof from the review: same client_order_id, probe unreachable
    both times. Before the fix this produced two submissions."""
    state = {"posted": 0}
    v, fake = _armed({("GET", "/trading/orders/get"): _unreachable,
                      ("POST", "/trading/orders/place"): _count_posts(state)})
    order = Order("slc-w1-dup", "AAPL", "buy", 5)
    _raises(lambda: v.place_order(order))
    _raises(lambda: v.place_order(order))
    assert state["posted"] == 0, \
        "two calls, probe blind both times, %d submissions" % state["posted"]
    assert fake.paths("POST") == []
    assert fake.paths("GET").count("/trading/orders/get") == 2, \
        "each attempt must still probe; it just must not submit on a blind one"


def test_w1_probe_401_is_not_read_as_absence():
    state = {"posted": 0}
    v, _ = _armed({("GET", "/trading/orders/get"):
                   lambda q, b: urllib.error.HTTPError("u", 401, "bad key", {}, None),
                   ("POST", "/trading/orders/place"): _count_posts(state)})
    e = _raises(lambda: v.place_order(Order("slc-w1b", "AAPL", "buy", 1)))
    assert state["posted"] == 0, "an auth failure says nothing about the order"
    assert "401" in str(e), str(e)


def test_w1_probe_500_is_not_read_as_absence():
    state = {"posted": 0}
    v, _ = _armed({("GET", "/trading/orders/get"):
                   lambda q, b: urllib.error.HTTPError("u", 500, "boom", {}, None),
                   ("POST", "/trading/orders/place"): _count_posts(state)})
    e = _raises(lambda: v.place_order(Order("slc-w1c", "AAPL", "buy", 1)))
    assert state["posted"] == 0
    assert e.retryable is True, "a 5xx probe failure is transient; retry is safe"


def test_w1_probe_417_is_not_read_as_absence_by_default():
    """417 is Webull's ordinary business-rejection status. It cannot double as
    'no such order' unless an operator has actually observed that."""
    state = {"posted": 0}
    v, _ = _armed({("GET", "/trading/orders/get"):
                   lambda q, b: urllib.error.HTTPError("u", 417, "nope", {}, None),
                   ("POST", "/trading/orders/place"): _count_posts(state)})
    _raises(lambda: v.place_order(Order("slc-w1d", "AAPL", "buy", 1)))
    assert state["posted"] == 0


def test_w1_probe_empty_200_is_not_read_as_absence():
    state = {"posted": 0}
    v, _ = _armed({("GET", "/trading/orders/get"): lambda q, b: {},
                   ("POST", "/trading/orders/place"): _count_posts(state)})
    e = _raises(lambda: v.place_order(Order("slc-w1e", "AAPL", "buy", 1)))
    assert state["posted"] == 0
    assert "absence" in str(e).lower(), str(e)


def test_w1_probe_error_envelope_is_not_read_as_absence():
    state = {"posted": 0}
    v, _ = _armed({("GET", "/trading/orders/get"):
                   lambda q, b: {"code": "60001", "msg": "system busy"},
                   ("POST", "/trading/orders/place"): _count_posts(state)})
    _raises(lambda: v.place_order(Order("slc-w1f", "AAPL", "buy", 1)))
    assert state["posted"] == 0


def test_w1_a_definite_not_found_still_places():
    """The fix must refuse uncertainty, not refuse everything: a venue that
    definitely says 'no such order' still gets the submit."""
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    r = v.place_order(Order("slc-w1g", "AAPL", "buy", 1))
    assert r.venue_order_id == "WB-9001"
    assert fake.paths("POST") == ["/trading/orders/place"]


def test_w1_indeterminate_recovery_probe_does_not_resubmit_or_claim_absence():
    """Submit lands, response drops, and the follow-up probe cannot reach the
    venue. The order may be live: say so, do not report it as not-placed."""
    state = {"posted": 0}

    def flaky_post(q, b):
        state["posted"] += 1
        return urllib.error.HTTPError("u", 503, "gateway", {}, None)

    def probe(q, b):
        if state["posted"] == 0:
            return urllib.error.HTTPError("u", 404, "no such order", {}, None)
        return urllib.error.URLError("connection reset")

    v, fake = _armed({("GET", "/trading/orders/get"): probe,
                      ("POST", "/trading/orders/place"): flaky_post})
    e = _raises(lambda: v.place_order(Order("slc-w1h", "AAPL", "buy", 2)))
    assert state["posted"] == 1, "must not resubmit into an unknown outcome"
    low = str(e).lower()
    assert "may be live" in low, str(e)
    assert "not found at venue" not in low, \
        "an unreachable follow-up probe is not evidence the order is absent"
    assert e.retryable is False, \
        "this needs reconciliation, not a retry loop"


def test_w1_recovery_probe_finding_another_order_does_not_confirm_ours():
    state = {"posted": 0}

    def flaky_post(q, b):
        state["posted"] += 1
        return urllib.error.HTTPError("u", 503, "gateway", {}, None)

    def probe(q, b):
        if state["posted"] == 0:
            return urllib.error.HTTPError("u", 404, "no such order", {}, None)
        return {"order_id": "WB-SOMEONE-ELSE", "client_order_id": "slc-other",
                "status": "FILLED", "filled_quantity": "9"}

    v, _ = _armed({("GET", "/trading/orders/get"): probe,
                   ("POST", "/trading/orders/place"): flaky_post})
    e = _raises(lambda: v.place_order(Order("slc-w1i", "AAPL", "buy", 2)))
    assert state["posted"] == 1
    assert "may be live" in str(e).lower(), str(e)


def test_w1_config_cannot_make_a_transport_failure_mean_absent():
    for bad in (503, 500, 429, 401, 403):
        e = _raises(lambda bad=bad: _venue(order_not_found_http=[bad]))
        assert str(bad) in str(e), str(e)
        assert "second fill" in str(e), str(e)


def test_w1_config_can_add_an_observed_not_found_status():
    """Once a real 417 'order does not exist' has been seen, the operator can
    map it — and only then does 417 authorise a submit."""
    v, fake = _armed({("GET", "/trading/orders/get"):
                      lambda q, b: urllib.error.HTTPError("u", 417, "nope", {}, None),
                      ("POST", "/trading/orders/place"): _placed},
                     order_not_found_http=[417])
    r = v.place_order(Order("slc-w1j", "AAPL", "buy", 1))
    assert r.venue_order_id == "WB-9001"


def test_w1_venue_error_carries_the_status_that_made_the_call_fail():
    fake = FakeHTTP({("GET", "/trading/accounts/list"):
                     lambda q, b: urllib.error.HTTPError("u", 418, "teapot", {}, None)})
    e = _raises(lambda: _venue(fake).accounts())
    assert e.http_status == 418, e.http_status


def test_w1_unreachable_venue_error_has_no_status_at_all():
    """None is the signal that the venue never answered. It must not be
    confusable with a status code."""
    def boom(*a, **kw):
        raise urllib.error.URLError("dns is down")
    e = _raises(lambda: _venue(boom).accounts())
    assert e.http_status is None, e.http_status


# ============================================================ W2: probe identity
# The probe must verify the order it got back is the order it asked for.
# Proved before the fix: asked to place slc-brand-new, the probe returned an
# unrelated FILLED order, the adapter made zero POSTs and returned it.

def test_w2_probe_answer_about_a_different_order_is_refused():
    state = {"posted": 0}
    other = {"order_id": "WB-OTHER", "client_order_id": "slc-someone-else",
             "status": "FILLED", "filled_quantity": "100",
             "avg_filled_price": "999.99"}
    v, fake = _armed({("GET", "/trading/orders/get"): lambda q, b: other,
                      ("POST", "/trading/orders/place"): _count_posts(state)})
    e = _raises(lambda: v.place_order(Order("slc-brand-new", "AAPL", "buy", 1)))
    assert "slc-someone-else" in str(e), str(e)
    assert "slc-brand-new" in str(e), str(e)
    assert state["posted"] == 0, "an unverified answer is not permission to submit"


def test_w2_a_phantom_fill_is_never_returned_as_this_orders_result():
    """The engine-facing shape of the same bug: whatever happens, this call must
    not hand back an OrderResult describing somebody else's fill."""
    other = {"order_id": "WB-OTHER", "client_order_id": "slc-someone-else",
             "status": "FILLED", "filled_quantity": "100"}
    v, _ = _armed({("GET", "/trading/orders/get"): lambda q, b: other,
                   ("POST", "/trading/orders/place"): _placed})
    try:
        r = v.place_order(Order("slc-brand-new", "AAPL", "buy", 1))
    except VenueError:
        return                                    # refusing is the correct outcome
    raise AssertionError(
        "returned a result for a foreign order: id=%s status=%s filled=%s"
        % (r.venue_order_id, r.status, r.filled_qty))


def test_w2_probe_answer_with_no_client_order_id_is_refused():
    """order_id alone does not prove the answer is about our key."""
    state = {"posted": 0}
    v, _ = _armed({("GET", "/trading/orders/get"):
                   lambda q, b: {"order_id": "WB-77", "status": "FILLED"},
                   ("POST", "/trading/orders/place"): _count_posts(state)})
    e = _raises(lambda: v.place_order(Order("slc-w2c", "AAPL", "buy", 1)))
    assert "none carrying client_order_id" in str(e), str(e)
    assert state["posted"] == 0


def test_w2_probe_picks_our_order_out_of_a_list():
    rows = {"orders": [
        {"order_id": "WB-1", "client_order_id": "slc-nope", "status": "FILLED"},
        {"order_id": "WB-2", "client_order_id": "slc-w2d", "status": "PENDING"}]}
    v, fake = _armed({("GET", "/trading/orders/get"): lambda q, b: rows,
                      ("POST", "/trading/orders/place"): _placed})
    r = v.place_order(Order("slc-w2d", "AAPL", "buy", 1))
    assert r.venue_order_id == "WB-2", \
        "matched on position in the list, not on client_order_id: got %s" \
        % r.venue_order_id
    assert r.status == "accepted", r.status
    assert fake.paths("POST") == [], "our order is already there"


def test_w2_place_response_naming_another_order_is_refused():
    v, _ = _armed({("GET", "/trading/orders/get"): _not_found,
                   ("POST", "/trading/orders/place"): lambda q, b: {
                       "orders": [{"order_id": "WB-X",
                                   "client_order_id": "slc-not-mine",
                                   "status": "FILLED"}]}})
    e = _raises(lambda: v.place_order(Order("slc-w2e", "AAPL", "buy", 1)))
    assert "slc-not-mine" in str(e), str(e)


# ================================================================= W4: cancel
# cancel() returned True unconditionally without reading the response.

def _cancel_venue(handler, **overrides):
    return _armed({(webull._CANCEL_METHOD, "/trading/orders/cancel"): handler},
                  **overrides)


def test_w4_cancel_refused_inside_a_200_body_is_not_reported_as_success():
    """The proved case: HTTP 200, body says the cancel did not happen."""
    v, _ = _cancel_venue(lambda q, b: {"success": False,
                                       "msg": "order already filled"})
    e = _raises(lambda: v.cancel("WB-9001"))
    assert "did not confirm" in str(e), str(e)
    assert "already filled" in str(e), \
        "the venue's own reason has to reach the operator"


def test_w4_cancel_confirmed_returns_true():
    v, fake = _cancel_venue(lambda q, b: {"success": True, "order_id": "WB-9001"})
    assert v.cancel("WB-9001") is True
    assert fake.calls[0]["body"]["order_id"] == "WB-9001"


def test_w4_cancel_accepts_a_cancelled_status_as_confirmation():
    v, _ = _cancel_venue(lambda q, b: {"order_id": "WB-9001",
                                       "status": "CANCELLED"})
    assert v.cancel("WB-9001") is True


def test_w4_a_pending_cancel_is_not_a_completed_cancel():
    """PENDING_CANCEL means the venue took the request, not that the order is
    dead — it can still lose the race to a fill."""
    for st in ("PENDING_CANCEL", "CANCELLING"):
        v, _ = _cancel_venue(lambda q, b, st=st: {"order_id": "WB-9001",
                                                  "status": st})
        e = _raises(lambda: v.cancel("WB-9001"))
        assert "pending" in str(e).lower(), (st, str(e))


def test_w4_disagreeing_flags_fail_closed():
    v, _ = _cancel_venue(lambda q, b: {"success": True, "cancelled": False,
                                       "order_id": "WB-9001"})
    e = _raises(lambda: v.cancel("WB-9001"))
    assert "cancelled=False" in str(e), str(e)


def test_w4_an_unreadable_success_flag_is_not_consent():
    v, _ = _cancel_venue(lambda q, b: {"success": "maybe", "order_id": "WB-9001"})
    e = _raises(lambda: v.cancel("WB-9001"))
    assert "cannot read as consent" in str(e), str(e)


def test_w4_cancel_of_an_order_reported_filled_is_not_a_cancel():
    v, _ = _cancel_venue(lambda q, b: {"order_id": "WB-9001", "status": "FILLED"})
    e = _raises(lambda: v.cancel("WB-9001"))
    assert "filled" in str(e).lower(), str(e)


def test_w4_cancel_with_an_unrecognised_body_is_not_reported_as_success():
    for body in ({}, {"whatever": 1}, {"data": []}):
        v, _ = _cancel_venue(lambda q, b, body=body: body)
        e = _raises(lambda: v.cancel("WB-9001"))
        assert "did not confirm" in str(e), (body, str(e))


def test_w4_cancel_response_about_another_order_is_refused():
    v, _ = _cancel_venue(lambda q, b: {"success": True, "order_id": "WB-OTHER"})
    e = _raises(lambda: v.cancel("WB-9001"))
    assert "WB-OTHER" in str(e), str(e)


def test_w4_cancel_error_envelope_still_raises():
    v, _ = _cancel_venue(lambda q, b: {"code": "417", "msg": "already filled"})
    e = _raises(lambda: v.cancel("WB-9001"))
    assert "417" in str(e), str(e)


def test_w4_cancel_needs_an_order_id():
    v, fake = _cancel_venue(lambda q, b: {"success": True})
    _raises(lambda: v.cancel(""))
    assert fake.calls == [], "an empty order id must not become a blind cancel"


def test_w4_cancel_of_an_unreachable_venue_is_not_a_cancel():
    v, _ = _cancel_venue(lambda q, b: urllib.error.URLError("down"))
    e = _raises(lambda: v.cancel("WB-9001"))
    assert "unreachable" in str(e).lower(), str(e)


# ============================================================= W5: finiteness
# _num used float() with no finiteness check, so NaN and Infinity passed every
# guard built on comparison (nan <= 0 is False).

def test_w5_num_refuses_a_non_finite_value_outright():
    # two positional args only: this asserts the BEHAVIOUR of the old signature,
    # so it cannot be satisfied by a shape change.
    for bad in (NAN, INF, -INF, "nan", "Infinity", "-inf"):
        e = _raises(lambda bad=bad: webull._num({"q": bad}, ("q",)))
        assert "finite" in str(e), (bad, str(e))


def test_w5_num_does_not_launder_a_nan_into_a_missing_field():
    """Falling through to the next key would turn 'corrupt' into 'absent', and
    every caller that writes `_num(...) or 0.0` would then read a confident
    zero."""
    e = _raises(lambda: webull._num({"qty": NAN, "quantity": 5},
                                    ("qty", "quantity")))
    assert "finite" in str(e), str(e)


def test_w5_num_names_the_field_and_the_value_it_refused():
    """The refusal is what an operator gets in the log; it has to be diagnostic."""
    e = _raises(lambda: webull._num({"quantity": NAN}, ("quantity",),
                                    "position quantity"))
    assert "quantity" in str(e) and "position quantity" in str(e), str(e)


def test_w5_num_still_returns_ordinary_numbers():
    assert webull._num({"a": "1.5"}, ("a", "b")) == 1.5
    assert webull._num({"a": "", "b": 2}, ("a", "b")) == 2.0
    assert webull._num({"a": "abc"}, ("a",)) is None
    assert webull._num({}, ("a",)) is None


def _meta_venue(row):
    fake = FakeHTTP({("GET", "/i"): lambda q, b: {"instruments": [row]}})
    return _venue(fake, instrument_path="/i",
                  instrument_fields={"tick_size": "tick", "lot_step": "lot",
                                     "min_qty": "min_q", "min_notional": "min_n"})


def test_w5_nan_tick_size_is_refused():
    v = _meta_venue({"symbol": "AAPL", "tick": NAN, "lot": "1", "min_q": "1"})
    e = _raises(lambda: v.symbol_meta("AAPL"))
    assert "finite" in str(e), str(e)


def test_w5_infinite_lot_step_is_refused():
    v = _meta_venue({"symbol": "AAPL", "tick": "0.01", "lot": INF, "min_q": "1"})
    assert "finite" in str(_raises(lambda: v.symbol_meta("AAPL")))


def test_w5_nan_min_qty_is_refused():
    v = _meta_venue({"symbol": "AAPL", "tick": "0.01", "lot": "1", "min_q": NAN})
    assert "finite" in str(_raises(lambda: v.symbol_meta("AAPL")))


def test_w5_non_finite_min_notional_is_refused_too():
    v = _meta_venue({"symbol": "AAPL", "tick": "0.01", "lot": "1", "min_q": "1",
                     "min_n": INF})
    assert "finite" in str(_raises(lambda: v.symbol_meta("AAPL")))


def test_w5_a_nan_increment_never_reaches_a_SymbolMeta():
    """The comparison guard alone let both through: nan <= 0 and inf <= 0 are
    both False."""
    assert not (NAN <= 0) and not (INF <= 0), "premise of this test"
    for bad in (NAN, INF):
        v = _meta_venue({"symbol": "AAPL", "tick": bad, "lot": "1", "min_q": "1"})
        try:
            m = v.symbol_meta("AAPL")
        except VenueError:
            continue
        raise AssertionError("accepted tick_size=%r" % (m.tick_size,))


def test_w5_nan_position_quantity_is_refused():
    fake = FakeHTTP({("GET", "/trading/assets/positions/list"): lambda q, b: {
        "positions": [{"symbol": "AAPL", "quantity": NAN}]}})
    e = _raises(lambda: _venue(fake).positions())
    assert "finite" in str(e), str(e)


def test_w5_infinite_position_quantity_is_refused():
    fake = FakeHTTP({("GET", "/trading/assets/positions/list"): lambda q, b: {
        "positions": [{"symbol": "AAPL", "quantity": INF}]}})
    assert "finite" in str(_raises(lambda: _venue(fake).positions()))


def test_w5_nan_position_pnl_is_refused_not_read_as_zero():
    fake = FakeHTTP({("GET", "/trading/assets/positions/list"): lambda q, b: {
        "positions": [{"symbol": "AAPL", "quantity": "10", "cost_price": "1",
                       "unrealized_profit_loss": NAN}]}})
    assert "finite" in str(_raises(lambda: _venue(fake).positions()))


def test_w5_non_finite_balance_is_refused():
    fake = FakeHTTP({("GET", "/trading/assets/balances/get"): lambda q, b: {
        "account_currency_assets": [{"currency": "USD", "total_amount": INF}]}})
    assert "finite" in str(_raises(lambda: _venue(fake).balances()))


def test_w5_nan_order_quantity_never_reaches_the_venue():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    e = _raises(lambda: v.place_order(Order("slc-w5a", "AAPL", "buy", NAN)))
    assert "finite" in str(e), str(e)
    assert fake.calls == [], "a corrupt order must not even open a connection"


def test_w5_infinite_order_quantity_never_reaches_the_venue():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    assert "finite" in str(
        _raises(lambda: v.place_order(Order("slc-w5b", "AAPL", "buy", INF))))
    assert fake.calls == []


def test_w5_non_finite_limit_price_never_reaches_the_venue():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    e = _raises(lambda: v.place_order(
        Order("slc-w5c", "AAPL", "buy", 1, order_type="limit", limit_price=NAN)))
    assert "finite" in str(e), str(e)
    assert fake.calls == []


def test_w5_zero_or_negative_quantity_is_refused():
    v, fake = _armed({("GET", "/trading/orders/get"): _not_found,
                      ("POST", "/trading/orders/place"): _placed})
    for bad in (0, -1):
        _raises(lambda bad=bad: v.place_order(Order("slc-w5d", "AAPL", "buy", bad)))
    assert fake.calls == []


def test_w5_decimal_str_refuses_to_render_a_non_finite_number():
    for bad in (NAN, INF, -INF):
        e = _raises(lambda bad=bad: webull._decimal_str(bad))
        assert "finite" in str(e), (bad, str(e))
    assert webull._decimal_str(0.00000123) == "0.00000123"


def test_w5_nan_fill_quantity_is_not_reported_as_zero():
    """A filled order whose size we cannot read must not come back as
    filled_qty 0.0 — that is the UNKNOWN-becomes-ZERO bug in a new place."""
    seen = {"order_id": "WB-8", "client_order_id": "slc-w5e", "status": "FILLED",
            "filled_quantity": NAN}
    v, _ = _armed({("GET", "/trading/orders/get"): lambda q, b: seen,
                   ("POST", "/trading/orders/place"): _placed})
    try:
        r = v.place_order(Order("slc-w5e", "AAPL", "buy", 3))
    except VenueError as e:
        assert "finite" in str(e), str(e)
        return
    raise AssertionError("reported filled_qty=%r for an unreadable fill"
                         % (r.filled_qty,))


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


def test_no_refusal_message_leaks_the_secret():
    """Refusal text ends up in logs and notifications. It must carry the reason,
    never the credential."""
    v, _ = _armed({("GET", "/trading/orders/get"): _unreachable})
    e = _raises(lambda: v.place_order(Order("slc-leak", "AAPL", "buy", 1)))
    assert v._secret not in str(e), str(e)
    assert v._key not in str(e), str(e)


class _EchoingError(urllib.error.HTTPError):
    """A venue that quotes the request back inside its error body. Real APIs do
    this, and the body lands in a VenueError message that gets persisted."""

    def __init__(self, body):
        urllib.error.HTTPError.__init__(self, "u", 401, "denied", {}, None)
        self._body = body.encode()

    def read(self):
        return self._body


def test_an_error_body_echoing_the_credentials_is_redacted():
    key, secret = CREDS["api_key"], CREDS["api_secret"]
    body = json.dumps({"msg": "bad signature for app_key=%s secret=%s"
                              % (key, secret)})
    fake = FakeHTTP({("GET", "/trading/accounts/list"):
                     lambda q, b: _EchoingError(body)})
    e = _raises(lambda: _venue(fake).accounts())
    assert "401" in str(e), str(e)
    assert secret not in str(e), str(e)
    assert key not in str(e), str(e)
    assert "<redacted>" in str(e), str(e)


def test_a_non_json_error_page_echoing_a_credential_is_redacted():
    """A proxy returning an HTML error page is the other way a request gets
    quoted back at you."""
    v = _venue(lambda *a, **kw: ("<html>key=%s</html>" % CREDS["api_key"], {}),
               read_only=False)
    e = _raises(lambda: v.place_order(Order("slc-echo2", "AAPL", "buy", 1)))
    assert CREDS["api_key"] not in str(e), str(e)
    assert "non-JSON" in str(e), str(e)


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
