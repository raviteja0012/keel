"""Adapter conformance suite — the properties every venue must hold, checked
against a hostile fake venue rather than against a reviewer's attention.

WHY THIS EXISTS
---------------
brokers/__init__.py makes two properties mandatory: place_order is idempotent
on client_order_id, and a venue is read_only until execution is armed. Until
now the only thing enforcing them was `isinstance(v, BrokerAdapter)`, which
checks method NAMES and nothing else, plus per-adapter tests written by the
same person who wrote the adapter. That combination shipped 106 passing tests
over an adapter that still double-fills.

The gap is not test COUNT, it is test SHAPE. A hand-written test asserts an
example the author thought of. Every idempotency defect found in this codebase
lived in an example the author did not think of:

  webull   an envelope {"code":"404"} (meaning "account not found") stamped
           onto http_status and read as "no such order"; the suite's own
           envelope test used code "60001", which does not collide
  ccxt     `except Exception: continue` in the probe returns None, and None
           reads as absence
  3commas  the probe reads one 100-row page and substring-matches a `note`

Each is the same bug. None was caught, because each test named a scenario
rather than a PROPERTY.

WHAT THIS SUITE DOES DIFFERENTLY
--------------------------------
It states the properties once, and drives every adapter through a fake venue
that behaves the way real venues behave on their worst day: dropping responses
after the order landed, answering "I don't know", paginating the order list,
returning somebody else's order, and emitting NaN.

The suite never inspects adapter internals. It counts what reached the venue.
An adapter passes only if, for every fault, the number of live orders created
by N submissions of one client_order_id is at most one.

THE SEAM
--------
An adapter cannot be conformance-tested unless the suite can inject faults
under it. Each adapter therefore exposes ONE classmethod:

    @classmethod
    def _conformance(cls, venue: FakeVenue, **cfg) -> "BrokerAdapter"

returning an instance whose transport is `venue` and which is otherwise
configured normally. That is the only production change the suite requires,
and an adapter that does not provide it does not get registered (see
require_conformance() at the bottom).

Run:  cd trading-bot && python tests/conformance.py
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brokers import Order, VenueError, VenueReadOnly   # noqa: E402


# --------------------------------------------------------------- fault model
# The vocabulary of things a real venue does that break naive adapters. These
# are not hypotheticals: each one is a defect already found in this tree.
DROP_AFTER_LANDING = "drop_after_landing"   # order created, response lost
PROBE_UNREACHABLE = "probe_unreachable"     # the lookup cannot answer
PROBE_AUTH_FAIL = "probe_auth_fail"         # 401: says nothing about the order
PROBE_ENVELOPE_ERR = "probe_envelope_err"   # HTTP 200, error code in the body
PROBE_FOREIGN_ORDER = "probe_foreign"       # answers about a different order
PROBE_PAGINATED = "probe_paginated"         # our order is past page 1
NON_FINITE = "non_finite"                   # NaN/Infinity in a numeric field
CANCEL_UNCONFIRMED = "cancel_unconfirmed"   # 200 body that refuses the cancel
CANCEL_PENDING = "cancel_pending"           # accepted, still racing a fill

ALL_FAULTS = (DROP_AFTER_LANDING, PROBE_UNREACHABLE, PROBE_AUTH_FAIL,
              PROBE_ENVELOPE_ERR, PROBE_FOREIGN_ORDER, PROBE_PAGINATED,
              NON_FINITE, CANCEL_UNCONFIRMED, CANCEL_PENDING)


class FakeVenue:
    """A venue that actually keeps an order book, so the suite can count real
    consequences instead of counting API calls.

    The critical property: `live_orders` is incremented when an order is
    CREATED, regardless of whether the adapter ever learns about it. That is
    what makes a dropped response indistinguishable from success to the
    adapter, and exactly what a double fill looks like from the venue's side.
    """

    def __init__(self, faults=(), page_size=100, noise_orders=0):
        self.faults = set(faults)
        self.page_size = page_size
        self.orders = {}            # client_order_id -> order dict
        self.live_orders = 0        # every CREATE, even ones we hid
        self.cancels = []
        self._seq = 0
        for i in range(noise_orders):
            self.orders["noise-%d" % i] = {
                "client_order_id": "noise-%d" % i, "order_id": "N%d" % i,
                "status": "FILLED", "_noise": True}

    def create(self, client_order_id, **fields):
        """The venue creates an order. This is the thing we are counting."""
        self._seq += 1
        self.live_orders += 1
        row = {"client_order_id": client_order_id,
               "order_id": "V-%d" % self._seq, "status": "SUBMITTED"}
        row.update(fields)
        if NON_FINITE in self.faults:
            row["filled_quantity"] = float("nan")
            row["avg_filled_price"] = float("inf")
        self.orders[client_order_id] = row
        if DROP_AFTER_LANDING in self.faults:
            raise Dropped("connection reset after the order was accepted")
        return row

    def lookup(self, client_order_id):
        """The idempotency probe's view. Every fault here is a way of NOT
        answering the question, and none of them means 'no such order'."""
        if PROBE_UNREACHABLE in self.faults:
            raise Unreachable("connection refused")
        if PROBE_AUTH_FAIL in self.faults:
            raise HttpStatus(401, "signature expired")
        if PROBE_ENVELOPE_ERR in self.faults:
            # HTTP 200. The body carries a business code that happens to be a
            # number a careless adapter maps onto "not found".
            return {"code": "404", "msg": "account not found"}
        if PROBE_FOREIGN_ORDER in self.faults:
            return {"client_order_id": "somebody-else", "order_id": "V-999",
                    "status": "FILLED", "filled_quantity": "9"}
        return self.orders.get(client_order_id)

    def list_orders(self):
        """A LIMIT-capped list read, like ccxt fetch_open_orders(limit=N) or
        3commas per_page: one page, and under PROBE_PAGINATED it returns only
        the first page_size rows and no more. An adapter that reads this and
        concludes absence will place a duplicate."""
        rows = list(self.orders.values())
        if PROBE_PAGINATED in self.faults:
            return rows[:self.page_size]
        return rows

    def list_orders_full(self):
        """A CURSOR-paginated view: the caller can walk the WHOLE set page by
        page. This is the honest model for an adapter (Robinhood) that follows
        a `next` cursor to exhaustion — the fault such an adapter must survive
        is failing to follow the cursor to the end, not a hard page cap. The
        driver chunks this by page_size and sets `next` while more remain; an
        adapter that stops early and calls absence still double-fills."""
        return list(self.orders.values())

    def cancel(self, order_id):
        self.cancels.append(order_id)
        if CANCEL_UNCONFIRMED in self.faults:
            return {"order_id": order_id, "success": False,
                    "msg": "order already filled"}
        if CANCEL_PENDING in self.faults:
            # Accepted but still racing a fill. NOT a completed cancel.
            return {"order_id": order_id, "success": True,
                    "status": "PENDING_CANCEL"}
        row = self.orders.get(order_id) or {}
        row["status"] = "CANCELLED"
        return {"order_id": order_id, "success": True, "status": "CANCELLED"}


class Dropped(Exception):
    """Transport died after the venue acted."""


class Unreachable(Exception):
    """Transport never got an answer."""


class HttpStatus(Exception):
    def __init__(self, code, msg=""):
        super().__init__("HTTP %d: %s" % (code, msg))
        self.code = code


# ------------------------------------------------------------------ the laws
class Violation(AssertionError):
    pass


def law_idempotent_under_fault(make_adapter, fault):
    """THE law. N submissions of one client_order_id create at most one order.

    This is stated over the VENUE's order count, not over the adapter's return
    value, because the adapter's opinion is the thing under test.
    """
    venue = FakeVenue([fault], noise_orders=150 if fault == PROBE_PAGINATED else 0)
    adapter = make_adapter(venue)
    order = Order("slc-conformance-1", "BTC/USDT", "buy", 1.0)

    outcomes = []
    for _ in range(3):                    # the engine retries; so do we
        try:
            outcomes.append(("ok", adapter.place_order(order)))
        except VenueError as e:
            outcomes.append(("refused", e))
        except Exception as e:            # noqa: BLE001 - any leak is a defect
            raise Violation(
                "place_order raised %r under %s. The contract's error type is "
                "VenueError; an unclassified exception is an adapter bug that "
                "the engine cannot reason about." % (e, fault))

    if venue.live_orders > 1:
        raise Violation(
            "%d live orders were created at the venue from 3 submissions of "
            "ONE client_order_id under fault %r. This is a double fill; no "
            "downstream rail can undo it." % (venue.live_orders, fault))

    # Refusing is always acceptable. Claiming success while the venue has no
    # order is not: the engine would then believe it holds a position it does
    # not hold, and reconciliation would call it LOCAL_ONLY forever.
    for kind, res in outcomes:
        if kind == "ok" and venue.live_orders == 0:
            raise Violation(
                "place_order reported success (%s) under %r but the venue "
                "created no order — the engine would book a phantom position."
                % (getattr(res, "status", "?"), fault))
    return venue, outcomes


def law_never_claims_absence_it_cannot_prove(make_adapter):
    """An indeterminate probe must not be reported as 'not found at venue'.

    The engine reads that phrase (and retryable=True) as licence to try again.
    Saying it after a probe that failed is how one intent becomes two fills.
    """
    for fault in (PROBE_UNREACHABLE, PROBE_AUTH_FAIL, PROBE_ENVELOPE_ERR):
        venue = FakeVenue([DROP_AFTER_LANDING, fault])
        adapter = make_adapter(venue)
        try:
            adapter.place_order(Order("slc-conformance-2", "BTC/USDT", "buy", 1.0))
        except VenueError as e:
            text = str(e).lower()
            if "not found" in text and "may be live" not in text:
                raise Violation(
                    "under %r the adapter said %r. The probe could not answer; "
                    "'not found' is a claim it has no evidence for."
                    % (fault, str(e)[:90]))
            if e.retryable and venue.live_orders > 0:
                raise Violation(
                    "under %r the adapter marked retryable=True while an order "
                    "IS live at the venue. A retry loop on this is a double "
                    "fill." % fault)
        except Exception as e:            # noqa: BLE001
            raise Violation("non-VenueError %r under %r" % (e, fault))


def law_read_only_blocks_before_the_wire(make_adapter):
    """A read-only venue must refuse without touching the network at all."""
    venue = FakeVenue()
    adapter = make_adapter(venue, read_only=True)
    try:
        adapter.place_order(Order("slc-conformance-3", "BTC/USDT", "buy", 1.0))
    except VenueReadOnly:
        pass
    except VenueError as e:
        raise Violation("read-only venue raised %s, not VenueReadOnly: %s"
                        % (type(e).__name__, str(e)[:80]))
    else:
        raise Violation("a read-only venue accepted an order")
    if venue.live_orders or venue._seq:
        raise Violation("a read-only venue reached the wire")


def law_no_non_finite_escapes(make_adapter):
    """NaN and Infinity satisfy no comparison, so they walk through every
    guard written as `x <= 0` and land in sizing. Neither may leave an
    adapter, in either direction."""
    venue = FakeVenue([NON_FINITE])
    adapter = make_adapter(venue)
    try:
        r = adapter.place_order(Order("slc-conformance-4", "BTC/USDT", "buy", 1.0))
    except VenueError:
        return                            # refusing is the correct answer
    for label, val in (("filled_qty", r.filled_qty), ("avg_price", r.avg_price)):
        if val is not None and not math.isfinite(float(val)):
            raise Violation(
                "OrderResult.%s came back as %r. It will pass every "
                "comparison-based guard downstream." % (label, val))

    # And in the outbound direction.
    for bad in (float("nan"), float("inf")):
        v2 = FakeVenue()
        a2 = make_adapter(v2)
        try:
            a2.place_order(Order("slc-conformance-5", "BTC/USDT", "buy", bad))
        except VenueError:
            continue
        raise Violation("the adapter transmitted qty=%r to the venue" % bad)


def law_cancel_only_true_when_confirmed(make_adapter):
    """cancel() may return True only for a cancel the venue CONFIRMED.
    A pending cancel can still lose the race to a fill."""
    for fault in (CANCEL_UNCONFIRMED, CANCEL_PENDING):
        venue = FakeVenue([fault])
        adapter = make_adapter(venue)
        try:
            ok = adapter.cancel("V-1", "BTC/USDT")
        except VenueError:
            continue                      # raising is correct
        if ok:
            raise Violation(
                "cancel() returned True under %r. The order is still working "
                "and the position is still live, but the engine now believes "
                "it is flat." % fault)


LAWS = [
    ("read_only blocks before the wire", law_read_only_blocks_before_the_wire),
    ("never claims unprovable absence", law_never_claims_absence_it_cannot_prove),
    ("no non-finite number escapes", law_no_non_finite_escapes),
    ("cancel true only when confirmed", law_cancel_only_true_when_confirmed),
]


def run(name, make_adapter, verbose=True):
    """Run every law plus the idempotency law under every fault."""
    failures = []
    for fault in ALL_FAULTS:
        try:
            law_idempotent_under_fault(make_adapter, fault)
            if verbose:
                print("    ok   idempotent under %s" % fault)
        except Violation as e:
            failures.append(("idempotent under %s" % fault, str(e)))
            if verbose:
                print("    FAIL idempotent under %s" % fault)
    for label, law in LAWS:
        try:
            law(make_adapter)
            if verbose:
                print("    ok   %s" % label)
        except Violation as e:
            failures.append((label, str(e)))
            if verbose:
                print("    FAIL %s" % label)
        except NotImplementedError:
            if verbose:
                print("    skip %s (not applicable)" % label)
    return failures


def require_conformance(kind, make_adapter):
    """The gate. An adapter registers only if it passes.

    This is the structural change: conformance stops being a document an
    author is trusted to have read and becomes a precondition of being
    constructible, in the same way brokers.build() already refuses an
    unregistered kind.
    """
    failures = run(kind, make_adapter, verbose=False)
    if failures:
        raise VenueError(
            "adapter %r fails %d conformance law(s) and will not be "
            "registered: %s" % (kind, len(failures),
                                "; ".join(f[0] for f in failures)))
    return True
