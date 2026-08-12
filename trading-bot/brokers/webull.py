"""Webull OpenAPI venue adapter — US equities, options, futures and crypto.

STATUS: WRITTEN FROM THE PUBLISHED DOCS, NEVER RUN AGAINST A LIVE ACCOUNT.

Webull OpenAPI access requires an application and a 1-2 business day review
(developer.webull.com/apis/docs/getting-started). No application has been made
for this deployment, so not one line below has been confirmed against a real
response. That fact is load-bearing, not a footnote, so the adapter is built to
make it visible rather than to look finished:

  * health() reports "no credentials" as a health RESULT, never an exception,
    so an unconfigured Webull venue shows up honestly on the dashboard instead
    of taking a code path nobody has exercised.
  * symbol_meta() refuses. The docs list instrument-profile endpoints but do
    not publish the field carrying tick size, lot step or min notional. A
    guessed 0.01 tick is wrong for sub-dollar equities and for crypto, and a
    wrong lot step is a real-money error.
  * place_order() probes for the client_order_id BEFORE every submit and again
    after any transport failure. Webull documents client_order_id as unique and
    non-repeating but does not document what it does with a duplicate, so this
    adapter does not rely on the venue to reject one.
  * that probe has THREE outcomes, not two. found / absent / indeterminate. A
    probe that could not reach the venue, could not authenticate, or came back
    describing some other order is indeterminate, and place_order refuses on
    it. "I could not determine whether this order exists" is not "it does not
    exist"; collapsing the two turns one intent into two fills.
  * cancel() returns True only when the response CONFIRMS the cancel. Webull
    can answer HTTP 200 with a body that says the cancel was refused.
  * every number crossing this boundary is checked for finiteness. NaN and
    Infinity satisfy no comparison, so they walk straight through guards
    written as `x <= 0` and land in sizing.
  * Anything the docs leave ambiguous is marked AMBIGUOUS in a comment and
    fails closed. Nothing here invents a field name to look complete.

Endpoints used (US region, all verbatim from developer.webull.com):
  GET    /trading/accounts/list              accounts under these credentials
  GET    /trading/assets/balances/get        balance, buying power, cash
  GET    /trading/assets/positions/list      current holdings
  GET    /trading/orders/get                 order detail by order_id OR
                                             client_order_id -- the idempotency probe
  POST   /trading/orders/place               submit
  POST   /trading/orders/cancel              cancel (see _CANCEL_METHOD below)

Auth is a signed-header scheme, not a bearer token. Per
developer.webull.com/apis/docs/authentication/signature the request carries
x-app-key, x-timestamp, x-signature-nonce, x-signature-algorithm (HMAC-SHA1),
x-signature-version (1.0) and x-signature; the app secret is never transmitted.
The signature covers the path, the query parameters, an MD5 of the body and the
signing headers themselves, so a replayed or edited request does not verify.
"""
import base64
import calendar
import hashlib
import hmac
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import (Any, Dict, Iterator, List, NamedTuple, Optional, Sequence,
                    Tuple)

from . import (Balance, Order, OrderResult, Position, SymbolMeta, Tick,
               VenueError, VenueHealth, VenueReadOnly, register)

# developer.webull.com/apis/docs/AI-friendly-Resources/skills lists the hosts.
HOST_PROD = "api.webull.com"
HOST_UAT = "us-openapi-alb.uat.webullbroker.com"

_SIGNED_HEADERS = ("x-app-key", "x-signature-algorithm", "x-signature-version",
                   "x-signature-nonce", "x-timestamp", "host")
_ALGORITHM = "HMAC-SHA1"
_SIGNATURE_VERSION = "1.0"

# AMBIGUOUS: the reference index lists Cancel Order as DELETE, the endpoint page
# for the same call says POST. Defaulting to the more specific page and leaving
# it overridable, because guessing wrong here fails loudly on the first cancel
# rather than quietly, and a cancel that silently no-ops leaves a live position.
_CANCEL_METHOD = "POST"

# The idempotency probe has exactly three answers and "indeterminate" is not a
# spelling of "absent". brokers/__init__.py makes idempotency mandatory, and the
# only thing that delivers it is refusing to submit when we cannot prove the
# order is not already there.
PROBE_FOUND = "found"
PROBE_ABSENT = "absent"
PROBE_INDETERMINATE = "indeterminate"


class _Probe(NamedTuple):
    outcome: str                       # PROBE_FOUND | PROBE_ABSENT | PROBE_INDETERMINATE
    order: Optional[Dict[str, Any]]    # only ever set for PROBE_FOUND
    reason: str                        # why, in words, for the refusal message
    retryable: bool = False            # is the *cause* of indeterminacy transient


# AMBIGUOUS: the docs do not say what /trading/orders/get returns for an unknown
# client_order_id. 404 is the only status this adapter reads as a definite "no
# such order". Note what is deliberately NOT here: 417, which Webull also uses
# for ordinary business rejections, so it cannot carry that meaning alone.
_DEFAULT_NOT_FOUND_HTTP: Tuple[int, ...] = (404,)

