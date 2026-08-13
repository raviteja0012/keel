"""CCXT adapter: the idempotency probe, and what it is allowed to conclude.

This adapter covers 103 exchanges and until now had no tests at all. It shipped
the same defect Webull was disarmed for, undetected, because nobody ever asked
it a question.

The defect: _find_by_client_id wrapped its probe in `except Exception: continue`
and returned None. place_order then read that None as "the venue says no such
order" and raised retryable=True — telling the caller a resubmission was safe
at the exact moment it was least safe. If the first request had landed, the
retry opened a second position.

The rule these tests pin is one sentence: a probe that could not ask is not a
probe that was told no. Only a venue-confirmed absence may set retryable.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "cx.db"))

import ccxt                                                # noqa: E402

from brokers import (PROBE_ABSENT, PROBE_FOUND,            # noqa: E402
                     PROBE_INDETERMINATE, Order, VenueError,
                     VenueIndeterminate, VenueReadOnly)
from brokers.ccxt_venue import CcxtVenue                   # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


# --------------------------------------------------------------------------
# A fake exchange whose two order books can each be made to answer, to be
# empty, or to fail. Every interesting case is a combination of those three.

class FakeExchange:
    def __init__(self, create=None, open_orders=None, closed_orders=None):
        self._create = create
        self._open = open_orders if open_orders is not None else []
        self._closed = closed_orders if closed_orders is not None else []
        self.create_calls = 0

    def create_order(self, *a, **k):
        self.create_calls += 1
        if isinstance(self._create, BaseException):
            raise self._create
        return self._create or {"id": "v-1", "clientOrderId": "keel-1",
                                "status": "open", "filled": 0}

    def _books(self, which):
        if isinstance(which, BaseException):
            raise which
        return which

    def fetch_open_orders(self, *a, **k):
        return self._books(self._open)

    def fetch_closed_orders(self, *a, **k):
        return self._books(self._closed)


def venue(exchange_id="binanceus", read_only=False, **kw):
    v = CcxtVenue.__new__(CcxtVenue)
    v.name = "test"
    v.exchange_id = exchange_id
    v.read_only = read_only
    v._default_type = "spot"
    v._markets = {"BTC/USD": {}}
    v._x = FakeExchange(**kw)
    v._resolve = lambda s: "BTC/USD"
    return v


ORDER = Order(client_order_id="keel-1", symbol="BTC/USD", side="buy", qty=0.5)
LANDED = {"id": "v-1", "clientOrderId": "keel-1", "status": "open", "filled": 0}
NET = ccxt.NetworkError("connection reset")


# ------------------------------------------------------------------ probe

def test_probe_distinguishes_absent_from_unaskable():
    v = venue(open_orders=[], closed_orders=[])
    check("both books answer and are empty -> ABSENT",
          v._find_by_client_id("keel-1", "BTC/USD").outcome == PROBE_ABSENT)

    v = venue(open_orders=NET, closed_orders=NET)
    p = v._find_by_client_id("keel-1", "BTC/USD")
    check("neither book can be read -> INDETERMINATE",
          p.outcome == PROBE_INDETERMINATE, p.outcome)
    check("and it says why", "connection reset" in p.reason, p.reason)


def test_one_readable_book_is_still_not_an_absence():
    # The order is not in the book we CAN read. It could be sitting in the one
    # we cannot. Half an answer is not an answer.
    v = venue(open_orders=[], closed_orders=NET)
    p = v._find_by_client_id("keel-1", "BTC/USD")
    check("one book readable and empty, other unreadable -> INDETERMINATE",
          p.outcome == PROBE_INDETERMINATE, p.outcome)


def test_a_found_order_beats_a_broken_second_book():
    v = venue(open_orders=[LANDED], closed_orders=NET)
    p = v._find_by_client_id("keel-1", "BTC/USD")
    check("found in the first book -> FOUND, second book never consulted",
          p.outcome == PROBE_FOUND and p.order is LANDED, p.outcome)


def test_is_absent_is_not_falsiness():
    # The trap the original code fell into, pinned as a property: `not p.order`
    # is true for indeterminate, so anything testing the order rather than the
    # outcome reads "could not ask" as "no".
    v = venue(open_orders=NET, closed_orders=NET)
    p = v._find_by_client_id("keel-1", "BTC/USD")
    check("indeterminate has no order...", p.order is None)
    check("...but is_absent is still False", p.is_absent is False)


# ------------------------------------------------- place_order, pre-submit

def test_untrusted_venue_refuses_when_it_cannot_verify():
    v = venue(exchange_id="binanceus", open_orders=NET, closed_orders=NET)
    try:
        v.place_order(ORDER)
        check("untrusted venue with unreadable books must not submit", False,
              "it submitted")
    except VenueIndeterminate as e:
        check("untrusted venue with unreadable books refuses to submit", True)
        check("and the refusal is not retryable", e.retryable is False)
        check("and it names the order", e.client_order_id == "keel-1")
    check("nothing was sent to the exchange", v._x.create_calls == 0,
          v._x.create_calls)


def test_untrusted_venue_returns_the_existing_order():
    v = venue(exchange_id="binanceus", open_orders=[LANDED])
    r = v.place_order(ORDER)
    check("a resubmitted id returns the existing order", r.venue_order_id == "v-1")
    check("and does not create a second one", v._x.create_calls == 0)


# ------------------------------------------------ place_order, post-error
#
# These use a TRUSTED exchange deliberately. Untrusted venues are stopped by
# the pre-submit probe above and never reach create_order at all. The ten
# exchanges in _TRUSTED_CLIENT_ID skip that probe — we rely on the venue to
# enforce the key itself — so the post-transport-error path is the ONLY
# idempotency protection they have. It is also where the defect lived.

def test_transport_error_with_confirmed_absence_is_retryable():
    # The venue answered, and answered no. Nothing landed. This is the only
    # branch permitted to say retryable=True.
    v = venue(exchange_id="binance", create=NET, open_orders=[], closed_orders=[])
    try:
        v.place_order(ORDER)
        check("transport error must raise", False)
    except VenueIndeterminate:
        check("confirmed absence must NOT be indeterminate", False)
    except VenueError as e:
        check("transport error + confirmed absence -> retryable", e.retryable is True)


def test_transport_error_with_unreadable_probe_is_never_retryable():
    # THE REGRESSION. The order may be live on the exchange right now. The old
    # code said "not found on venue" with retryable=True here.
    #
    # Note this refuses even though the exchange is in _TRUSTED_CLIENT_ID.
    # Membership of a hardcoded list is not evidence that this particular
    # market type mapped clientOrderId onto the wire. Trust is an assumption;
    # a duplicate fill is a fact.
    v = venue(exchange_id="binance", create=NET, open_orders=NET, closed_orders=NET)
    try:
        v.place_order(ORDER)
        check("transport error must raise", False)
    except VenueIndeterminate as e:
        check("transport error + unreadable probe -> VenueIndeterminate", True)
        check("and it is NOT retryable", e.retryable is False)
        check("and it tells the operator to reconcile",
              "reconcile" in str(e).lower(), str(e)[:80])
    except VenueError as e:
        check("transport error + unreadable probe must not be a plain retry",
              False, "retryable=%s" % e.retryable)


def test_transport_error_after_landing_recovers_the_order():
    v = venue(exchange_id="binance", create=NET, open_orders=[LANDED])
    r = v.place_order(ORDER)
    check("an order that landed despite the transport error is recovered",
          r.venue_order_id == "v-1")


# ------------------------------------------------------------- read_only

def test_read_only_is_checked_before_anything_else():
    v = venue(read_only=True, open_orders=NET, closed_orders=NET)
    try:
        v.place_order(ORDER)
        check("a read-only venue must refuse", False)
    except VenueReadOnly:
        check("a read-only venue refuses before probing or submitting", True)
    check("and touches the exchange not at all", v._x.create_calls == 0)


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
