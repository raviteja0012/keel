"""Robinhood Crypto adapter.

The centrepiece is test_signature_covers_the_bytes_actually_sent. Robinhood's
published example signature is not reproducible from the body printed next to
it (verified: it is the signature of a Python dict's str(), not of the JSON),
so it cannot be used to validate an implementation. The invariant that does
matter, and that their own client only satisfies by coincidence, is that the
bytes signed are the bytes transmitted. That is checked here by intercepting
the request and verifying the signature against what actually went out.

The rest pins the same discipline as every other adapter: an unreadable probe
is not a confirmed absence, and a missing number is not zero.
"""
import base64
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "rh.db"))

from nacl.signing import SigningKey, VerifyKey                      # noqa: E402

from brokers import (Order, VenueError, VenueIndeterminate,          # noqa: E402
                     VenueReadOnly)
from brokers.robinhood import (RobinhoodVenue, _fmt, _is_uuid,       # noqa: E402
                               _num, _venue_symbol, new_client_order_id)

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


_SEED = SigningKey.generate()
PRIV = base64.b64encode(bytes(_SEED)).decode()
PUB = VerifyKey(bytes(_SEED.verify_key))
KEY = "rh-api-6148effc-c0b1-486c-8940-a1d099456be6"
CID = "131de903-5a9c-4260-abc1-28d562a5dcf0"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = b"{}" if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records every request and replays scripted responses."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.sent.append({"method": method, "url": url, "headers": headers,
                          "body": data})
        if not self.script:
            raise AssertionError("unexpected extra request to %s" % url)
        nxt = self.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def venue(script=(), read_only=False, account="ACC1", version="v2"):
    v = RobinhoodVenue({"api_key": KEY, "private_key": PRIV, "name": "rh",
                        "read_only": read_only, "account_number": account,
                        "api_version": version})
    v._session = FakeSession(script)
    return v


def orders_page(rows, nxt=None):
    return FakeResponse({"results": rows, "next": nxt})


ORDER = Order(client_order_id=CID, symbol="BTC-USD", side="buy", qty=0.1)
PLACED = {"id": "v-1", "client_order_id": CID, "state": "open",
          "filled_asset_quantity": "0", "average_price": None}


# ------------------------------------------------------------------ signing

def test_signature_covers_the_bytes_actually_sent():
    v = venue(script=[orders_page([]), FakeResponse(PLACED)])
    v.place_order(ORDER)

    post = [s for s in v._session.sent if s["method"] == "POST"][0]
    h = post["headers"]
    body_bytes = post["body"] or b""
    path = post["url"].replace("https://trading.robinhood.com", "")

    message = "%s%s%s%s%s" % (h["x-api-key"], h["x-timestamp"], path, "POST",
                              body_bytes.decode("utf-8"))
    try:
        PUB.verify(message.encode("utf-8"),
                   base64.b64decode(h["x-signature"]))
        ok = True
    except Exception:                                     # noqa: BLE001
        ok = False
    check("the signature verifies against the transmitted body and URL", ok)


def test_query_params_are_signed_and_sent_identically():
    v = venue(script=[orders_page([]), FakeResponse(PLACED)])
    v.place_order(ORDER)
    post = [s for s in v._session.sent if s["method"] == "POST"][0]
    check("account_number rides in the query string",
          "account_number=ACC1" in post["url"], post["url"])
    # If path and URL had been built separately they could differ; the
    # signature check above is what proves they did not.


def test_get_requests_sign_an_empty_body():
    v = venue(script=[orders_page([])])
    v._paginate("trading/orders/", {"account_number": "ACC1"}, 1)
    get = v._session.sent[0]
    check("a GET carries no body", get["body"] is None)
    path = get["url"].replace("https://trading.robinhood.com", "")
    h = get["headers"]
    msg = "%s%s%s%s%s" % (h["x-api-key"], h["x-timestamp"], path, "GET", "")
    try:
        PUB.verify(msg.encode(), base64.b64decode(h["x-signature"]))
        ok = True
    except Exception:                                     # noqa: BLE001
        ok = False
    check("and its signature omits the body too", ok)