# An operator may extend that set once a real response has been observed, but
# only with statuses that could mean "no such order". A transport failure (429,
# 5xx) or an auth failure (401/403) says nothing whatsoever about whether the
# order exists, and letting one be configured as "absent" would re-open the hole
# from the other side — through config instead of through code.
_ALLOWED_NOT_FOUND_HTTP: Tuple[int, ...] = (400, 404, 410, 417, 422)

# Cancel-confirmation vocabulary. Webull's cancel response schema is not
# published, so read every marker that could carry a verdict and treat the
# absence of all of them as "not confirmed" rather than as success.
_CANCEL_FLAG_KEYS = ("success", "ok", "cancelled", "canceled", "is_cancelled")
_TRUE_WORDS = frozenset(("true", "1", "success", "succeeded", "ok", "yes",
                         "cancelled", "canceled"))
_FALSE_WORDS = frozenset(("false", "0", "fail", "failed", "failure", "no",
                          "error", "rejected"))

# Webull caps client_order_id at 32 characters. This is NOT a formatting detail:
# truncating a longer id would map two distinct orders onto one idempotency key,
# and the second one would be mistaken for a retry of the first.
_MAX_CLIENT_ORDER_ID = 32

# Webull's own order states, mapped onto the contract's vocabulary. Anything not
# listed becomes "unknown", which the engine treats as needing reconciliation --
# the right answer for a state this adapter has never seen.
_STATUS_MAP = {
    "FILLED": "filled",
    "PARTIAL_FILLED": "partial",
    "PARTIALLY_FILLED": "partial",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
    "PENDING_CANCEL": "cancelled",
    "FAILED": "rejected",
    "REJECTED": "rejected",
    "PENDING": "accepted",
    "PENDING_SUBMIT": "accepted",
    "QUEUED": "accepted",
    "SUBMITTED": "accepted",
    "WORKING": "accepted",
}

# AMBIGUOUS: Webull publishes the balance and position endpoints but not their
# response schemas. These are candidate field names drawn from the regional API
# docs. Every reader below RAISES when none of them match rather than returning
# a zero -- a silent zero balance mis-sizes the next order and a silent empty
# position list reads as "flat" to reconciliation.
_POS_QTY_KEYS = ("quantity", "qty", "position", "holding_quantity")
_POS_COST_KEYS = ("cost_price", "avg_cost", "average_cost", "cost", "open_price")
_POS_PNL_KEYS = ("unrealized_profit_loss", "unrealized_pnl", "unrealizedProfitLoss")
_POS_SIDE_KEYS = ("side", "position_side", "direction")
_POS_SYMBOL_KEYS = ("symbol", "ticker", "instrument_symbol")
_BAL_TOTAL_KEYS = ("total_amount", "net_liquidation_value", "total_market_value",
                   "total_cash_value", "amount")
_BAL_FREE_KEYS = ("cash_balance", "available_amount", "settled_funds",
                  "buying_power", "available_cash")


def _num(row: Dict[str, Any], keys: Sequence[str], where: str = "",
         venue: str = "") -> Optional[float]:
    """First key present and FINITE, or None. Never a default: the caller has
    to decide whether a missing field is survivable, and mostly it is not.

    A value that parses as NaN or Infinity raises instead of being returned.
    float() accepts both, and neither satisfies any comparison — `nan <= 0` is
    False, `nan > 0` is False — so a non-finite number walks through every
    guard downstream that is written as a comparison, including the `v <= 0`
    check on tick size and lot step a few lines below. Returning None for one
    would be no better: it would be laundered into "field missing", and the
    callers that write `_num(...) or 0.0` would turn a corrupt increment into a
    confident zero. Raise where the corruption is visible.
    """
    for k in keys:
        v = row.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f):
            raise VenueError(
                "Webull %s field %r carries %r, which is not a finite number. "
                "NaN and Infinity pass every comparison-based guard in the "
                "engine, so this value is refused here rather than propagated "
                "into sizing." % (where or "numeric", k, v), venue=venue)
        return f
    return None


def _decimal_str(x: float) -> str:
    """Webull takes quantities and prices as strings. Exponent notation is what
    a naive str() produces for a small crypto quantity and it is not a format
    the docs offer, so render plain decimal and trim.

    Non-finite input is refused rather than formatted: "%.8f" % float("nan")
    is the literal text "nan", which would be transmitted as a quantity.
    """
    v = float(x)
    if not math.isfinite(v):
        raise VenueError(
            "refusing to send %r to Webull as a number: it is not finite and "
            "would be transmitted as the literal text %r" % (x, "%.8f" % v))
    s = ("%.8f" % v).rstrip("0").rstrip(".")
    return s or "0"


def _truthy(v: Any) -> Optional[bool]:
    """True / False / None for "the venue did not say". None is the important
    one: an unrecognised marker must not be read as consent."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return bool(v)
    s = str(v).strip().lower()
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    return None


def _iso_timestamp(now: Optional[float] = None) -> str:
    """x-timestamp is documented as ISO 8601, UTC only, YYYY-MM-DDThh:mm:ssZ."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))


