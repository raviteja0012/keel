"""Cryptohopper strategy-host adapter tests.

The centrepiece is the allowlist battery: Cryptohopper's "manage" scope also
grants delete and config writes, and its buy/sell endpoints size the order
themselves when amount is omitted, so the transport frozenset is the ONLY
thing enforcing "see but never manage". The rest pins the house discipline:
an unreadable state is an error rather than a stopped bot, a missing P&L is
None rather than zero, credentials never surface in any message, and both
published (mutually contradictory) API contracts stay operator-selected —
never auto-switched.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "ch.db"))

import requests                                                     # noqa: E402

from brokers.strategy_host import (STOP_UNKNOWN, HostError,          # noqa: E402
                                   HostReadOnly, StrategyHost, build_host,
                                   host_kinds, hosted_exposure)
from brokers.cryptohopper import (BASE_URL, _CONTRACTS,              # noqa: E402
                                  CryptohopperHost, _enabled_flag, _num)

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


TOKEN = "chtok4f8e2a91c6d05b73a1e9f2c8d4b6a0e51234"
APP_KEY = "appkey-3f19c2e8-secret"


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


def host(script=(), read_only=True, contract="openapi", app_key=None):
    cfg = {"name": "ch", "access_token": TOKEN, "api_contract": contract,
           "read_only": read_only}
    if app_key:
        cfg["app_key"] = app_key
    h = CryptohopperHost(cfg)
    h._session = FakeSession(script)
    h._min_interval = 0.0            # rate-limit test re-enables it
    return h


def env(data, status=200):
    """Source B success envelope."""
    return FakeResponse({"data": data}, status=status)


def err(message, status):
    """Source B ApiErrorBody."""
    return FakeResponse({"status": status, "code": 0, "error": 1,
                         "message": message}, status=status)


RUNNING = {"id": 123, "name": "grid-1", "exchange": "binance", "enabled": 1}
STOPPED = {"id": 123, "name": "grid-1", "exchange": "binance", "enabled": 0}
POS = {"hopper_id": 123, "coin": "BTC", "amount": "0.5", "rate": "43250.12"}

_FORBIDDEN = ("buy", "sell", "order", "panic", "delete", "create", "config")


# -------------------------------------------------------------- allowlist R2

def test_allowlists_exclude_every_trading_and_admin_path():
    for key, c in sorted(_CONTRACTS.items()):
        for method, template in sorted(c.allowlist):
            bad = [w for w in _FORBIDDEN if w in template.lower()]
            check("%s allowlist entry %s %s is read/start/stop only"
                  % (key, method, template), not bad, bad)
        writes = [(m, t) for m, t in c.allowlist if m != "GET"]
        check("%s contract has exactly one write, the enabled flip" % key,
              len(writes) == 1, writes)
        check("%s ops are all covered by the allowlist" % key,
              set(c.ops.values()) <= c.allowlist)


def test_transport_refuses_paths_off_the_allowlist():
    for method, template in [("POST", "/v1/hopper/buy"),
                             ("POST", "/v1/hopper/sell"),
                             ("POST", "/v1/hopper/panic"),
                             ("POST", "/v1/hopper/delete"),
                             ("GET", "/v1/hopper/orders"),
                             ("POST", "/v1/hopper/123/order")]:
        h = host(script=[])
        try:
            h._request(method, template)
            check("%s %s must be refused" % (method, template), False)
        except HostError as e:
            check("%s %s refused before any network I/O"
                  % (method, template),
                  "allowlist" in str(e) and not h._session.sent)


def test_even_an_allowlisted_path_with_the_wrong_method_is_refused():
    h = host(script=[])
    try:
        h._request("POST", "/v1/hopper/list")   # right path, wrong verb
        check("wrong method on an allowlisted path must be refused", False)
    except HostError:
        check("wrong method on an allowlisted path is refused",
              not h._session.sent)


def test_a_crafted_bot_id_cannot_forge_an_off_allowlist_path():
    """BEHAVIOUR, not shape. The allowlist checks the TEMPLATE, but the bot_id
    is substituted in afterward. Earlier tests only fed literal off-allowlist
    templates to _request; none drove a crafted bot_id through the public
    methods, so a bot_id of '1/config' forging PATCH /v1/hopper/1/config went
    uncaught. This drives the real entry points on the legacy contract, where
    ids land in the path, and asserts nothing reaches the wire.

    bot_ids are str(row['id']) from the venue's own response, so this is not a
    far-fetched input: one quirky or compromised API value is enough.
    """
    attacks = ["1/config", "123/../delete", "123/orders", "1/panic",
               "1%2Fconfig", "1 ", "1?x=y", "1#frag"]
    for bad in attacks:
        # read path (bot_state) and write path (start_bot, armed) both go
        # through _request with path_args on the legacy contract.
        for armed in (False, True):
            h = host(script=[], read_only=not armed, contract="legacy")
            fn = (lambda: h.start_bot(bad)) if armed else (lambda: h.bot_state(bad))
            try:
                fn()
                check("crafted bot_id %r (armed=%s) must be refused"
                      % (bad, armed), False, "reached: %s" % h._session.sent)
            except HostError:
                # The point: NOTHING was put on the wire. A refusal that still
                # sent the request would be no protection at all.
                check("crafted bot_id %r (armed=%s) sent nothing to the network"
                      % (bad, armed), not h._session.sent, h._session.sent)


def test_a_legitimate_numeric_bot_id_still_reaches_the_wire():
    # The guard must reject path-forging ids WITHOUT rejecting ordinary ones.
    h = host(script=[env(RUNNING), env([POS])], read_only=True, contract="legacy")
    h.bot_state("12345")
    urls = " ".join(s["url"] for s in h._session.sent)
    check("a plain numeric id reaches the network", h._session.sent, "nothing sent")
    check("with no path traversal in the URL", "/../" not in urls and "12345" in urls,
          urls)


# ------------------------------------------------------------ transport/auth

def test_openapi_contract_sends_bearer_auth():
    h = host(script=[env([RUNNING])])
    h.bots()
    sent = h._session.sent[0]
    check("openapi list path", sent["url"] == BASE_URL + "/v1/hopper/list",
          sent["url"])
    check("Authorization: Bearer <token>",
          sent["headers"].get("Authorization") == "Bearer %s" % TOKEN)
    check("no legacy access-token header",
          "access-token" not in sent["headers"])
    check("no x-api-app-key unless configured",
          "x-api-app-key" not in sent["headers"])


def test_app_key_header_attached_when_configured():
    h = host(script=[env([RUNNING])], app_key=APP_KEY)
    h.bots()
    check("x-api-app-key rides along when configured",
          h._session.sent[0]["headers"].get("x-api-app-key") == APP_KEY)


def test_legacy_contract_sends_access_token_and_rest_paths():
    h = host(script=[env([RUNNING])], contract="legacy")
    h.bots()
    sent = h._session.sent[0]
    check("legacy list path", sent["url"] == BASE_URL + "/v1/hopper",
          sent["url"])
    check("legacy access-token header",
          sent["headers"].get("access-token") == TOKEN)
    check("no Authorization header on legacy",
          "Authorization" not in sent["headers"])


def test_legacy_bot_state_paths_carry_the_id():
    h = host(script=[env(RUNNING), env([POS])], contract="legacy")
    h.bot_state("123")
    urls = [s["url"] for s in h._session.sent]
    check("legacy get path", urls[0] == BASE_URL + "/v1/hopper/123", urls[0])
    check("legacy positions path",
          urls[1] == BASE_URL + "/v1/hopper/123/position", urls[1])


def test_legacy_stop_is_a_patch_field_write():
    h = host(script=[env(RUNNING), env(STOPPED)], contract="legacy",
             read_only=False)
    a = h.stop_bot("123")
    write = h._session.sent[1]
    check("legacy stop uses PATCH /v1/hopper/{id}",
          write["method"] == "PATCH"
          and write["url"] == BASE_URL + "/v1/hopper/123")
    check("legacy stop body is the enabled field write",
          json.loads(write["body"]) == {"enabled": 0})
    check("and it reports changed", a.changed is True)


# -------------------------------------------------------------- secrets (R3)

def test_secret_never_in_a_transport_error():
    h = host(script=[requests.RequestException(
        "connection reset while sending token %s" % TOKEN)])
    try:
        h.bots()
        check("transport failure must raise", False)
    except HostError as e:
        check("the token is scrubbed from a transport error",
              TOKEN not in str(e))
        check("and the scrub leaves a marker", "***" in str(e), str(e))


def test_secret_never_in_an_http_error():
    h = host(script=[err("invalid token %s supplied" % TOKEN, 401)])
    try:
        h.bots()
        check("a 401 must raise", False)
    except HostError as e:
        check("the token is scrubbed from a server-echoed message",
              TOKEN not in str(e))
        check("http_status is carried", e.http_status == 401)


def test_app_key_is_scrubbed_too():
    h = host(script=[err("bad app key %s" % APP_KEY, 403)], app_key=APP_KEY)
    try:
        h.bots()
        check("a 403 must raise", False)
    except HostError as e:
        check("the app key is scrubbed as well", APP_KEY not in str(e))


def test_repr_carries_no_credentials():
    h = host(app_key=APP_KEY)
    check("repr has no token", TOKEN not in repr(h), repr(h))
    check("repr has no app key", APP_KEY not in repr(h))
    check("repr still identifies the host",
          "ch" in repr(h) and "openapi" in repr(h))


def test_health_detail_carries_no_credentials():
    h = host(script=[err("token %s rejected" % TOKEN, 401)])
    hh = h.health()
    check("health detail has no token", TOKEN not in hh.detail, hh.detail)


# --------------------------------------------------------------- rate limit

def test_default_min_interval_is_the_conservative_documented_bucket():
    h = CryptohopperHost({"name": "ch", "access_token": TOKEN})
    # Source B: 30 req/min. Source A: 2 req/s. The slower claim wins.
    check("default min interval is 2.0s (30/min bucket)",
          h._min_interval == 2.0, h._min_interval)


def test_rate_limit_waits_between_requests():
    h = host(script=[env([RUNNING]), env([RUNNING])])
    h._min_interval = 2.0
    clock = {"t": 1000.0}
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s

    h._now = lambda: clock["t"]
    h._sleep = fake_sleep
    h.bots()
    h.bots()
    check("second request waits out the min interval",
          len(sleeps) == 1 and abs(sleeps[0] - 2.0) < 1e-9, sleeps)


# ------------------------------------------------------------------- health

def test_health_never_raises_on_transport_failure():
    h = host(script=[requests.RequestException("dns")])
    hh = h.health()
    check("a dead host is a health result, not an exception",
          hh.reachable is False and hh.authenticated is False)


def test_health_401_names_the_other_contract_but_never_switches():
    h = host(script=[err("Unauthorized", 401)])
    hh = h.health()
    check("401 -> reachable but not authenticated",
          hh.reachable is True and hh.authenticated is False)
    check("detail points the operator at the legacy contract",
          "legacy" in hh.detail and "api_contract" in hh.detail, hh.detail)
    check("the contract was NOT auto-switched",
          h._contract.key == "openapi")
    check("and no legacy path was probed with the credential",
          [s["url"] for s in h._session.sent]
          == [BASE_URL + "/v1/hopper/list"])


def test_health_ok():
    h = host(script=[env([RUNNING, dict(RUNNING, id=7)])])
    hh = h.health()
    check("healthy host is reachable and authenticated",
          hh.reachable is True and hh.authenticated is True)
    check("latency is measured", isinstance(hh.latency_ms, int))
    check("read_only is passed through", hh.read_only is True)
    check("detail counts the visible hoppers", "2 hopper" in hh.detail,
          hh.detail)


# --------------------------------------------------------------------- reads

def test_bots_parse_and_enabled_spellings():
    rows = [RUNNING, {"id": 7, "name": "dca", "enabled": False},
            {"id": "8", "name": "sig", "enabled": "1"}]
    h = host(script=[env(rows)])
    bots = h.bots()
    check("three bots parsed", len(bots) == 3)
    check("int 1 -> running", bots[0].running is True)
    check("bool False -> stopped", bots[1].running is False)
    check("string '1' -> running (PHP backend spelling)",
          bots[2].running is True)
    check("ids are strings", [b.bot_id for b in bots] == ["123", "7", "8"])
    check("host name is carried", bots[0].host == "ch")
    check("raw row preserved", bots[0].raw is rows[0])


def test_missing_enabled_is_an_error_not_a_stopped_bot():
    # R1: reporting running=False for a state we could not read hides live
    # money from the kill switches.
    h = host(script=[env([{"id": 123, "name": "grid-1"}])])
    try:
        h.bots()
        check("a missing enabled flag must raise", False)
    except HostError as e:
        check("missing enabled raises rather than reading as stopped",
              "unreadable state is not a stopped bot" in str(e))


def test_a_hopper_row_without_an_id_is_refused():
    h = host(script=[env([{"name": "ghost", "enabled": 1}])])
    try:
        h.bots()
        check("a row without an id must raise", False)
    except HostError as e:
        check("a bot Keel cannot address is refused", "without an" in str(e))


def test_a_non_list_hopper_payload_is_an_error_not_an_empty_account():
    h = host(script=[env({"weird": 1})])
    try:
        h.bots()
        check("a malformed listing must raise", False)
    except HostError as e:
        check("malformed listing raises instead of reading as no bots",
              "expected a list" in str(e))


def test_bot_state_reads_positions():
    h = host(script=[env(RUNNING), env([POS])])
    s = h.bot_state("123")
    check("running flag read from the hopper", s.running is True)
    check("one position parsed", len(s.positions) == 1)
    p = s.positions[0]
    check("coin -> symbol", p.symbol == "BTC")
    check("amount -> qty", p.qty == 0.5)
    check("rate -> entry price", p.entry_price == 43250.12)
    check("hoppers hold spot long", p.side == "buy")


def test_pnl_is_none_and_the_bot_counts_as_unvalued():
    # R1: neither contract documents a P&L field, so the answer is None —
    # and hosted_exposure must count that as unvalued, not as flat.
    h = host(script=[env(RUNNING), env([POS])])
    s = h.bot_state("123")
    check("unrealized_pnl is None, never 0", s.unrealized_pnl is None)
    check("realized_pnl is None too", s.realized_pnl is None)
    check("valued is False", s.valued is False)
    exp = hosted_exposure([s])
    check("hosted_exposure counts it unvalued", exp.unvalued_bots == 1)
    check("and the total is not trustworthy", exp.trustworthy is False)
    check("but the open positions still count toward exposure",
          exp.open_positions == 1)


def test_an_unreadable_position_row_is_refused_not_dropped():
    h = host(script=[env(RUNNING),
                     env([{"coin": "BTC", "amount": "garbage",
                           "rate": "1"}])])
    try:
        h.bot_state("123")
        check("an unsizeable position must raise", False)
    except HostError as e:
        check("a partial book is refused rather than under-reported",
              "partial book" in str(e))


def test_a_position_read_failure_propagates():
    # R1: a state we could not fetch raises; it does not come back empty.
    h = host(script=[env(RUNNING), err("boom", 500)])
    try:
        h.bot_state("123")
        check("an unreadable position list must raise", False)
    except HostError as e:
        check("position read failure raises HostError",
              e.http_status == 500)
        check("and 500 is marked retryable", e.retryable is True)


# --------------------------------------------------------------- bot control

def test_read_only_refuses_bot_control_before_any_network():
    h = host(script=[])                      # read_only=True is the default
    for fn, label in ((h.start_bot, "start"), (h.stop_bot, "stop")):
        try:
            fn("123")
            check("read-only must refuse %s" % label, False)
        except HostReadOnly:
            check("read-only refuses %s before any request" % label, True)
    check("and nothing was sent", not h._session.sent)


def test_read_only_is_the_default():
    h = CryptohopperHost({"name": "ch", "access_token": TOKEN})
    check("a fresh host is read-only until armed", h.read_only is True)


def test_reading_is_allowed_while_read_only():
    h = host(script=[env([RUNNING])])        # read_only=True
    check("bots() works on a read-only host", len(h.bots()) == 1)


def test_start_flips_enabled_and_reports_changed():
    h = host(script=[env(STOPPED), env(RUNNING)], read_only=False)
    a = h.start_bot("123")
    check("start reports changed", a.changed is True and a.running is True)
    write = h._session.sent[1]
    check("write goes to the update endpoint",
          write["method"] == "POST"
          and write["url"] == BASE_URL + "/v1/hopper/update")
    check("body is the documented field write",
          json.loads(write["body"]) == {"hopper_id": "123", "enabled": 1})


def test_start_on_a_running_bot_is_a_noop_success():
    # R5: requesting the state a bot is already in is success, not an error.
    h = host(script=[env(RUNNING)], read_only=False)
    a = h.start_bot("123")
    check("already-running start returns changed=False",
          a.changed is False and a.running is True)
    check("and no write was sent",
          len(h._session.sent) == 1 and h._session.sent[0]["method"] == "GET")


def test_stop_flips_enabled():
    h = host(script=[env(RUNNING), env(STOPPED)], read_only=False)
    a = h.stop_bot("123")
    check("stop reports changed", a.changed is True and a.running is False)
    check("stop writes enabled=0",
          json.loads(h._session.sent[1]["body"])
          == {"hopper_id": "123", "enabled": 0})


def test_stop_on_a_stopped_bot_is_a_noop_success():
    h = host(script=[env(STOPPED)], read_only=False)
    a = h.stop_bot("123")
    check("already-stopped stop returns changed=False",
          a.changed is False and a.running is False)
    check("and no write was sent", len(h._session.sent) == 1)


def test_an_unconfirming_update_response_triggers_one_reread():
    # Source B types every Hopper field optional, so {success: true} is a
    # legal update response; the adapter re-reads rather than assuming.
    h = host(script=[env(STOPPED), env({"success": True}), env(RUNNING)],
             read_only=False)
    a = h.start_bot("123")
    check("re-read confirms and the action succeeds", a.changed is True)
    check("exactly three requests: read, write, re-read",
          len(h._session.sent) == 3)


def test_a_write_that_does_not_take_raises():
    # Host still reports enabled=1 after we wrote enabled=0.
    h = host(script=[env(RUNNING), env(RUNNING)], read_only=False)
    try:
        h.stop_bot("123")
        check("a write the host contradicts must raise", False)
    except HostError as e:
        check("unconfirmed stop raises rather than reporting success",
              "still running" in str(e))


def test_a_write_that_cannot_be_verified_raises():
    h = host(script=[env(STOPPED), env({"success": True}),
                     env({"id": 123, "name": "grid-1"})],   # no enabled
             read_only=False)
    try:
        h.start_bot("123")
        check("an unverifiable write must raise", False)
    except HostError as e:
        check("unverifiable write raises with the state marked unknown",
              "unknown" in str(e))


def test_an_unreadable_current_state_refuses_a_blind_write():
    h = host(script=[env({"id": 123, "name": "g", "enabled": "maybe"})],
             read_only=False)
    try:
        h.start_bot("123")
        check("an unreadable current state must refuse the write", False)
    except HostError as e:
        check("blind write refused", "blind write" in str(e))
    check("and only the read was sent",
          [s["method"] for s in h._session.sent] == ["GET"])


# -------------------------------------------------------------- declarations

def test_stop_disposition_is_declared_unknown():
    # R6: neither source states what disabling does to open positions, so
    # the honest declaration is UNKNOWN (callers treat it as orphans).
    check("stop_disposition is STOP_UNKNOWN",
          CryptohopperHost.stop_disposition == STOP_UNKNOWN)


def test_num_keeps_unknown_unknown():
    check("None stays None", _num(None) is None)
    check("empty string stays None", _num("") is None)
    check("garbage stays None", _num("abc") is None)
    check("NaN is not a number", _num(float("nan")) is None)
    check("inf is not a number", _num(float("inf")) is None)
    check("zero is a real zero", _num("0") == 0.0)


def test_enabled_flag_never_guesses():
    check("2 is not a truthy running flag", _enabled_flag(2) is None)
    check("'yes' is unreadable, not True", _enabled_flag("yes") is None)
    check("None is unreadable", _enabled_flag(None) is None)
    check("'0' reads stopped", _enabled_flag("0") is False)


# ---------------------------------------------------------- registry/config

def test_registered_as_a_strategy_host():
    check("kind is registered", "cryptohopper" in host_kinds())
    h = build_host("cryptohopper", {"name": "ch", "access_token": TOKEN})
    check("build_host returns the adapter", isinstance(h, CryptohopperHost))
    check("and it satisfies the StrategyHost protocol",
          isinstance(h, StrategyHost))


def test_config_validation():
    try:
        CryptohopperHost({"name": "ch"})
        check("a missing access_token must raise", False)
    except HostError as e:
        check("missing access_token raises", "access_token" in str(e))
    try:
        CryptohopperHost({"name": "ch", "access_token": TOKEN,
                          "api_contract": "wat"})
        check("an unknown api_contract must raise", False)
    except HostError as e:
        check("unknown api_contract names the valid ones",
              "legacy" in str(e) and "openapi" in str(e))


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
