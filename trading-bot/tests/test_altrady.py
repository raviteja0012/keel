"""Altrady strategy-host adapter.

The centrepiece is the allowlist: POST /v2/signal_bot_positions opens and
closes REAL positions with the SAME credentials the read path uses (Altrady
has no scopes), so the tests prove the transport refuses that call before any
network I/O happens. The other pin is landmine L1: the positions GET carries
api_key and api_secret in the query string, so a transport error whose text
embeds the full URL — which is exactly what requests produces — must surface
scrubbed. The rest holds the house discipline: an unreadable P&L is None and
never 0, a state that could not be fetched raises rather than reading as
stopped, and read_only refuses control before touching the wire.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "alt.db"))

import json                                                          # noqa: E402

import requests                                                      # noqa: E402

from brokers.altrady import (_ALLOWED, _POSITIONS_PATH,              # noqa: E402
                             _START_STOP_PATH, AltradyHost, _num, _scrub)
from brokers.strategy_host import (STOP_ORPHANS_POSITIONS,           # noqa: E402
                                   HostError, HostReadOnly, StrategyHost,
                                   build_host, host_kinds)

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


KEY = "ak-4f9d2c81e74b0a6d"
SECRET = "as-SUPERSECRET-93b1xq77"


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


def host(script=(), read_only=False, bots=None):
    cfg = {"name": "alt", "read_only": read_only,
           "bots": bots if bots is not None else [
               {"bot_id": "alpha", "name": "Alpha",
                "api_key": KEY, "api_secret": SECRET}]}
    h = AltradyHost(cfg)
    h._session = FakeSession(script)
    return h


def row(**over):
    base = {"id": "p-1", "coinraySymbol": "BINA_USDT_BTC", "status": "running",
            "side": "long", "invested": "100", "feesPaid": "0.1",
            "realizedProfit": "0", "unrealizedProfit": "1.5",
            "netProfit": "1.4"}
    base.update(over)
    return base


# --------------------------------------------------------------- allowlist

def test_allowlist_is_exactly_the_two_read_and_trigger_operations():
    check("the allowlist holds exactly two operations", len(_ALLOWED) == 2,
          sorted(_ALLOWED))
    check("GET positions is allowed", ("GET", _POSITIONS_PATH) in _ALLOWED)
    check("POST start_stop is allowed", ("POST", _START_STOP_PATH) in _ALLOWED)
    check("POST to the positions path — the order-placement endpoint — is not",
          ("POST", _POSITIONS_PATH) not in _ALLOWED)


def test_position_management_post_is_refused_before_any_network_io():
    # Landmine L2: same path as the read, one verb apart, opens real
    # positions. The refusal must precede I/O, which is proven by the fake
    # session recording nothing at all.
    h = host(script=[])
    try:
        h._request("POST", _POSITIONS_PATH,
                   body={"api_key": KEY, "api_secret": SECRET,
                         "action": "open"})
        check("POST /v2/signal_bot_positions must be refused", False)
    except HostError as e:
        check("POST /v2/signal_bot_positions is refused",
              "allowlist" in str(e))
    check("and the refusal happened before any network I/O",
          not h._session.sent)


def test_every_other_operation_on_the_platform_is_refused():
    h = host(script=[])
    for method, path in [("POST", "/v2/signal_bot/filters"),
                         ("POST", "/v2/signal_bot/bulk"),
                         ("GET", _START_STOP_PATH),
                         ("POST", "/v2/smart_positions/whk/cancel"),
                         ("GET", "/v2/smart_positions/whk/cancel"),
                         ("DELETE", _POSITIONS_PATH)]:
        try:
            h._request(method, path)
            check("%s %s must be refused" % (method, path), False)
        except HostError as e:
            check("%s %s is refused" % (method, path), "allowlist" in str(e))
    check("none of them reached the wire", not h._session.sent)


# ------------------------------------------------------------- credentials

def test_connection_error_embedding_the_url_surfaces_scrubbed():
    # Landmine L1: the GET puts api_key AND api_secret in the query string,
    # and requests embeds the full URL in its exception text. This is the
    # exact leak path named in the brief, reproduced verbatim.
    msg = ("HTTPSConnectionPool(host='api.altrady.com', port=443): Max "
           "retries exceeded with url: /v2/signal_bot_positions?api_key=%s"
           "&api_secret=%s&status=open (connection refused)" % (KEY, SECRET))
    h = host(script=[requests.ConnectionError(msg)])
    try:
        h.bot_state("alpha")
        check("a transport failure must raise", False)
    except HostError as e:
        s = str(e)
        check("the api_secret is absent from the exception", SECRET not in s, s)
        check("the api_key is absent from the exception", KEY not in s, s)
        check("the scrub marker shows where values were removed", "***" in s)
        check("the original exception is not carried as cause (its args "
              "still hold the URL)", e.cause is None)


def test_the_credential_does_not_survive_in_the_exception_chain():
    """BEHAVIOUR, not shape. str(e) being clean is not enough: `raise ...`
    inside an `except` chains the original requests exception as __context__,
    and traceback.format_exc(), logging(exc_info=True) and str(e.__context__)
    all resurrect the unscrubbed URL from there. The earlier test only checked
    str(e), so this whole channel was green while leaking. Here we assert on
    the channels that actually carry it into logs.
    """
    import traceback
    msg = ("Max retries exceeded with url: /v2/signal_bot_positions?api_key=%s"
           "&api_secret=%s&status=open" % (KEY, SECRET))
    h = host(script=[requests.ConnectionError(msg)])
    try:
        h.bot_state("alpha")
        check("a transport failure must raise", False)
    except HostError as e:
        tb = traceback.format_exc()
        check("the api_secret is absent from the FULL traceback",
              SECRET not in tb, "leaked in traceback")
        check("the api_key is absent from the full traceback", KEY not in tb)
        check("__context__ is not populated (the chain was never linked)",
              e.__context__ is None, repr(e.__context__)[:80])
        check("__cause__ is absent too", e.__cause__ is None)


def test_an_empty_positions_body_is_not_a_flat_book():
    """FAIL-OPEN. An empty or truncated 200 on the positions GET used to map
    {} -> [] -> a confirmed-flat, valued, trustworthy book. hosted_exposure
    would count that bot as fully valued at zero. A read that required content
    and got none is a failure, not 'nothing held'."""
    h = host(script=[FakeResponse(None, 200)])   # 200, empty body
    try:
        h.bot_state("alpha")
        check("an empty positions body must not read as a flat book", False,
              "returned instead of raising")
    except HostError as e:
        check("an empty required-body read raises", "empty body" in str(e), str(e))


def test_http_error_body_echoing_the_secret_is_scrubbed():
    h = host(script=[FakeResponse({"error": "bad credentials",
                                   "echo": "api_secret=%s" % SECRET}, 400)])
    try:
        h.bot_state("alpha")
        check("an HTTP 400 must raise", False)
    except HostError as e:
        check("a secret echoed in the response body is scrubbed",
              SECRET not in str(e), str(e))
        check("and the status code is preserved", e.http_status == 400)


def test_scrub_erases_raw_and_urlencoded_spellings():
    sec = "ab+cd/ef=="
    check("raw spelling is erased",
          sec not in _scrub("x %s y" % sec, (sec,)))
    check("urlencoded spelling is erased too",
          "ab%2Bcd%2Fef%3D%3D" not in _scrub(
              "url?api_secret=ab%2Bcd%2Fef%3D%3D", (sec,)))
    check("empty secrets do not corrupt the text",
          _scrub("hello", ("",)) == "hello")


def test_config_error_for_missing_secret_does_not_echo_the_key():
    try:
        AltradyHost({"name": "alt",
                     "bots": [{"bot_id": "x", "api_key": KEY}]})
        check("a bot without api_secret must be refused", False)
    except HostError as e:
        check("missing api_secret is named by field name",
              "api_secret" in str(e))
        check("and the api_key value that WAS present is not echoed",
              KEY not in str(e), str(e))


def test_duplicate_bot_id_is_refused():
    try:
        AltradyHost({"name": "alt", "bots": [
            {"bot_id": "x", "api_key": "k1", "api_secret": "s1"},
            {"bot_id": "x", "api_key": "k2", "api_secret": "s2"}]})
        check("a duplicate bot_id must be refused", False)
    except HostError as e:
        check("a duplicate bot_id is refused", "twice" in str(e))


def test_bots_registry_carries_no_credentials():
    h = host()
    r = repr(h.bots())
    check("api_secret never appears in the bots() repr", SECRET not in r)
    check("api_key never appears in the bots() repr", KEY not in r)


# -------------------------------------------------------------- read_only

def test_read_only_is_the_default():
    h = AltradyHost({"name": "alt", "bots": [
        {"bot_id": "alpha", "api_key": KEY, "api_secret": SECRET}]})
    check("a host with no read_only key is read-only", h.read_only is True)


def test_read_only_refuses_control_before_touching_the_network():
    h = host(script=[], read_only=True)
    for op in (h.start_bot, h.stop_bot):
        try:
            op("alpha")
            check("%s must refuse when read-only" % op.__name__, False)
        except HostReadOnly:
            check("%s refuses when read-only" % op.__name__, True)
    check("and nothing was sent", not h._session.sent)


def test_reading_is_allowed_while_read_only():
    h = host(script=[FakeResponse([])], read_only=True)
    st = h.bot_state("alpha")
    check("bot_state works on a read-only host", st.bot_id == "alpha")
    check("bots() works on a read-only host",
          host(read_only=True).bots()[0].bot_id == "alpha")


# ---------------------------------------------------------------- control

def test_start_sends_active_true_in_the_body_not_the_url():
    h = host(script=[FakeResponse(None, 201)])
    a = h.start_bot("alpha")
    sent = h._session.sent[0]
    body = json.loads(sent["body"])
    check("start POSTs to /v2/signal_bot/start_stop",
          sent["method"] == "POST" and sent["url"].endswith(_START_STOP_PATH))
    check("active=true rides in the body", body["active"] is True)
    check("credentials ride in the body as the API requires",
          body["api_key"] == KEY and body["api_secret"] == SECRET)
    check("and the POST URL carries no query string", "?" not in sent["url"])
    check("the action reports running", a.running is True and a.bot_id == "alpha")


def test_stop_sends_active_false():
    h = host(script=[FakeResponse(None, 201)])
    a = h.stop_bot("alpha")
    body = json.loads(h._session.sent[0]["body"])
    check("stop sends active=false", body["active"] is False)
    check("and reports not running", a.running is False)


def test_changed_is_asserted_because_prior_state_is_unreadable():
    # No endpoint reports a bot's active flag and the 201 has no schema, so
    # a repeat command cannot be observed as a no-op. The platform treats
    # active as a set, not a toggle, which is what keeps the repeat safe.
    h = host(script=[FakeResponse(None, 201), FakeResponse(None, 201)])
    first = h.start_bot("alpha")
    second = h.start_bot("alpha")
    check("a repeated start is a success, not an error",
          first.changed and second.changed)
    check("and the detail says why changed cannot be observed",
          "set, not a toggle" in second.detail, second.detail)


def test_control_state_is_remembered_for_running():
    h = host(script=[FakeResponse([])])
    check("before any command a bot is presumed live",
          h.bots()[0].running is True)
    check("and bot_state says so", h.bot_state("alpha").running is True)
    h2 = host(script=[FakeResponse(None, 201), FakeResponse([])])
    h2.stop_bot("alpha")
    check("after a stop, bots() reflects the last command",
          h2.bots()[0].running is False)
    check("and bot_state does too", h2.bot_state("alpha").running is False)


def test_unknown_bot_is_refused_without_network():
    h = host(script=[])
    for fn in (h.bot_state, h.start_bot, h.stop_bot):
        try:
            fn("ghost")
            check("%s must refuse an unknown bot" % fn.__name__, False)
        except HostReadOnly:
            check("%s must refuse an unknown bot" % fn.__name__, False,
                  "read_only fired first on an armed host")
        except HostError as e:
            check("%s refuses an unknown bot" % fn.__name__,
                  "ghost" in str(e))
    check("and nothing was sent", not h._session.sent)


# -------------------------------------------------------------- bot_state

def test_open_positions_and_their_quote_currency_pnl_sum():
    h = host(script=[FakeResponse([
        row(unrealizedProfit="1.5"),
        row(id="p-2", side="short", unrealizedProfit="-0.25")])])
    st = h.bot_state("alpha")
    check("two open positions are reported", len(st.positions) == 2)
    check("the unrealized sum is exact", st.unrealized_pnl == 1.25,
          st.unrealized_pnl)
    check("and the state is valued", st.valued is True)
    check("long maps to buy", st.positions[0].side == "buy")
    check("short maps to sell", st.positions[1].side == "sell")
    check("the vendor symbol passes through verbatim",
          st.positions[0].symbol == "BINA_USDT_BTC")
    check("quantity is marked unknown, not invented",
          st.positions[0].qty == 0.0
          and st.positions[0].raw["qty_known"] is False)
    check("the GET carried status=open in the query",
          "status=open" in h._session.sent[0]["url"])
    check("as_of is stamped", st.as_of > 0)


def test_one_unreadable_row_makes_the_whole_pnl_none_never_zero():
    h = host(script=[FakeResponse([
        row(unrealizedProfit=None),
        row(id="p-2", unrealizedProfit="3.0")])])
    st = h.bot_state("alpha")
    check("an unreadable P&L is None", st.unrealized_pnl is None)
    check("not zero, and not the sum of the readable rows",
          st.unrealized_pnl != 0.0 and st.unrealized_pnl != 3.0)
    check("the state is explicitly unvalued", st.valued is False)
    check("but the positions are still visible", len(st.positions) == 2)


def test_garbage_number_is_as_unreadable_as_a_missing_one():
    h = host(script=[FakeResponse([row(unrealizedProfit="abc")])])
    check("garbage P&L is None", h.bot_state("alpha").unrealized_pnl is None)


def test_a_confirmed_empty_book_is_a_real_zero():
    # The read succeeded and said "no open positions". That is a known-flat
    # book, not an unknown one — the opposite case from an unreadable row.
    h = host(script=[FakeResponse([])])
    st = h.bot_state("alpha")
    check("an empty book sums to a real 0.0", st.unrealized_pnl == 0.0)
    check("and is valued", st.valued is True)
    check("with no positions", st.positions == [])


def test_a_saturated_100_row_window_withholds_the_sum():
    # The endpoint returns "the 100 most recently updated positions" with no
    # pagination. At exactly 100 rows the open book may be truncated, and an
    # unread position is not a flat one.
    h = host(script=[FakeResponse([row(id="p-%d" % i, unrealizedProfit="0.1")
                                   for i in range(100)])])
    st = h.bot_state("alpha")
    check("a saturated window makes the sum None", st.unrealized_pnl is None)
    check("all 100 rows are still reported as positions",
          len(st.positions) == 100)
    check("and the detail says the window saturated", "window" in st.detail)


def test_realized_pnl_is_never_fabricated_from_the_window():
    h = host(script=[FakeResponse([row(realizedProfit="5.0")])])
    check("realized_pnl is None — a 100-row window cannot yield a lifetime "
          "total", h.bot_state("alpha").realized_pnl is None)


def test_single_object_response_shape_is_accepted():
    # alt.json declares the response schema as ONE SignalBotPosition while
    # the description promises a list; the adapter accepts both.
    h = host(script=[FakeResponse(row())])
    check("a single-object response reads as one position",
          len(h.bot_state("alpha").positions) == 1)


def test_unfetchable_state_raises_rather_than_reading_as_stopped():
    h = host(script=[FakeResponse(None, 500)])
    try:
        st = h.bot_state("alpha")
        check("an unreadable state must raise, not return running=%s"
              % st.running, False)
    except HostError as e:
        check("an unreadable state raises HostError", True)
        check("with the venue's status attached", e.http_status == 500)


# ----------------------------------------------------------------- health

def test_health_never_raises_and_reports_what_it_established():
    ok = host(script=[FakeResponse([row()])]).health()
    check("a working host is reachable and authenticated",
          ok.reachable and ok.authenticated)
    check("and reports latency", ok.latency_ms is not None)

    denied = host(script=[FakeResponse({"error": "unauthorized"}, 401)]).health()
    check("a 401 is reachable but not authenticated",
          denied.reachable and not denied.authenticated)

    dead = host(script=[requests.ConnectionError(
        "url: /v2/signal_bot_positions?api_key=%s&api_secret=%s"
        % (KEY, SECRET))]).health()
    check("a connection failure is unreachable", not dead.reachable)
    check("and its detail is scrubbed",
          SECRET not in dead.detail and KEY not in dead.detail, dead.detail)


def test_health_with_no_bots_probes_nothing():
    h = host(script=[], bots=[])
    ph = h.health()
    check("no bots means nothing to probe with",
          not ph.reachable and not ph.authenticated)
    check("and no request was attempted", not h._session.sent)


# --------------------------------------------------------------- contract

def test_registered_as_a_strategy_host():
    check("'altrady' is a registered host kind", "altrady" in host_kinds())
    built = build_host("altrady", {"name": "alt", "bots": [
        {"bot_id": "alpha", "api_key": KEY, "api_secret": SECRET}]})
    check("build_host constructs an AltradyHost",
          isinstance(built, AltradyHost))
    check("which satisfies the StrategyHost protocol",
          isinstance(built, StrategyHost))


def test_stop_disposition_is_declared_orphans_from_the_docs():
    # alt.json, stop_bot: "Any open positions will remain open." — and
    # stop_and_close exists separately precisely because plain stop does not
    # close. Declared from that evidence, not guessed.
    check("stopping orphans positions",
          AltradyHost.stop_disposition == STOP_ORPHANS_POSITIONS)


def test_num_keeps_unknown_unknown():
    check("None stays None", _num(None) is None)
    check("empty string stays None", _num("") is None)
    check("garbage stays None", _num("abc") is None)
    check("NaN is not a number", _num(float("nan")) is None)
    check("inf is not a number", _num(float("inf")) is None)
    check("zero is a real zero", _num("0") == 0.0)


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
