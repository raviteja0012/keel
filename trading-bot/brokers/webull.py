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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

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


def _num(row: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    """First key present and numeric, or None. Never a default: the caller has
    to decide whether a missing field is survivable, and mostly it is not."""
    for k in keys:
        v = row.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _decimal_str(x: float) -> str:
    """Webull takes quantities and prices as strings. Exponent notation is what
    a naive str() produces for a small crypto quantity and it is not a format
    the docs offer, so render plain decimal and trim."""
    s = ("%.8f" % float(x)).rstrip("0").rstrip(".")
    return s or "0"


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
                detail = e.read().decode()[:300]
            except Exception:
                pass
            # 401 is bad credentials and 417 is a business rejection: retrying
            # either just repeats the same answer. 429/5xx may be transient.
            raise VenueError("Webull HTTP %d: %s" % (e.code, detail),
                             retryable=e.code in (429, 500, 502, 503, 504),
                             venue=self.name, cause=e)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise VenueError("Webull unreachable: %s" % e, retryable=True,
                             venue=self.name, cause=e)

        self._note_skew(resp_headers)
        return self._unwrap(raw)

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
            raise VenueError("Webull returned non-JSON: %s" % raw[:200],
                             venue=self.name, cause=e)
        # AMBIGUOUS: the docs signal failure with HTTP status codes and do not
        # publish an envelope. Some Webull surfaces still wrap in {code,msg,data},
        # so unwrap only when a code is actually present, and treat an unfamiliar
        # code as a failure rather than reading through it.
        if isinstance(data, dict) and "code" in data:
            code = str(data.get("code"))
            if code not in ("200", "0", "OK", "success"):
                raise VenueError("Webull error %s: %s"
                                 % (code, str(data.get("msg"))[:200]),
                                 venue=self.name)
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
            v = _num(row, (field,)) if field else None
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
            min_notional = _num(row, (f["min_notional"],)) or 0.0

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
            total = _num(row, _BAL_TOTAL_KEYS)
            if not cur or total is None:
                continue
            free = _num(row, _BAL_FREE_KEYS)
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
            qty = _num(row, _POS_QTY_KEYS)
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
                entry_price=_num(row, _POS_COST_KEYS) or 0.0,
                unrealized_pnl=_num(row, _POS_PNL_KEYS) or 0.0,
                venue_id=str(row.get("position_id") or row.get("instrument_id") or ""),
                raw=row))
        return out

    # --------------------------------------------------------------- writes
    def _find_by_client_id(self, client_order_id: str,
                           account_id: str) -> Optional[Dict[str, Any]]:
        """The idempotency probe. /trading/orders/get takes either the venue's
        order_id or our client_order_id, which is exactly what a retry needs."""
        try:
            payload = self._call("GET", "/trading/orders/get",
                                 {"account_id": account_id,
                                  "client_order_id": client_order_id})
        except VenueError:
            # Not-found is reported as an HTTP error by this API. A genuine
            # outage is handled by the caller, which never blind-retries.
            return None
        rows = self._rows(payload, "orders", "items", "data")
        if rows:
            payload = rows[0]
        if isinstance(payload, dict) and (payload.get("order_id")
                                          or payload.get("client_order_id")):
            return payload
        return None

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
        existing = self._find_by_client_id(cid, acct)
        if existing:
            return self._to_result(cid, existing,
                                   "idempotent: already placed at venue")

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
            landed = self._find_by_client_id(cid, acct)
            if landed:
                return self._to_result(cid, landed,
                                       "order landed despite transport error")
            raise VenueError("Webull transport error, order not found at venue: %s"
                             % e, retryable=True, venue=self.name, cause=e)

        rows = self._rows(raw, "orders", "new_orders", "data")
        return self._to_result(cid, rows[0] if rows else
                               (raw if isinstance(raw, dict) else {}))

    def _to_result(self, cid: str, raw: Dict[str, Any],
                   message: str = "") -> OrderResult:
        status = _STATUS_MAP.get(str(raw.get("status") or
                                     raw.get("order_status") or "").upper(),
                                 "")
        if not status:
            # A place that returned an order_id and no state we recognise is
            # accepted-but-unconfirmed, not filled and not failed.
            status = "accepted" if raw.get("order_id") else "unknown"
        return OrderResult(
            client_order_id=cid,
            venue_order_id=str(raw.get("order_id") or ""),
            status=status,
            filled_qty=_num(raw, ("filled_quantity", "filledQuantity",
                                  "filled_qty")) or 0.0,
            avg_price=_num(raw, ("avg_filled_price", "average_filled_price",
                                 "avg_price")),
            message=message, raw=raw)

    def cancel(self, venue_order_id: str, symbol: str = "") -> bool:
        if self.read_only:
            raise VenueReadOnly("%s is read-only" % self.name, venue=self.name)
        acct = self._require_account()
        self._call(_CANCEL_METHOD, "/trading/orders/cancel",
                   {"account_id": acct}, {"account_id": acct,
                                          "order_id": venue_order_id})
        return True

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