# -------------------------------------------------------------------- probe

def test_probe_absent_only_when_pagination_is_exhausted():
    v = venue(script=[orders_page([], nxt=None)])
    p = v._probe_client_id(CID, "BTC-USD")
    check("end of list reached and no match -> ABSENT", p.is_absent, p.outcome)


def test_unexhausted_pagination_is_indeterminate_not_absent():
    # The 3Commas defect: read one page, conclude absence, submit a duplicate.
    pages = [orders_page([{"client_order_id": "other"}], nxt="/api/v2/x?cursor=%d" % i)
             for i in range(12)]
    v = venue(script=pages)
    p = v._probe_client_id(CID, "BTC-USD")
    check("ran out of page budget -> INDETERMINATE", p.outcome == "indeterminate",
          p.outcome)
    check("and it is explicitly not an absence", p.is_absent is False)


def test_unreadable_order_list_is_indeterminate():
    v = venue(script=[FakeResponse({"errors": [{"detail": "boom"}]}, status=503)])
    p = v._probe_client_id(CID, "BTC-USD")
    check("unreadable order list -> INDETERMINATE", p.outcome == "indeterminate",
          p.outcome)


def test_probe_finds_a_matching_client_order_id():
    v = venue(script=[orders_page([{"client_order_id": "x"}, PLACED])])
    p = v._probe_client_id(CID, "BTC-USD")
    check("a matching client_order_id -> FOUND", p.outcome == "found", p.outcome)


# -------------------------------------------------------------- place_order

def test_refuses_to_submit_when_the_probe_cannot_answer():
    v = venue(script=[FakeResponse(None, status=500)])
    try:
        v.place_order(ORDER)
        check("must not submit on an unreadable probe", False, "it submitted")
    except VenueIndeterminate as e:
        check("refuses to submit when it cannot verify", True)
        check("and the refusal is not retryable", e.retryable is False)
    check("nothing was POSTed",
          not [s for s in v._session.sent if s["method"] == "POST"])


def test_a_duplicate_submission_returns_the_existing_order():
    v = venue(script=[orders_page([PLACED])])
    r = v.place_order(ORDER)
    check("a resubmitted id returns the existing order", r.venue_order_id == "v-1")
    check("and never reaches POST",
          not [s for s in v._session.sent if s["method"] == "POST"])


def test_post_failure_with_unreadable_probe_is_indeterminate():
    import requests as _rq
    v = venue(script=[orders_page([]),
                      _rq.RequestException("connection reset"),
                      FakeResponse(None, status=503)])
    try:
        v.place_order(ORDER)
        check("a failed POST must raise", False)
    except VenueIndeterminate as e:
        check("failed POST + unreadable probe -> VenueIndeterminate", True)
        check("and it is not retryable", e.retryable is False)
        check("and it says to reconcile", "reconcile" in str(e).lower())
    except VenueError as e:
        check("must not be a plain retryable error", False,
              "retryable=%s" % e.retryable)


def test_post_failure_with_confirmed_absence_is_retryable():
    import requests as _rq
    v = venue(script=[orders_page([]),
                      _rq.RequestException("connection reset"),
                      orders_page([])])
    try:
        v.place_order(ORDER)
        check("a failed POST must raise", False)
    except VenueIndeterminate:
        check("a confirmed absence must not be indeterminate", False)
    except VenueError as e:
        check("failed POST + confirmed absence -> retryable", e.retryable is True)


def test_post_failure_recovers_an_order_that_landed():
    import requests as _rq
    v = venue(script=[orders_page([]),
                      _rq.RequestException("connection reset"),
                      orders_page([PLACED])])
    r = v.place_order(ORDER)
    check("an order that landed despite the error is recovered",
          r.venue_order_id == "v-1")


def test_read_only_refuses_before_touching_the_network():
    v = venue(script=[], read_only=True)
    try:
        v.place_order(ORDER)
        check("read-only must refuse", False)
    except VenueReadOnly:
        check("read-only refuses before any request", True)
    check("and sends nothing", not v._session.sent)