class WebullVenue:
    """One configured Webull OpenAPI account."""

    def __init__(self, config: Dict[str, Any]):
        self.name: str = config.get("name", "webull")
        self.read_only: bool = bool(config.get("read_only", True))
        self._key: str = config.get("api_key") or ""
        self._secret: str = config.get("api_secret") or ""
        self._account_id: Optional[str] = config.get("account_id")
        self._host: str = config.get("host") or (
            HOST_UAT if config.get("sandbox") else HOST_PROD)
        self._timeout = int(config.get("timeout_ms", 15000)) / 1000.0

        # A Webull account trades several instrument types through one API, but
        # the engine's Order carries no asset class. Rather than infer one from
        # the symbol -- a decision, and not the adapter's to make -- each venue
        # entry is scoped to a single instrument_type by the operator.
        self._instrument_type: Optional[str] = config.get("instrument_type")
        self._market: str = config.get("market", "US")
        # AMBIGUOUS: the US stock example sends support_trading_session "CORE",
        # an older regional doc sends "N". CORE (regular hours) is the
        # conservative reading: the engine models neither extended-hours
        # liquidity nor its spreads.
        self._session: str = config.get("support_trading_session", "CORE")
        # The contract's Order has no time-in-force. DAY is the safe default:
        # an order that expires at the close beats one that rests unmanaged.
        self._tif: str = config.get("time_in_force", "DAY")
        # Selling equity you do not hold is a short, with borrow and margin
        # consequences the engine does not model. Off unless the operator says so.
        self._allow_short: bool = bool(config.get("allow_short", False))
        # Set once the instrument-profile response has actually been seen and
        # its tick/lot fields mapped. Until then symbol_meta refuses.
        self._instrument_path: Optional[str] = config.get("instrument_path")
        self._instrument_fields: Dict[str, str] = config.get("instrument_fields") or {}
        self._clock_skew_s: Optional[float] = None
        self._not_found_http: Tuple[int, ...] = self._read_not_found_http(
            config.get("order_not_found_http"))

    def _read_not_found_http(self, raw: Any) -> Tuple[int, ...]:
        """Which HTTP statuses this venue is allowed to read as "no such order".

        Validated at construction, not at probe time: a venue whose config
        would make a 503 mean "absent" must never get as far as placing an
        order, because that is the double-fill hole wearing a config file.
        """
        if raw is None or raw == "":
            return _DEFAULT_NOT_FOUND_HTTP
        if isinstance(raw, (int, str)):
            raw = [raw]
        out: List[int] = []
        for s in raw:
            try:
                code = int(s)
            except (TypeError, ValueError):
                raise VenueError("order_not_found_http takes HTTP status codes, "
                                 "got %r" % (s,), venue=self.name)
            if code not in _ALLOWED_NOT_FOUND_HTTP:
                raise VenueError(
                    "HTTP %d may not be configured to mean 'this order does not "
                    "exist' (allowed: %s). A transport or auth failure read as "
                    "absence is exactly how a retry becomes a second fill."
                    % (code, ", ".join(str(c) for c in _ALLOWED_NOT_FOUND_HTTP)),
                    venue=self.name)
            out.append(code)
        return tuple(sorted(set(out)))

    # ------------------------------------------------------------- internals
    def _sign(self, path: str, params: Dict[str, str], body: str,
              headers: Dict[str, str]) -> str:
        """Signature per developer.webull.com/apis/docs/authentication/signature.

        Merge query params with the signing headers, sort by name, join as
        k=v&k=v, append uppercase MD5 of the body when there is one, prefix the
        path, URL-encode the lot, and HMAC-SHA1 it with the secret plus '&'.
        """
        merged: Dict[str, str] = {k: str(v) for k, v in params.items()}
        merged.update({h: headers[h] for h in _SIGNED_HEADERS if h in headers})
        str1 = "&".join("%s=%s" % (k, merged[k]) for k in sorted(merged))
        str3 = path + "&" + str1
        if body:
            str3 += "&" + hashlib.md5(body.encode()).hexdigest().upper()
        # AMBIGUOUS: the docs say "URL-encode str3" without naming the reserved
        # set. Encoding everything non-unreserved is the strict reading; it
        # cannot be verified without an approved application.
        encoded = urllib.parse.quote(str3, safe="")
        mac = hmac.new((self._secret + "&").encode(), encoded.encode(),
                       hashlib.sha1)
        return base64.b64encode(mac.digest()).decode()

    def _transport(self, method: str, url: str, headers: Dict[str, str],
                   body: str) -> Tuple[str, Dict[str, str]]:
        """The only place this module touches the network. Split out so the
        tests can exercise signing, parsing and the idempotency path offline."""
        req = urllib.request.Request(url, method=method,
                                     data=body.encode() if body else None,
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return r.read().decode(), dict(r.headers)

    def _call(self, method: str, path: str, params: Optional[Dict] = None,
              body: Optional[Dict] = None) -> Any:
        if not (self._key and self._secret):
            raise VenueError("Webull app key/secret not configured",
                             venue=self.name)
        params = {k: str(v) for k, v in (params or {}).items() if v is not None}
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""

        headers = {
            "host": self._host,
            "x-app-key": self._key,
            "x-signature-algorithm": _ALGORITHM,
            "x-signature-version": _SIGNATURE_VERSION,
            "x-signature-nonce": uuid.uuid4().hex,
            "x-timestamp": _iso_timestamp(),
            "x-version": "1.0",
            "Content-Type": "application/json",
        }
        headers["x-signature"] = self._sign(path, params, payload, headers)

        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        url = "https://%s%s%s" % (self._host, path, qs)
        try:
            raw, resp_headers = self._transport(method, url, headers, payload)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = self._redact(e.read().decode()[:300])
            except Exception:
                pass
            # 401 is bad credentials and 417 is a business rejection: retrying
            # either just repeats the same answer. 429/5xx may be transient.
            # http_status is carried so the idempotency probe can tell a
            # definite "no such order" from "I could not find out".
            raise VenueError("Webull HTTP %d: %s" % (e.code, detail),
                             retryable=e.code in (429, 500, 502, 503, 504),
                             venue=self.name, cause=e, http_status=e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise VenueError("Webull unreachable: %s" % e, retryable=True,
                             venue=self.name, cause=e)

        self._note_skew(resp_headers)
        return self._unwrap(raw)

    def _redact(self, text: str) -> str:
        """Venue error bodies echo the request on some surfaces, and this text
        travels straight into a VenueError message, then into a log line, a
        notification and a persisted reason string. Credentials live in the
        runtime DB; an error path must not be the thing that copies them out."""
        for secret in (self._secret, self._key):
            if secret and secret in text:
                text = text.replace(secret, "<redacted>")
        return text

    def _note_skew(self, resp_headers: Dict[str, str]) -> None:
        """Webull publishes no server-time endpoint, but every response carries
        a Date header. Signed requests are timestamped, so a drifting clock is
        an auth failure waiting to happen and worth surfacing in health()."""
        date = resp_headers.get("Date") or resp_headers.get("date")
        if not date:
            return
        try:
            # timegm, not mktime: the header is GMT and mktime would read it as
            # local, turning a healthy clock into a whole-timezone "skew".
            server = calendar.timegm(
                time.strptime(date, "%a, %d %b %Y %H:%M:%S GMT"))
            self._clock_skew_s = round(server - time.time(), 3)
        except (ValueError, OverflowError):
            pass

    def _unwrap(self, raw: str) -> Any:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise VenueError("Webull returned non-JSON: %s"
                             % self._redact(raw[:200]),
                             venue=self.name, cause=e)
        # AMBIGUOUS: the docs signal failure with HTTP status codes and do not
        # publish an envelope. Some Webull surfaces still wrap in {code,msg,data},
        # so unwrap only when a code is actually present, and treat an unfamiliar
        # code as a failure rather than reading through it.
        if isinstance(data, dict) and "code" in data:
            code = str(data.get("code"))
            if code not in ("200", "0", "OK", "success"):
                # An envelope code is the venue saying the same thing it would
                # have said in the status line, so carry it the same way — a
                # numeric one is classified by the probe exactly like an HTTP
                # status, and an unfamiliar one classifies as indeterminate.
                raise VenueError("Webull error %s: %s"
                                 % (code, self._redact(str(data.get("msg"))[:200])),
                                 venue=self.name,
                                 http_status=int(code) if code.isdigit() else None)
            return data.get("data", data)
        return data

    def _require_account(self) -> str:
        if self._account_id:
            return str(self._account_id)
        accts = self.accounts()
        if not accts:
            raise VenueError("no Webull accounts returned for these credentials",
                             venue=self.name)
        if len(accts) > 1:
            # Picking one would be the adapter choosing which account to trade.
            raise VenueError(
                "%d Webull accounts available; set account_id on the venue"
                % len(accts), venue=self.name)
        self._account_id = str(accts[0].get("account_id") or accts[0].get("id") or "")
        if not self._account_id:
            raise VenueError("Webull account list carried no account_id",
                             venue=self.name)
        return self._account_id

    @staticmethod
    def _rows(payload: Any, *keys: str) -> List[Dict[str, Any]]:
        """Webull sometimes returns a bare list and sometimes wraps it. Accept
        both shapes; anything else is the caller's problem to report."""
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for k in keys:
                v = payload.get(k)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, dict)]
        return []

    # ---------------------------------------------------------------- reads
    def health(self) -> VenueHealth:
        """Never raises. No credentials is the expected state here until the
        OpenAPI application is approved, and it has to read as a plain fact on
        the dashboard rather than as a stack trace or as a healthy venue."""
        if not (self._key and self._secret):
            return VenueHealth(
                self.name, reachable=False, authenticated=False,
                read_only=self.read_only,
                detail="no Webull app key/secret configured — OpenAPI access "
                       "requires an approved application (1-2 business day "
                       "review); this venue cannot trade or report")

        t0 = time.time()
        try:
            accts = self.accounts()
            latency = int((time.time() - t0) * 1000)
        except VenueError as e:
            return VenueHealth(self.name, reachable="unreachable" not in str(e),
                               authenticated=False, read_only=self.read_only,
                               detail=str(e)[:300])

        detail = "%d account(s)" % len(accts)
        if not self._account_id and len(accts) != 1:
            detail += "; set account_id to pick one"
        bals: List[Balance] = []
        try:
            bals = self.balances()
        except VenueError as e:
            detail += " | balances: " + str(e)[:200]

        return VenueHealth(self.name, reachable=True, authenticated=True,
                           read_only=self.read_only, latency_ms=latency,
                           clock_skew_s=self._clock_skew_s,
                           detail=detail, balances=bals)

    def accounts(self) -> List[Dict[str, Any]]:
        return self._rows(self._call("GET", "/trading/accounts/list"),
                          "accounts", "account_list", "data")

    def symbol_meta(self, symbol: str) -> SymbolMeta:
        """Refuses by default, and that is the correct behaviour.

        Webull publishes instrument-profile endpoints but not the field names
        carrying tick size, lot step or min notional, and no live response has
        been seen. Assuming a 0.01 tick is wrong below $1.00 on US equities and
        wrong for every crypto pair; assuming a lot step of 1 is wrong for the
        fractional-share orders Webull explicitly supports. Once the profile
        response has been observed, set instrument_path and instrument_fields on
        the venue and this maps it -- still raising on any field that is absent.
        """
        if not self._instrument_path or not self._instrument_fields:
            raise VenueError(
                "Webull instrument metadata is unverified: the docs do not "
                "publish which field carries tick size / lot step / min "
                "notional. Configure instrument_path and instrument_fields "
                "from an observed response before arming execution; this "
                "adapter will not guess an instrument's increments.",
                venue=self.name)

        rows = self._rows(self._call("GET", self._instrument_path,
                                     {"symbols": symbol, "category": self._market}),
                          "instruments", "data")
        if not rows:
            raise VenueError("Webull returned no instrument profile for %r"
                             % symbol, venue=self.name)
        row, f = rows[0], self._instrument_fields

        def _need(key: str) -> float:
            field = f.get(key)
            # _num raises on a non-finite value rather than returning it: the
            # `v <= 0` below is False for NaN and False for Infinity, so this
            # guard alone would have accepted both as a valid increment.
            v = _num(row, (field,), "instrument profile %s for %s" % (key, symbol),
                     self.name) if field else None
            if v is None or v <= 0:
                raise VenueError(
                    "Webull instrument profile for %s has no usable %s "
                    "(mapped to field %r); refusing to guess it"
                    % (symbol, key, field), venue=self.name)
            return v

        # min_notional is genuinely optional on US equities, so a missing one is
        # 0.0 rather than a refusal -- unlike the three above, a zero here
        # cannot produce a wrongly-sized order, only a rejected one.
        min_notional = 0.0
        if f.get("min_notional"):
            min_notional = _num(row, (f["min_notional"],),
                                "instrument profile min_notional for %s" % symbol,
                                self.name) or 0.0

        return SymbolMeta(
            symbol=symbol,
            venue_symbol=str(row.get("symbol") or symbol),
            asset_class=(self._instrument_type or "unknown").lower(),
            tick_size=_need("tick_size"),
            lot_step=_need("lot_step"),
            min_qty=_need("min_qty"),
            min_notional=min_notional)

    def balances(self) -> List[Balance]:
        acct = self._require_account()
        payload = self._call("GET", "/trading/assets/balances/get",
                             {"account_id": acct})
        rows = self._rows(payload, "account_currency_assets", "currency_assets",
                          "assets", "balances")
        if not rows and isinstance(payload, dict):
            rows = [payload]           # single-currency accounts come back flat
        out: List[Balance] = []
        for row in rows:
            cur = row.get("currency") or row.get("currency_code")
            total = _num(row, _BAL_TOTAL_KEYS, "balance total", self.name)
            if not cur or total is None:
                continue
            free = _num(row, _BAL_FREE_KEYS, "balance free", self.name)
            out.append(Balance(str(cur), total, total if free is None else free))
        if not out:
            # Reporting zero equity would silently mis-size the next order.
            raise VenueError(
                "Webull balance response carried no recognised currency row "
                "(top-level keys: %s); the response schema is undocumented and "
                "must be mapped before this venue is trusted for sizing"
                % ", ".join(sorted(payload)[:8] if isinstance(payload, dict)
                            else ["<list>"]),
                venue=self.name)
        return sorted(out, key=lambda b: -b.total)

    def positions(self) -> List[Position]:
        acct = self._require_account()
        rows = self._rows(self._call("GET", "/trading/assets/positions/list",
                                     {"account_id": acct}),
                          "positions", "items", "data")
        out: List[Position] = []
        for row in rows:
            qty = _num(row, _POS_QTY_KEYS, "position quantity", self.name)
            if qty is None:
                # An unreadable holding must not be dropped: reconciliation
                # would then see the engine flat on a position that is open.
                raise VenueError(
                    "Webull position row has no recognised quantity field "
                    "(keys: %s)" % ", ".join(sorted(row)[:10]), venue=self.name)
            if qty == 0:
                continue
            side = str(row.get(next((k for k in _POS_SIDE_KEYS if row.get(k)), ""),
                                "") or ("sell" if qty < 0 else "buy")).lower()
            side = {"long": "buy", "short": "sell"}.get(side, side)
            symbol = next((str(row[k]) for k in _POS_SYMBOL_KEYS if row.get(k)), "")
            out.append(Position(
                symbol=symbol, side=side, qty=abs(qty),
                entry_price=_num(row, _POS_COST_KEYS,
                                 "position cost price", self.name) or 0.0,
                unrealized_pnl=_num(row, _POS_PNL_KEYS,
                                    "position unrealized pnl", self.name) or 0.0,
                venue_id=str(row.get("position_id") or row.get("instrument_id") or ""),
                raw=row))
        return out

    # --------------------------------------------------------------- writes
    def _probe_client_id(self, client_order_id: str, account_id: str) -> _Probe:
        """The idempotency probe, and the only reason place_order is safe to
        retry. /trading/orders/get takes either the venue's order_id or our
        client_order_id, which is exactly what a retry needs.

        Three outcomes, never two. This method used to catch VenueError and
        return None, which made "venue unreachable", "401" and "500" look
        identical to "no such order" — and every caller read None as absence
        and submitted. Both the pre-submit probe and the post-failure recovery
        probe did it, so an outage during a retry produced two live orders.

        PROBE_ABSENT is returned only for a status this venue is configured to
        read as a definite no. Everything else is PROBE_INDETERMINATE, and the
        caller must stand aside: an answer we could not get is not an answer
        of no.
        """
        try:
            payload = self._call("GET", "/trading/orders/get",
                                 {"account_id": account_id,
                                  "client_order_id": client_order_id})
        except VenueError as e:
            status = getattr(e, "http_status", None)
            if status is not None and status in self._not_found_http:
                return _Probe(PROBE_ABSENT, None,
                              "venue answered %d: no such order" % status)
            return _Probe(PROBE_INDETERMINATE, None,
                          "the probe failed without reporting the order as "
                          "absent: %s" % str(e)[:200],
                          retryable=bool(getattr(e, "retryable", False)))

        rows = self._rows(payload, "orders", "items", "data")
        if not rows and isinstance(payload, dict) and payload:
            rows = [payload]          # order detail comes back flat

        # Verify the answer is about the order we asked about. Accepting any
        # payload that merely carried an order_id let an unrelated FILLED order
        # be returned as this one's result: zero POSTs, and the engine booking a
        # fill that never happened and managing a phantom position.
        for row in rows:
            if str(row.get("client_order_id") or "") == client_order_id:
                return _Probe(PROBE_FOUND, row, "matched on client_order_id")

        if rows:
            seen = sorted({str(r.get("client_order_id") or "<none>")
                           for r in rows})[:4]
            return _Probe(
                PROBE_INDETERMINATE, None,
                "the venue answered with %d order(s), none carrying "
                "client_order_id %r (saw: %s) — the query did not filter the "
                "way this adapter assumes, so this is not evidence of absence"
                % (len(rows), client_order_id, ", ".join(seen)))
        return _Probe(
            PROBE_INDETERMINATE, None,
            "the venue answered with no order and no not-found status, so "
            "absence cannot be told apart from an envelope this adapter "
            "failed to parse")

    def place_order(self, order: Order) -> OrderResult:
        if self.read_only:
            raise VenueReadOnly(
                "%s is read-only; enable trading for this venue first" % self.name,
                venue=self.name)
        if not self._instrument_type:
            raise VenueError(
                "venue %r has no instrument_type; Webull needs EQUITY, OPTION, "
                "FUTURES or CRYPTO and the adapter will not infer one from the "
                "symbol" % self.name, venue=self.name)
        cid = order.client_order_id
        if not cid:
            raise VenueError("place_order needs a client_order_id: it is the "
                             "only thing making a retry safe", venue=self.name)
        if len(cid) > _MAX_CLIENT_ORDER_ID:
            # Truncating would collapse two orders onto one idempotency key.
            raise VenueError("client_order_id %r exceeds Webull's %d-character "
                             "limit" % (cid, _MAX_CLIENT_ORDER_ID), venue=self.name)
        # Numeric sanity BEFORE the probe, so a corrupt order never opens a
        # connection. `qty <= 0` is the guard, and it is also the guard NaN
        # defeats: float("nan") <= 0 is False, so a NaN quantity would sail
        # past it and be rendered into the body as the text "nan".
        for label, val in (("qty", order.qty), ("limit_price", order.limit_price)):
            if val is None:
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                raise VenueError("order %s is not a number: %r" % (label, val),
                                 venue=self.name)
            if not math.isfinite(fv):
                raise VenueError(
                    "order %s is %r, which is not a finite number; refusing to "
                    "send it to a venue" % (label, val), venue=self.name)
            if fv <= 0:
                raise VenueError("order %s must be positive, got %r"
                                 % (label, val), venue=self.name)
        if order.stop_loss is not None or order.take_profit is not None:
            # The documented order body has no attached stop or target for a
            # NORMAL order. Sending the order without them would leave a live
            # position whose protection the engine believes is at the venue.
            raise VenueError(
                "Webull's documented order body carries no attached stop or "
                "target; submitting this would open an unprotected position. "
                "Place the protective order separately or manage the stop in "
                "the engine.", venue=self.name)

        side = {"buy": "BUY", "sell": "SELL"}.get(order.side.lower())
        if side is None:
            raise VenueError("unknown side %r" % order.side, venue=self.name)
        if (side == "SELL" and not order.reduce_only
                and self._instrument_type.upper() == "EQUITY"):
            if not self._allow_short:
                raise VenueError(
                    "selling equity that is not being closed is a short on "
                    "Webull; set allow_short on the venue if that is intended",
                    venue=self.name)
            side = "SHORT"

        order_type = {"market": "MARKET", "limit": "LIMIT"}.get(
            order.order_type.lower())
        if order_type is None:
            raise VenueError("unsupported order_type %r" % order.order_type,
                             venue=self.name)
        if order_type == "LIMIT" and order.limit_price is None:
            raise VenueError("limit order without a limit_price", venue=self.name)

        acct = self._require_account()

        # Probe first, always. Webull documents client_order_id as non-repeating
        # but not what it does with a duplicate, and an unverified adapter is the
        # wrong place to find out. One extra GET against a double fill.
        probe = self._probe_client_id(cid, acct)
        if probe.outcome == PROBE_FOUND:
            return self._to_result(cid, probe.order or {},
                                   "idempotent: already placed at venue")
        if probe.outcome != PROBE_ABSENT:
            # Fail CLOSED. Standing aside costs an opportunity; submitting on a
            # probe that proved nothing costs a second position, and no rail
            # downstream can undo a double fill. A later retry with the same
            # client_order_id is still safe: it arrives back here.
            raise VenueError(
                "cannot confirm whether client_order_id %r is already at "
                "Webull: %s. Refusing to submit." % (cid, probe.reason),
                retryable=probe.retryable, venue=self.name)

        item: Dict[str, Any] = {
            "client_order_id": cid,
            "combo_type": "NORMAL",
            "instrument_type": self._instrument_type.upper(),
            "symbol": order.symbol,
            "market": self._market,
            "side": side,
            "order_type": order_type,
            "time_in_force": self._tif,
            "entrust_type": "QTY",
            "support_trading_session": self._session,
            "quantity": _decimal_str(order.qty),
        }
        if order.limit_price is not None:
            item["limit_price"] = _decimal_str(order.limit_price)
        body = {"account_id": acct, "new_orders": [item]}

        try:
            raw = self._call("POST", "/trading/orders/place", {"account_id": acct},
                             body)
        except VenueError as e:
            if not e.retryable:
                raise
            # The submit may or may not have landed. Look before retrying:
            # this is the exact window a double fill comes out of.
            landed = self._probe_client_id(cid, acct)
            if landed.outcome == PROBE_FOUND:
                return self._to_result(cid, landed.order or {},
                                       "order landed despite transport error")
            if landed.outcome == PROBE_ABSENT:
                raise VenueError("Webull transport error, order not found at "
                                 "venue: %s" % e, retryable=True,
                                 venue=self.name, cause=e)
            # Transport failed AND the probe proved nothing. The order may be
            # live. Not retryable: a retry cannot double-submit (the pre-submit
            # probe refuses the same way) but it also cannot resolve this, and
            # a silent retry loop would hide an unmanaged position. Reconcile.
            raise VenueError(
                "Webull transport error (%s) and the follow-up probe could not "
                "determine whether the order landed: %s. client_order_id %r may "
                "be live at the venue — reconcile before acting on it."
                % (str(e)[:200], landed.reason, cid),
                retryable=False, venue=self.name, cause=e)

        rows = self._rows(raw, "orders", "new_orders", "data")
        return self._to_result(cid, rows[0] if rows else
                               (raw if isinstance(raw, dict) else {}))

    def _to_result(self, cid: str, raw: Dict[str, Any],
                   message: str = "") -> OrderResult:
        # Last line of the identity check. Every payload that reaches here is
        # supposed to be about `cid`; one that names a different order would be
        # reported to the engine under this order's id, binding our intent to
        # somebody else's fill.
        echoed = str(raw.get("client_order_id") or "")
        if echoed and echoed != cid:
            raise VenueError(
                "Webull answered about client_order_id %r while this call was "
                "placing %r; refusing to report another order as this one"
                % (echoed, cid), venue=self.name)

        status = _STATUS_MAP.get(str(raw.get("status") or
                                     raw.get("order_status") or "").upper(),
                                 "")
        if not status:
            # A place that returned an order_id and no state we recognise is
            # accepted-but-unconfirmed, not filled and not failed.
            status = "accepted" if raw.get("order_id") else "unknown"
        # A non-finite fill size or price raises out of _num rather than
        # becoming 0.0 / None here. That loses the OrderResult for an order
        # that IS at the venue, which is why the message carries both ids: the
        # honest outcome is reconciliation, not a confident zero fill.
        return OrderResult(
            client_order_id=cid,
            venue_order_id=str(raw.get("order_id") or ""),
            status=status,
            filled_qty=_num(raw, ("filled_quantity", "filledQuantity",
                                  "filled_qty"),
                            "filled quantity for order %s / %s"
                            % (cid, raw.get("order_id")), self.name) or 0.0,
            avg_price=_num(raw, ("avg_filled_price", "average_filled_price",
                                 "avg_price"),
                           "average fill price for order %s / %s"
                           % (cid, raw.get("order_id")), self.name),
            message=message, raw=raw)

    def _cancel_confirmed(self, payload: Any,
                          venue_order_id: str) -> Tuple[bool, str]:
        """Did the venue CONFIRM this cancel? (ok, why-not).

        Only an explicit yes counts. Webull's cancel response schema is not
        published and HTTP 200 does not mean the cancel happened — a 200 body
        of {"success": false, "msg": "order already filled"} is a refusal.
        """
        rows = self._rows(payload, "orders", "items", "data")
        row = rows[0] if rows else (payload if isinstance(payload, dict) else None)
        if not isinstance(row, dict) or not row:
            return False, ("the response carried no cancel confirmation (%s)"
                           % (str(payload)[:120] or "empty body"))

        echoed = str(row.get("order_id") or row.get("orderId") or "")
        if echoed and echoed != str(venue_order_id):
            return False, ("the response is about order %s, not %s"
                           % (echoed, venue_order_id))

        msg = str(row.get("msg") or row.get("message") or "")[:120]
        # Every flag present has to agree. Returning on the first one found
        # would let {"success": true, "cancelled": false} through on whichever
        # happened to be checked first.
        flags = [(k, row[k], _truthy(row[k])) for k in _CANCEL_FLAG_KEYS if k in row]
        for k, val, verdict in flags:
            if verdict is False:
                return False, ("the venue reported %s=%r%s"
                               % (k, val, " (%s)" % msg if msg else ""))
            if verdict is None:
                return False, ("the venue reported %s=%r, which this adapter "
                               "cannot read as consent" % (k, val))
        if flags:
            return True, ""

        raw_status = str(row.get("status") or row.get("order_status") or "").upper()
        # _STATUS_MAP folds PENDING_CANCEL into "cancelled" because that is the
        # right reading for an order's lifecycle state. It is the WRONG reading
        # for "did my cancel happen": a cancel the venue has merely accepted can
        # still lose the race to a fill.
        if raw_status in ("PENDING_CANCEL", "CANCEL_PENDING", "CANCELLING",
                          "CANCELING"):
            return False, ("the venue reports the cancel as still pending (%s); "
                           "the order can still fill" % raw_status)
        status = _STATUS_MAP.get(raw_status, "")
        if status == "cancelled":
            return True, ""
        if status:
            return False, "the venue reports the order as %s" % status
        return False, ("the response carried no success flag and no recognised "
                       "status (keys: %s)" % ", ".join(sorted(row)[:10]))

    def cancel(self, venue_order_id: str, symbol: str = "") -> bool:
        """True only when Webull confirmed the cancel; raises otherwise.

        This used to `return True` without reading the response at all, so a
        200 carrying {"success": false, "msg": "order already filled"} recorded
        a cancel that never happened while the position stayed live. It raises
        rather than returning False because a bool nobody is obliged to check
        is call-site discipline, not a rail — and the failure mode of ignoring
        it is an unmanaged position.
        """
        if self.read_only:
            raise VenueReadOnly("%s is read-only" % self.name, venue=self.name)
        if not venue_order_id:
            raise VenueError("cancel needs a venue order_id", venue=self.name)
        acct = self._require_account()
        raw = self._call(_CANCEL_METHOD, "/trading/orders/cancel",
                         {"account_id": acct}, {"account_id": acct,
                                                "order_id": venue_order_id})
        ok, why = self._cancel_confirmed(raw, venue_order_id)
        if ok:
            return True
        raise VenueError(
            "Webull did not confirm the cancel of order %s: %s. Treat the order "
            "as still working and the position as live until a read says "
            "otherwise." % (venue_order_id, why), venue=self.name)

    def stream_prices(self, symbols: List[str]) -> Iterator[Tick]:
        """No ticks from this adapter, deliberately.

        Webull market data is a separate entitlement on a separate host
        (data-api.webull.com) over MQTT, not the trading HTTP API. Polling
        quotes through the trading credentials is not something the docs offer,
        so this venue must not be wired as a price source -- take prices from
        the exchange adapter and use Webull for execution and account state.
        """
        return iter(())


def new_client_order_id(prefix: str = "slc") -> str:
    """Idempotency key, kept inside Webull's 32-character ceiling."""
    cid = "%s-%s" % (prefix, uuid.uuid4().hex[:20])
    if len(cid) > _MAX_CLIENT_ORDER_ID:
        raise VenueError("client_order_id prefix %r is too long" % prefix)
    return cid


register("webull", WebullVenue)
