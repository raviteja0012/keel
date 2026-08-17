"""3Commas strategy-host adapter.

The centrepiece is the allowlist: 3Commas cannot scope a credential to
"start and stop but never manage" — BOTS_WRITE authorises panic_sell too —
so the adapter enforces it client-side, before any network I/O. The tests
here pin that gate from both directions: unlisted endpoints are refused
with nothing sent, and the allowlist itself is statically free of every
deal-management and bot-mutation endpoint.

The rest pins the house discipline: an unreadable state raises rather than
reading as stopped or flat, an unreadable P&L is None and never 0, start
and stop are idempotent no-ops when the bot is already there, and a secret
pushed through the transport never surfaces in an exception message.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "tc.db"))

import requests                                                      # noqa: E402

import brokers.threecommas_hosts as th                               # noqa: E402
from brokers.strategy_host import (STOP_ORPHANS_POSITIONS,           # noqa: E402
                                   HostError, HostReadOnly, StrategyHost,
                                   build_host, host_kinds)
from brokers.threecommas_hosts import (_ALLOWED, ThreeCommasBots,    # noqa: E402
                                       _keel_pair, _num)

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


KEY = "3c-key-1b2c3d4e5f60718293a4b5c6d7e8f901"
SECRET = "3c-secret-THIS-MUST-NEVER-LEAK-a1b2c3d4e5f6"
BASE = "https://api.3commas.io"


class FakeResponse:
    def __init__(self, payload, status=200, text=None):
        self._payload = payload
        self.status_code = status
        self.content = b"x" if payload is not None else b""
        self.text = (text if text is not None
                     else (json.dumps(payload) if payload is not None else ""))

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


def host(script=(), read_only=True, **cfg):
    conf = {"api_key": KEY, "api_secret": SECRET, "name": "3c-bots",
            "read_only": read_only}
    conf.update(cfg)
    h = ThreeCommasBots(conf)
    h._session = FakeSession(script)
    return h


def path_of(sent):
    return sent["url"].replace(BASE, "")


DCA_BOT = {"id": 7, "name": "eth accumulator", "is_enabled": True,
           "pairs": ["USDT_ETH"], "active_deals_count": 1}
GRID_BOT = {"id": 9, "name": "btc grid", "is_enabled": False,
            "pair": "USDT_BTC"}
DEAL = {"id": 987, "bot_id": 7, "pair": "USDT_ETH", "status": "bought",
        "strategy": "long", "bought_amount": "0.5",
        "bought_average_price": "2000.0", "actual_usd_profit": "12.5"}
STATS = {"active": 1, "completed": 40, "active_deals_usd_profit": "12.5",
         "completed_deals_usd_profit": "100.25"}


# ---------------------------------------------------- empty-body fail-open

def test_an_empty_body_on_a_deals_read_is_not_a_flat_book():
    """FAIL-OPEN (the Altrady-class defect). _request returned {} on an empty
    body, which decayed through `or []` into an empty deals page — a bot
    reported flat because the response was truncated, not because it holds
    nothing. Every allowlisted endpoint returns a body, so an empty one is a
    failure, and bot_state must raise rather than report zero positions."""
    # show reads fine; the active-deals read comes back with an empty body.
    h = host(script=[FakeResponse(DCA_BOT), FakeResponse(None, status=200)])
    try:
        h.bot_state("dca:7")
        check("empty deals body must not read as a flat book", False,
              "returned instead of raising")
    except HostError as e:
        check("empty required body raises", "empty body" in str(e), str(e))


def test_an_empty_body_on_the_bots_list_raises():
    h = host(script=[FakeResponse(None, status=200)])
    try:
        h.bots()
        check("empty bots-list body must not read as 'no bots'", False)
    except HostError as e:
        check("empty bots list raises", "empty body" in str(e), str(e))


# ------------------------------------------------------------------ signing

def test_signature_is_hmac_sha256_over_the_full_prefixed_path():
    import hashlib
    import hmac as hmac_mod
    h = host(script=[FakeResponse([DCA_BOT]), FakeResponse([GRID_BOT])])
    h.bots()
    sent = h._session.sent[0]
    path = path_of(sent)
    expect = hmac_mod.new(SECRET.encode(), path.encode(),
                          hashlib.sha256).hexdigest()
    check("the path starts with the /public/api prefix",
          path.startswith("/public/api/ver1/bots"), path)
    check("the query string is part of the signed message", "?" in path, path)
    check("Signature is HMAC-SHA256 of the full prefixed path",
          sent["headers"]["Signature"] == expect)
    check("Apikey header carries the key",
          sent["headers"]["Apikey"] == KEY)
    check("no request carries a body",
          all(s["body"] is None for s in h._session.sent))


# ---------------------------------------------------------------- allowlist

def test_unlisted_endpoints_are_refused_before_any_network_io():
    h = host(script=[])
    for method, tpl in [("POST", "/ver1/bots/{id}/panic_sell_all_deals"),
                        ("POST", "/ver1/bots/{id}/cancel_all_deals"),
                        ("GET", "/ver1/smart_trades/v2"),
                        ("POST", "/ver1/bots/create_bot"),
                        ("DELETE", "/ver1/grid_bots/{id}")]:
        try:
            h._request(method, tpl, bot_id=7)
            check("unlisted %s %s must be refused" % (method, tpl), False)
        except HostError as e:
            check("unlisted %s %s is refused" % (method, tpl),
                  "allowlist" in str(e))
    check("and nothing at all was sent", not h._session.sent)


def test_the_allowlist_contains_no_management_endpoints():
    # The static half of R2: the frozenset itself must never grow a
    # deal-management or bot-mutation endpoint. Order placement exists on
    # this platform; it must not exist here.
    forbidden = ("panic", "cancel", "create", "update", "delete",
                 "smart_trades", "close", "sell", "manual", "note",
                 "add_funds", "market_order")
    for method, tpl in _ALLOWED:
        for bad in forbidden:
            check("allowlist %s %s is free of %r" % (method, tpl, bad),
                  bad not in tpl)
        check("allowlist method %s %s is GET or POST" % (method, tpl),
              method in ("GET", "POST"))
        if method == "POST":
            check("every POST is enable/disable only: %s" % tpl,
                  tpl.endswith("/enable") or tpl.endswith("/disable"))


def test_bot_ids_that_are_not_positive_ints_cannot_reach_the_wire():
    h = host(script=[])
    for bad in ("7/../9", "7?x=1", 0, -3, None, True, 7.0):
        try:
            h._request("GET", "/ver1/bots/{id}/show", bot_id=bad)
            check("bot id %r must be refused" % (bad,), False)
        except HostError:
            check("bot id %r is refused before I/O" % (bad,), True)
    check("and nothing was sent", not h._session.sent)


# ------------------------------------------------------------- credentials

def test_a_secret_in_a_transport_error_never_surfaces():
    h = host(script=[requests.exceptions.ConnectionError(
        "boom apikey=%s secret=%s" % (KEY, SECRET))])
    try:
        h.bots()
        check("a transport failure must raise", False)
    except HostError as e:
        check("the secret is absent from the exception", SECRET not in str(e))
        check("the api key is absent from the exception", KEY not in str(e))
        check("the failure is marked retryable", e.retryable is True)
        check("and carries no http status", e.http_status is None)


def test_a_secret_echoed_in_an_error_body_is_scrubbed():
    body = {"error": "signature mismatch for %s / %s" % (KEY, SECRET)}
    h = host(script=[FakeResponse(body, status=401)])
    try:
        h.bots()
        check("a 401 must raise", False)
    except HostError as e:
        check("the echoed secret is scrubbed from the message",
              SECRET not in str(e))
        check("the echoed key is scrubbed too", KEY not in str(e))
        check("the status survives", e.http_status == 401)


def test_repr_and_health_detail_carry_no_credentials():
    h = host(script=[FakeResponse({"error": "bad " + SECRET}, status=401)])
    check("repr has no secret", SECRET not in repr(h))
    check("repr has no key", KEY not in repr(h))
    hh = h.health()
    check("health detail has no secret", SECRET not in hh.detail)
    check("health detail has no key", KEY not in hh.detail)


# ---------------------------------------------------------------- read-only

def test_read_only_is_the_default_and_blocks_control_before_io():
    h = ThreeCommasBots({"api_key": KEY, "api_secret": SECRET})
    check("read_only defaults to True", h.read_only is True)
    h = host(script=[])  # read_only=True
    for call in (h.start_bot, h.stop_bot):
        try:
            call("dca:7")
            check("%s must refuse when read-only" % call.__name__, False)
        except HostReadOnly:
            check("%s refuses when read-only" % call.__name__, True)
    check("and nothing was sent", not h._session.sent)


def test_reading_is_allowed_while_read_only():
    h = host(script=[FakeResponse([DCA_BOT]), FakeResponse([])],
             read_only=True)
    bots = h.bots()
    check("bots() works on a read-only host", len(bots) == 1)


# ------------------------------------------------------------ start / stop

def test_start_on_a_stopped_dca_bot_enables_it():
    h = host(script=[FakeResponse(dict(DCA_BOT, is_enabled=False)),
                     FakeResponse(dict(DCA_BOT, is_enabled=True))],
             read_only=False)
    a = h.start_bot("dca:7")
    check("changed is True", a.changed is True)
    check("running is True", a.running is True)
    post = [s for s in h._session.sent if s["method"] == "POST"]
    check("exactly one POST went out", len(post) == 1)
    check("and it hit the DCA enable endpoint",
          path_of(post[0]).startswith("/public/api/ver1/bots/7/enable"),
          path_of(post[0]))


def test_start_on_a_running_bot_is_a_noop_success():
    h = host(script=[FakeResponse(DCA_BOT)], read_only=False)
    a = h.start_bot("dca:7")
    check("already-running start returns changed=False", a.changed is False)
    check("and running=True", a.running is True)
    check("and no POST was sent",
          not [s for s in h._session.sent if s["method"] == "POST"])


def test_stop_on_a_running_bot_disables_it_and_stop_is_idempotent():
    h = host(script=[FakeResponse(DCA_BOT),
                     FakeResponse(dict(DCA_BOT, is_enabled=False))],
             read_only=False)
    a = h.stop_bot("dca:7")
    check("stop flips a running bot", a.changed is True and a.running is False)
    post = [s for s in h._session.sent if s["method"] == "POST"]
    check("via the disable endpoint",
          path_of(post[0]).startswith("/public/api/ver1/bots/7/disable"))

    h2 = host(script=[FakeResponse(dict(DCA_BOT, is_enabled=False))],
              read_only=False)
    a2 = h2.stop_bot("dca:7")
    check("stopping a stopped bot is changed=False, not an error",
          a2.changed is False and a2.running is False)
    check("and sends no POST",
          not [s for s in h2._session.sent if s["method"] == "POST"])


def test_grid_control_uses_the_grid_endpoints():
    h = host(script=[FakeResponse(GRID_BOT),
                     FakeResponse(dict(GRID_BOT, is_enabled=True))],
             read_only=False)
    a = h.start_bot("grid:9")
    check("grid start succeeds", a.changed is True and a.running is True)
    post = [s for s in h._session.sent if s["method"] == "POST"][0]
    check("via /ver1/grid_bots/9/enable",
          path_of(post).startswith("/public/api/ver1/grid_bots/9/enable"),
          path_of(post))


def test_an_enable_that_does_not_take_fails_closed():
    h = host(script=[FakeResponse(dict(DCA_BOT, is_enabled=False)),
                     FakeResponse(dict(DCA_BOT, is_enabled=False))],
             read_only=False)
    try:
        h.start_bot("dca:7")
        check("an enable the venue did not apply must raise", False)
    except HostError as e:
        check("accepted-but-not-applied raises rather than claiming success",
              "still reports" in str(e))


def test_a_flip_response_without_the_flag_triggers_a_reread():
    h = host(script=[FakeResponse(dict(DCA_BOT, is_enabled=False)),
                     FakeResponse({}),          # 201 body without is_enabled
                     FakeResponse(dict(DCA_BOT, is_enabled=True))],
             read_only=False)
    a = h.start_bot("dca:7")
    check("a confirmation-free 2xx is verified by re-reading",
          a.changed is True and a.running is True)
    check("three requests went out", len(h._session.sent) == 3)


def test_bare_or_unknown_bot_ids_are_refused():
    h = host(script=[], read_only=False)
    for bad in ("123", "foo:1", "dca:", "dca:-2", "grid:x", ""):
        try:
            h.bot_state(bad) if bad != "123" else h.start_bot(bad)
            check("bot id %r must be refused" % bad, False)
        except HostError:
            check("bot id %r is refused" % bad, True)
    check("and nothing was sent", not h._session.sent)


# ------------------------------------------------------------------- state

def test_dca_state_reads_running_positions_and_pnl():
    h = host(script=[FakeResponse(DCA_BOT), FakeResponse([DEAL]),
                     FakeResponse(STATS)])
    s = h.bot_state("dca:7")
    check("running comes from is_enabled", s.running is True)
    check("one position per active deal", len(s.positions) == 1)
    p = s.positions[0]
    check("pair is respelled BASE/QUOTE", p.symbol == "ETH/USDT", p.symbol)
    check("long spells buy", p.side == "buy")
    check("qty from bought_amount", p.qty == 0.5)
    check("entry from bought_average_price", p.entry_price == 2000.0)
    check("unrealized_pnl from active_deals_usd_profit",
          s.unrealized_pnl == 12.5)
    check("realized_pnl from completed_deals_usd_profit",
          s.realized_pnl == 100.25)
    check("the state is valued", s.valued is True)
    check("as_of is set", s.as_of > 0)
    deals_get = path_of(h._session.sent[1])
    check("deals were scoped to this bot",
          "bot_id=7" in deals_get and "scope=active" in deals_get, deals_get)


def test_unreadable_pnl_is_none_never_zero():
    # The stats endpoint dies; the state read must survive with None P&L.
    h = host(script=[FakeResponse(DCA_BOT), FakeResponse([DEAL]),
                     FakeResponse(None, status=500)])
    s = h.bot_state("dca:7")
    check("state still returns when only P&L is unreadable",
          s.running is True and len(s.positions) == 1)
    check("unreadable unrealized P&L is None", s.unrealized_pnl is None)
    check("unreadable realized P&L is None", s.realized_pnl is None)
    check("the state is explicitly unvalued", s.valued is False)
    check("and the detail says why", "P&L unreadable" in s.detail, s.detail)

    # A present but missing/garbled field is equally unknown (R7).
    h2 = host(script=[FakeResponse(DCA_BOT), FakeResponse([DEAL]),
                      FakeResponse({"completed_deals_usd_profit": "abc"})])
    s2 = h2.bot_state("dca:7")
    check("a missing active_deals_usd_profit is None",
          s2.unrealized_pnl is None)
    check("an unparseable completed_deals_usd_profit is None",
          s2.realized_pnl is None)


def test_an_unreadable_bot_raises_instead_of_reading_as_stopped():
    h = host(script=[FakeResponse(None, status=503)])
    try:
        h.bot_state("dca:7")
        check("an unreadable show must raise", False)
    except HostError as e:
        check("an unreadable show raises HostError", True)
        check("and is marked retryable for a 503", e.retryable is True)


def test_unreadable_deals_raise_rather_than_reporting_a_flat_book():
    h = host(script=[FakeResponse(DCA_BOT), FakeResponse(None, status=500)])
    try:
        h.bot_state("dca:7")
        check("an unreadable deal list must raise", False)
    except HostError:
        check("an unreadable deal list raises: no positions is a claim, "
              "not a fallback", True)


def test_missing_is_enabled_is_refused_not_read_as_stopped():
    h = host(script=[FakeResponse({"id": 7, "name": "mystery"})])
    try:
        h.bot_state("dca:7")
        check("a show without is_enabled must raise", False)
    except HostError as e:
        check("a show without is_enabled raises", "is_enabled" in str(e))


def test_grid_state_values_dollar_quoted_grids_only():
    g_usd = dict(GRID_BOT, is_enabled=True,
                 unrealized_profit_loss="-3.4", current_profit_usd="8.8")
    h = host(script=[FakeResponse(g_usd)])
    s = h.bot_state("grid:9")
    check("a USDT-quoted grid reports unrealized P&L",
          s.unrealized_pnl == -3.4)
    check("and realized from current_profit_usd", s.realized_pnl == 8.8)
    check("grid running from is_enabled", s.running is True)

    # BTC-quoted: the venue does not document the unit of
    # unrealized_profit_loss, and a BTC number summed as USD is a
    # real-money error. Unvalued and visible beats confidently wrong.
    g_btc = {"id": 11, "name": "eth/btc grid", "is_enabled": True,
             "pair": "BTC_ETH", "unrealized_profit_loss": "0.5",
             "current_profit_usd": "4.2"}
    h2 = host(script=[FakeResponse(g_btc)])
    s2 = h2.bot_state("grid:11")
    check("a BTC-quoted grid's unrealized P&L is withheld as None",
          s2.unrealized_pnl is None)
    check("so the bot reads as unvalued, not flat", s2.valued is False)
    check("and the detail explains the withholding",
          "not dollar-pegged" in s2.detail, s2.detail)


def test_deals_with_unparseable_numbers_are_reported_not_dropped():
    broken = dict(DEAL, bought_amount=None, actual_usd_profit="oops",
                  strategy="short")
    h = host(script=[FakeResponse(DCA_BOT), FakeResponse([broken]),
                     FakeResponse(STATS)])
    s = h.bot_state("dca:7")
    check("the deal still appears as a position", len(s.positions) == 1)
    p = s.positions[0]
    check("short spells sell", p.side == "sell")
    check("an unknown qty is flagged, not invented",
          p.qty == 0.0 and p.raw["qty_known"] is False)
    check("an unknown per-deal P&L is flagged",
          p.raw["upnl_known"] is False)


# -------------------------------------------------------------------- bots

def test_bots_lists_both_families():
    h = host(script=[FakeResponse([DCA_BOT]), FakeResponse([GRID_BOT])])
    bots = h.bots()
    check("both families are listed", len(bots) == 2)
    dca, grid = bots[0], bots[1]
    check("dca id is namespaced", dca.bot_id == "dca:7")
    check("grid id is namespaced", grid.bot_id == "grid:9")
    check("strategies are labelled", (dca.strategy, grid.strategy)
          == ("dca", "grid"))
    check("running flags carried through",
          dca.running is True and grid.running is False)
    check("pairs carried through",
          dca.pairs == ["USDT_ETH"] and grid.pairs == ["USDT_BTC"])
    check("host is stamped on every bot",
          all(b.host == "3c-bots" for b in bots))


def test_bots_paginates_until_a_short_page():
    old = th._PAGE_LIMIT_BOTS
    th._PAGE_LIMIT_BOTS = 2
    try:
        page1 = [dict(DCA_BOT, id=1), dict(DCA_BOT, id=2)]
        page2 = [dict(DCA_BOT, id=3)]
        h = host(script=[FakeResponse(page1), FakeResponse(page2),
                         FakeResponse([])])
        bots = h.bots()
        check("a full page triggers another read", len(bots) == 3)
        check("offsets advance by the limit",
              "offset=0" in path_of(h._session.sent[0])
              and "offset=2" in path_of(h._session.sent[1]))
    finally:
        th._PAGE_LIMIT_BOTS = old


def test_page_budget_exhaustion_raises_rather_than_truncating():
    old_limit, old_pages = th._PAGE_LIMIT_BOTS, th._MAX_PAGES
    th._PAGE_LIMIT_BOTS, th._MAX_PAGES = 1, 2
    try:
        h = host(script=[FakeResponse([dict(DCA_BOT, id=1)]),
                         FakeResponse([dict(DCA_BOT, id=2)])])
        try:
            h.bots()
            check("an unfinished listing must raise", False)
        except HostError as e:
            check("an unfinished listing raises: an unread tail is not an "
                  "absence", "truncated" in str(e))
    finally:
        th._PAGE_LIMIT_BOTS, th._MAX_PAGES = old_limit, old_pages


def test_a_failing_grid_list_fails_the_whole_read():
    h = host(script=[FakeResponse([DCA_BOT]), FakeResponse(None, status=500)])
    try:
        h.bots()
        check("a half-read bot list must raise", False)
    except HostError:
        check("a half-read bot list raises: a missing family would escape "
              "the exposure maths", True)


# ------------------------------------------------------------------ health

def test_health_never_raises():
    dead = host(script=[requests.exceptions.ConnectionError("no route")])
    hh = dead.health()
    check("transport failure -> unreachable, not an exception",
          hh.reachable is False and hh.authenticated is False)

    denied = host(script=[FakeResponse({"error": "api_key_invalid"},
                                       status=401)])
    hh2 = denied.health()
    check("401 -> reachable but not authenticated",
          hh2.reachable is True and hh2.authenticated is False)

    ok = host(script=[FakeResponse([])])
    hh3 = ok.health()
    check("a readable bots endpoint -> healthy",
          hh3.reachable is True and hh3.authenticated is True)
    check("latency is measured", isinstance(hh3.latency_ms, int))
    check("read_only is reported", hh3.read_only is True)


# ---------------------------------------------------------------- registry

def test_registered_as_a_strategy_host_not_a_venue():
    check("3commas-bots is a registered host kind",
          "3commas-bots" in host_kinds())
    h = build_host("3commas-bots", {"api_key": KEY, "api_secret": SECRET})
    check("build_host builds this adapter", isinstance(h, ThreeCommasBots))
    check("and it satisfies the StrategyHost protocol",
          isinstance(h, StrategyHost))
    check("stop_disposition is declared as orphans (evidence in module "
          "docstring)", h.stop_disposition == STOP_ORPHANS_POSITIONS)
    check("a host has no place_order", not hasattr(h, "place_order"))
    from brokers import kinds
    check("the VENUE registry did not grow a bots kind",
          "3commas-bots" not in kinds())


def test_missing_credentials_are_refused_at_construction():
    for conf in ({}, {"api_key": KEY}, {"api_secret": SECRET}):
        try:
            ThreeCommasBots(conf)
            check("config %r must be refused" % (sorted(conf),), False)
        except HostError:
            check("config without both credentials is refused (%r)"
                  % (sorted(conf),), True)


# ----------------------------------------------------------------- helpers

def test_num_keeps_unknown_unknown():
    check("None stays None", _num(None) is None)
    check("empty string stays None", _num("") is None)
    check("garbage stays None", _num("abc") is None)
    check("NaN is not a number", _num(float("nan")) is None)
    check("inf is not a number", _num(float("inf")) is None)
    check("zero is a real zero", _num("0") == 0.0)
    check("a negative parses", _num("-12.5") == -12.5)


def test_keel_pair_spelling():
    check("USDT_ETH -> ETH/USDT", _keel_pair("USDT_ETH") == "ETH/USDT")
    check("BTC_ETH -> ETH/BTC", _keel_pair("BTC_ETH") == "ETH/BTC")
    check("no underscore passes through", _keel_pair("BTCUSD") == "BTCUSD")
    check("empty stays empty", _keel_pair("") == "")


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