def test_non_uuid_client_order_id_is_rejected_locally():
    v = venue(script=[])
    try:
        v.place_order(Order(client_order_id="slc-123", symbol="BTC-USD",
                            side="buy", qty=0.1))
        check("a non-UUID client_order_id must be rejected", False)
    except VenueError as e:
        check("a non-UUID client_order_id is rejected before sending",
              "UUID" in str(e))
    check("and nothing was sent", not v._session.sent)


# ------------------------------------------------------- refusing to pretend

def test_stop_loss_is_refused_rather_than_silently_dropped():
    # Robinhood models stops as separate order types. Accepting the field and
    # dropping it would report a protected position that has no protection.
    v = venue(script=[orders_page([])])
    try:
        v.place_order(Order(client_order_id=CID, symbol="BTC-USD", side="buy",
                            qty=0.1, stop_loss=50000.0))
        check("an attached stop must not be silently dropped", False)
    except VenueError as e:
        check("an attached stop is refused, loudly",
              "separate order types" in str(e))


def test_reduce_only_is_refused():
    v = venue(script=[orders_page([])])
    try:
        v.place_order(Order(client_order_id=CID, symbol="BTC-USD", side="sell",
                            qty=0.1, reduce_only=True))
        check("reduce_only must not be silently dropped", False)
    except VenueError as e:
        check("reduce_only is refused", "reduce_only" in str(e))


def test_symbol_meta_refuses_to_guess_missing_increments():
    v = venue(script=[FakeResponse({"results": [
        {"symbol": "BTC-USD", "is_api_tradable": True,
         "quote_increment": "0.01", "asset_increment": None,
         "min_order_size": "0.000001"}]})])
    try:
        v.symbol_meta("BTC-USD")
        check("a missing increment must not be defaulted", False)
    except VenueError as e:
        check("a missing sizing field raises rather than guessing",
              "refusing to guess" in str(e))


def test_symbol_meta_refuses_untradable_pairs():
    v = venue(script=[FakeResponse({"results": [
        {"symbol": "XYZ-USD", "is_api_tradable": False,
         "quote_increment": "0.01", "asset_increment": "0.01",
         "min_order_size": "1"}]})])
    try:
        v.symbol_meta("XYZ-USD")
        check("an untradable pair must be refused", False)
    except VenueError as e:
        check("a pair that is not api-tradable is refused",
              "not api-tradable" in str(e))


# ------------------------------------------------------------------ helpers

def test_symbol_spelling():
    check("BTC/USD -> BTC-USD", _venue_symbol("BTC/USD") == "BTC-USD")
    check("btcusd -> BTC-USD", _venue_symbol("btcusd") == "BTC-USD")
    check("BTC-USD unchanged", _venue_symbol("BTC-USD") == "BTC-USD")


def test_num_keeps_unknown_unknown():
    check("None stays None", _num(None) is None)
    check("empty string stays None", _num("") is None)
    check("garbage stays None", _num("abc") is None)
    check("NaN is not a number", _num(float("nan")) is None)
    check("inf is not a number", _num(float("inf")) is None)
    check("zero is a real zero", _num("0") == 0.0)


def test_fmt_emits_neither_sci_notation_nor_float_noise():
    # Both are real wire hazards: 1e-05 is not a number Robinhood parses, and
    # 0.100000000000000006 is a quantity nobody asked for.
    check("small quantities stay positional", "e" not in _fmt(0.00001).lower(),
          _fmt(0.00001))
    check("0.00001 is exact", _fmt(0.00001) == "0.00001", _fmt(0.00001))
    check("0.1 is 0.1", _fmt(0.1) == "0.1", _fmt(0.1))
    check("1.0 loses its trailing zero", _fmt(1.0) == "1", _fmt(1.0))
    check("0.0001234 survives intact", _fmt(0.0001234) == "0.0001234",
          _fmt(0.0001234))
    check("a whole number stays whole", _fmt(250) == "250", _fmt(250))


def test_generated_ids_are_uuids():
    check("new_client_order_id is a UUID", _is_uuid(new_client_order_id()))


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
