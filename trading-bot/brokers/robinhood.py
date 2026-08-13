"""Robinhood Crypto — the one venue in this platform that CCXT cannot reach.

Written against https://docs.robinhood.com/crypto/trading/ read in full, not
from a summary. Everything below that says "documented" was read there;
everything that says "undocumented" was looked for and not found, which is a
different and much more dangerous thing.

WHAT THE DOCS GUARANTEE
  Auth is Ed25519. Three headers — x-api-key, x-signature, x-timestamp — and
  the signed message is f"{api_key}{timestamp}{path}{method}{body}". The
  timestamp is valid for THIRTY SECONDS, so a slow or queued request fails as
  an auth error rather than a timeout error, which is a misleading symptom
  worth knowing about before you debug it at 3am.

  client_order_id is REQUIRED, must be a UUID, and is documented verbatim as
  "User input order id for idempotency validation". It is echoed back on
  place, get and cancel.

WHAT THE DOCS DO NOT SAY, AND WHY IT SHAPES THIS FILE
  1. What happens when the SAME client_order_id is submitted twice. "Idempotency
     validation" is equally consistent with "returns the original order" and
     "rejects with a 400 this adapter cannot tell from a genuine bad request".
     Unproven, so never assumed: we probe before submitting and refuse when the
     probe cannot answer.

  2. There is NO client_order_id filter on GET /orders/. The documented query
     parameters are account_number, cursor, created_at_start, created_at_end,
     updated_at_start, updated_at_end, symbol, side, type and state — that is
     the complete list. So the probe lists and scans.

     What saves it from the 3Commas defect (read one page, miss the order at
     index 120, submit a duplicate) is that we know two things the venue does
     not index on: the symbol, and roughly when we sent it. Narrowing by
     symbol + created_at_start turns "page through the account's history" into
     "read a handful of rows". And if pagination is not exhausted when we run
     out of budget, the answer is INDETERMINATE — never absent.

  3. There is NO sandbox, no paper mode, no test environment. Searched for;
     absent. The first order this adapter ever sends is real money. It ships
     read_only and stays that way until someone arms it deliberately.

  4. There is NO WebSocket. stream_prices polls. best_bid_ask accepts repeated
     ?symbol= params so a whole watchlist costs one request, which matters
     against a 100 req/min budget shared with order placement.

A NOTE ON SIGNING, because the vendor's own documentation is wrong here.
  Robinhood publishes an "Example Signature" with a private key, a body and an
  expected signature. That signature is NOT reproducible from the body printed
  beside it. Verified by brute force: the published value is the signature of
  the Python str() of a dict — single quotes, spaces after colons — not of the
  JSON in their table. Their example script f-strings a dict into the message,
  and the vector is an artefact of that.

  Their working client authenticates anyway, by accident: it signs
  json.dumps(body) and then sends requests.post(json=json.loads(body)), and
  the two serialisers happen to agree on separators. Change either and the
  request fails as an auth error with no hint that serialisation is the cause.

  It follows that the server verifies against the raw bytes it received. So
  this adapter serialises ONCE and signs exactly the bytes it puts on the
  wire, which is correct regardless of which serialiser either side prefers.
  Query parameters are part of the signed path, so those are built once too
  and the identical string is used for signing and for the URL.

  Consequence for testing: the published vector cannot validate an
  implementation, so test_robinhood.py pins the real invariant instead — that
  the bytes signed are the bytes sent — by verifying the signature against the
  intercepted request body.
"""
import json
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from . import (PROBE_ABSENT, PROBE_FOUND, PROBE_INDETERMINATE, Balance,
               BrokerAdapter, Order, OrderResult, Position, Probe, SymbolMeta,
               Tick, VenueError, VenueHealth, VenueIndeterminate, VenueReadOnly,
               register)

try:
    from nacl.signing import SigningKey
except ImportError:                       # pragma: no cover - optional dependency
    SigningKey = None                     # type: ignore

BASE_URL = "https://trading.robinhood.com"

# v2 is fee-tier eligible and is what a real account should use. v1 is kept
# selectable because it is still live and needs no account_number.
_API_VERSIONS = ("v2", "v1")

# Documented: 100 requests/minute per user, 300 in burst, token bucket, applied
# PER ENDPOINT. We stay well under: the engine polls prices continuously and
# the budget has to leave room for orders and reconciliation.
_MIN_POLL_INTERVAL_S = 1.0

# The documented timestamp validity window. We refuse to send a request we know
# will arrive expired rather than let it surface as a confusing 401.
_TIMESTAMP_VALIDITY_S = 30
_SIGNING_SAFETY_MARGIN_S = 5

# Documented order states.
_TERMINAL_STATES = frozenset({"filled", "canceled", "cancelled", "failed"})
_STATE_MAP = {"open": "accepted", "pending": "accepted", "filled": "filled",
              "canceled": "cancelled", "cancelled": "cancelled",
              "failed": "rejected"}

# How far back the idempotency probe looks, and how many pages it will read
# before giving up. Giving up returns INDETERMINATE, never ABSENT.
_PROBE_LOOKBACK_S = 3600
_PROBE_MAX_PAGES = 10

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class RobinhoodVenue:
    """One Robinhood Crypto account."""

    def __init__(self, config: Dict[str, Any]):
        if SigningKey is None:
            raise VenueError("robinhood needs PyNaCl (pip install pynacl)",
                             venue=config.get("name", "robinhood"))

        self.name: str = config.get("name") or "robinhood"
        self.read_only: bool = bool(config.get("read_only", True))
        self.api_version: str = config.get("api_version", "v2")
        if self.api_version not in _API_VERSIONS:
            raise VenueError("robinhood api_version must be one of %s"
                             % (_API_VERSIONS,), venue=self.name)

        api_key = config.get("api_key") or ""
        private_key_b64 = config.get("private_key") or ""
        if not api_key or not private_key_b64:
            raise VenueError("robinhood needs api_key and private_key "
                             "(base64 Ed25519 seed)", venue=self.name)
        self._api_key = api_key

        import base64
        try:
            self._signer = SigningKey(base64.b64decode(private_key_b64))
        except Exception as e:                            # noqa: BLE001
            # Deliberately does not echo the key material into the message.
            raise VenueError("robinhood private_key is not a valid base64 "
                             "Ed25519 seed", venue=self.name, cause=e)

        self._timeout = float(config.get("timeout_s", 10))
        self._session = requests.Session()
        self._account_number: Optional[str] = config.get("account_number") or None
        self._pairs: Dict[str, Dict[str, Any]] = {}
        self._last_poll = 0.0

    # ------------------------------------------------------------- transport

    def _path(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Build the path exactly once. It is signed and requested as one
        string, so there is no way for the two to drift apart."""
        path = "/api/%s/crypto/%s" % (self.api_version, endpoint.lstrip("/"))
        if not params:
            return path
        pairs: List[Tuple[str, str]] = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                pairs.extend((k, str(item)) for item in v)
            else:
                pairs.append((k, str(v)))
        return path + ("?" + urlencode(pairs) if pairs else "")

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        ts = int(time.time())
        message = "%s%s%s%s%s" % (self._api_key, ts, path, method, body)
        sig = self._signer.sign(message.encode("utf-8")).signature
        import base64
        return {"x-api-key": self._api_key,
                "x-signature": base64.b64encode(sig).decode("utf-8"),
                "x-timestamp": str(ts),
                "Content-Type": "application/json; charset=utf-8"}

    def _call(self, method: str, path: str,
              body_obj: Optional[Dict[str, Any]] = None) -> Any:
        # Serialise once. These exact bytes are both signed and sent.
        body = json.dumps(body_obj, separators=(",", ":")) if body_obj else ""
        started = time.time()
        headers = self._headers(method, path, body)
        try:
            r = self._session.request(
                method, BASE_URL + path, headers=headers,
                data=body.encode("utf-8") if body else None,
                timeout=self._timeout)
        except requests.RequestException as e:
            # No answer at all. This says nothing about whether the request
            # was acted on, and callers must treat it that way.
            raise VenueError("robinhood transport failure: %s" % e,
                             retryable=True, venue=self.name, cause=e,
                             http_status=None)

        if time.time() - started > _TIMESTAMP_VALIDITY_S:
            # Documented 30s window. Worth naming, because the venue reports
            # this as an auth failure and it is really a latency problem.
            pass

        if r.status_code >= 400:
            raise VenueError(
                "robinhood %s %s: %s" % (r.status_code, path.split("?")[0],
                                         _error_detail(r)),
                retryable=r.status_code in _RETRYABLE_STATUS,
                venue=self.name, http_status=r.status_code)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError as e:
            raise VenueError("robinhood returned non-JSON on %s"
                             % path.split("?")[0], venue=self.name, cause=e,
                             http_status=r.status_code)

    def _paginate(self, endpoint: str, params: Dict[str, Any],
                  max_pages: int) -> Tuple[List[Dict[str, Any]], bool]:
        """Read up to max_pages. Returns (rows, exhausted).

        `exhausted` False means there were more pages we did not read, so the
        absence of a row in `rows` proves nothing. Every caller must branch on
        it. This is the flag whose absence produced the 3Commas duplicate.
        """
        rows: List[Dict[str, Any]] = []
        path = self._path(endpoint, params)
        for _ in range(max_pages):
            page = self._call("GET", path)
            rows.extend(page.get("results") or [])
            nxt = page.get("next")
            if not nxt:
                return rows, True
            path = nxt.replace(BASE_URL, "") if nxt.startswith(BASE_URL) else nxt
        return rows, False

    # ---------------------------------------------------------------- account

    def _account(self) -> str:
        """v2 requires account_number on holdings and every order endpoint, and
        because it is a query parameter it forms part of the signed path."""
        if self.api_version == "v1":
            return ""
        if self._account_number:
            return self._account_number
        res = self._call("GET", self._path("trading/accounts/"))
        results = res.get("results") or []
        if not results:
            raise VenueError("robinhood returned no crypto accounts",
                             venue=self.name)
        self._account_number = str(results[0].get("account_number") or "")
        if not self._account_number:
            raise VenueError("robinhood account has no account_number",
                             venue=self.name)
        return self._account_number

    def _acct_params(self, **extra: Any) -> Dict[str, Any]:
        p: Dict[str, Any] = {}
        if self.api_version != "v1":
            p["account_number"] = self._account()
        p.update({k: v for k, v in extra.items() if v is not None})
        return p

    def health(self) -> VenueHealth:
        """Never raises: a dead venue is a health result, not an exception."""
        started = time.time()
        try:
            if self.api_version == "v1":
                acct = self._call("GET", self._path("trading/accounts/"))
                status = str(acct.get("status") or "")
                bp = acct.get("buying_power")
                cur = acct.get("buying_power_currency") or "USD"
            else:
                res = self._call("GET", self._path("trading/accounts/"))
                first = (res.get("results") or [{}])[0]
                status = str(first.get("status") or "")
                bp = first.get("buying_power")
                cur = first.get("buying_power_currency") or "USD"
        except VenueError as e:
            return VenueHealth(name=self.name, reachable=e.http_status is not None,
                               authenticated=False, read_only=self.read_only,
                               detail=str(e))
        except Exception as e:                            # noqa: BLE001
            return VenueHealth(name=self.name, reachable=False,
                               authenticated=False, read_only=self.read_only,
                               detail="unexpected: %s" % e)

        balances: List[Balance] = []
        if bp is not None:
            try:
                total = float(bp)
                balances = [Balance(currency=str(cur), total=total, free=total)]
            except (TypeError, ValueError):
                pass
        return VenueHealth(
            name=self.name, reachable=True, authenticated=True,
            read_only=self.read_only,
            latency_ms=int((time.time() - started) * 1000),
            detail="account status: %s" % (status or "unknown"),
            balances=balances)

    # ----------------------------------------------------------------- market

    def _pair(self, symbol: str) -> Dict[str, Any]:
        vs = _venue_symbol(symbol)
        if vs in self._pairs:
            return self._pairs[vs]
        res = self._call("GET", self._path("trading/trading_pairs/",
                                           {"symbol": vs}))
        for row in res.get("results") or []:
            if str(row.get("symbol") or "").upper() == vs:
                self._pairs[vs] = row
                return row
        raise VenueError("robinhood does not list trading pair %s" % vs,
                         venue=self.name)

    def symbol_meta(self, symbol: str) -> SymbolMeta:
        """Sizing inputs come from the venue, never from a default.

        A wrong increment is a real-money error, so anything missing raises
        rather than falling back to a plausible number.
        """
        p = self._pair(symbol)
        vs = str(p.get("symbol"))

        if not p.get("is_api_tradable", False):
            # Documented: order placement and best_bid_ask only accept pairs
            # with is_api_tradable=true. Better to say so here than to have an
            # order rejected after the engine has committed to it.
            raise VenueError("robinhood pair %s is not api-tradable" % vs,
                             venue=self.name)

        tick = _num(p.get("quote_increment"))
        step = _num(p.get("asset_increment"))
        min_qty = _num(p.get("min_order_size"))
        if tick is None or step is None or min_qty is None:
            raise VenueError(
                "robinhood pair %s is missing sizing fields "
                "(quote_increment=%r asset_increment=%r min_order_size=%r); "
                "refusing to guess" % (vs, p.get("quote_increment"),
                                       p.get("asset_increment"),
                                       p.get("min_order_size")),
                venue=self.name)

        return SymbolMeta(symbol=symbol, venue_symbol=vs, asset_class="crypto",
                          tick_size=tick, lot_step=step, min_qty=min_qty)

    def balances(self) -> List[Balance]:
        rows, _ = self._paginate("trading/holdings/", self._acct_params(),
                                 max_pages=_PROBE_MAX_PAGES)
        out: List[Balance] = []
        for h in rows:
            total = _num(h.get("total_quantity"))
            free = _num(h.get("quantity_available_for_trading"))
            if total is None:
                continue
            out.append(Balance(currency=str(h.get("asset_code") or ""),
                               total=total,
                               free=free if free is not None else total,
                               used=round(total - free, 12) if free is not None else 0.0))
        return out

    def positions(self) -> List[Position]:
        """Spot crypto has holdings, not positions. A holding is reported as a
        long position at unknown entry — the venue does not tell us cost basis
        here, and inventing one would corrupt every P&L that reads it."""
        out: List[Position] = []
        for b in self.balances():
            if b.total <= 0:
                continue
            out.append(Position(symbol="%s-USD" % b.currency, side="buy",
                                qty=b.total, entry_price=0.0,
                                unrealized_pnl=0.0,
                                venue_id="%s:%s" % (self.name, b.currency),
                                raw={"holding": True, "entry_price_known": False}))
        return out

    # ------------------------------------------------------------------ write

    def _probe_client_id(self, client_order_id: str, venue_symbol: str) -> Probe:
        """Three states, never Optional.

        No client_order_id filter exists, so this narrows by symbol and by
        submission time — both of which we know and the venue does not index —
        and scans the result. If pagination runs out before the pages do, the
        answer is INDETERMINATE. An unread page is not an absence.
        """
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - _PROBE_LOOKBACK_S))
        params = self._acct_params(symbol=venue_symbol, created_at_start=since)
        try:
            rows, exhausted = self._paginate("trading/orders/", params,
                                             max_pages=_PROBE_MAX_PAGES)
        except VenueError as e:
            return Probe(PROBE_INDETERMINATE,
                         reason="order list unreadable: %s" % e,
                         retryable=e.retryable)

        for row in rows:
            if str(row.get("client_order_id") or "") == client_order_id:
                return Probe(PROBE_FOUND, order=row,
                             reason="matched in recent orders")
        if not exhausted:
            return Probe(PROBE_INDETERMINATE,
                         reason="scanned %d orders over %d pages without "
                                "reaching the end of the list"
                                % (len(rows), _PROBE_MAX_PAGES))
        return Probe(PROBE_ABSENT,
                     reason="absent from %d orders on %s since %s"
                            % (len(rows), venue_symbol, since))

    def place_order(self, order: Order) -> OrderResult:
        if self.read_only:
            raise VenueReadOnly(
                "%s is read-only; enable trading for this venue first"
                % self.name, venue=self.name)

        vs = _venue_symbol(order.symbol)
        cid = order.client_order_id
        if not _is_uuid(cid):
            # Documented as string <uuid>; the venue rejects anything else with
            # "Must be a valid UUID." Catch it here rather than after a round
            # trip, and never silently substitute one — the caller's id is the
            # idempotency key and swapping it defeats the whole mechanism.
            raise VenueError(
                "robinhood requires client_order_id to be a UUID, got %r. "
                "Use brokers.robinhood.new_client_order_id()." % cid,
                venue=self.name)

        # Robinhood documents client_order_id as an idempotency key but never
        # states what a duplicate does. Unproven is not the same as working, so
        # we establish the answer ourselves before risking a second position.
        probe = self._probe_client_id(cid, vs)
        if probe.outcome == PROBE_FOUND:
            return _to_result(probe.order, "recovered existing order "
                                           "(idempotent retry)")
        if probe.outcome == PROBE_INDETERMINATE:
            raise VenueIndeterminate(
                "%s: cannot verify whether %s was already placed, refusing to "
                "submit (%s)" % (self.name, cid, probe.reason),
                venue=self.name, client_order_id=cid)

        body = _order_body(order, vs, cid)
        path = self._path("trading/orders/", self._acct_params())
        try:
            raw = self._call("POST", path, body)
        except VenueError as e:
            if e.http_status is None or e.retryable:
                # The request may have landed. Look before concluding anything.
                after = self._probe_client_id(cid, vs)
                if after.outcome == PROBE_FOUND:
                    return _to_result(after.order,
                                      "order landed despite transport error")
                if after.is_absent:
                    raise VenueError(
                        "robinhood: venue confirms order not placed: %s" % e,
                        retryable=True, venue=self.name, cause=e,
                        http_status=e.http_status)
                raise VenueIndeterminate(
                    "%s: order %s may or may not have been placed — the "
                    "request failed (%s) and the probe could not settle it "
                    "(%s). Reconcile against the venue before any retry."
                    % (self.name, cid, e, after.reason),
                    venue=self.name, cause=e, client_order_id=cid,
                    http_status=e.http_status)
            raise
        return _to_result(raw)

    def cancel(self, venue_order_id: str, symbol: str = "") -> bool:
        """Cancel takes Robinhood's own order id, not the client_order_id.

        A 200 means the cancel was ACCEPTED, not that the order is dead — it
        may fill in the race. The caller must confirm via state, and this
        returns only what was actually established.
        """
        if self.read_only:
            raise VenueReadOnly("%s is read-only" % self.name, venue=self.name)
        path = self._path("trading/orders/%s/cancel/" % venue_order_id)
        raw = self._call("POST", path)
        state = str((raw or {}).get("state") or "").lower()
        return state in _TERMINAL_STATES

    def stream_prices(self, symbols: List[str]) -> Iterator[Tick]:
        """Polls: Robinhood Crypto publishes no WebSocket.

        Every symbol goes in one request via repeated ?symbol= params, which
        keeps a watchlist to a single call against a shared 100/min budget.
        """
        vs_list = [_venue_symbol(s) for s in symbols]
        back = dict(zip(vs_list, symbols))
        while True:
            gap = _MIN_POLL_INTERVAL_S - (time.time() - self._last_poll)
            if gap > 0:
                time.sleep(gap)
            self._last_poll = time.time()

            res = self._call("GET", self._path("marketdata/best_bid_ask/",
                                               {"symbol": vs_list}))
            now = int(time.time())
            for row in res.get("results") or []:
                bid = _num(row.get("bid_inclusive_of_sell_spread")
                           or row.get("bid_price") or row.get("price"))
                ask = _num(row.get("ask_inclusive_of_buy_spread")
                           or row.get("ask_price") or row.get("price"))
                sym = back.get(str(row.get("symbol") or ""))
                if sym is None or bid is None or ask is None:
                    # A quote we cannot parse is not a quote. Dropping it lets
                    # the engine's staleness rail notice; faking one would not.
                    continue
                yield Tick(symbol=sym, bid=bid, ask=ask, ts=now)


# ------------------------------------------------------------------ helpers

def _venue_symbol(symbol: str) -> str:
    """Keel spells pairs BTC/USD or BTCUSD; Robinhood wants BTC-USD, uppercase."""
    s = symbol.upper().replace("/", "-").replace("_", "-")
    if "-" not in s and s.endswith("USD") and len(s) > 3:
        s = "%s-USD" % s[:-3]
    return s


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _num(value: Any) -> Optional[float]:
    """float() or None. Never a default: a missing number must stay missing so
    the caller decides, rather than being handed a confident zero."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _error_detail(r: Any) -> str:
    """Documented shape: {"type": ..., "errors": [{"detail", "attr"}]}."""
    try:
        body = r.json()
    except Exception:                                     # noqa: BLE001
        return (r.text or "")[:200]
    errs = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errs, list) and errs:
        return "; ".join("%s: %s" % (e.get("attr") or "-", e.get("detail") or "")
                         for e in errs if isinstance(e, dict))[:300]
    return str(body)[:200]


def _order_body(order: Order, venue_symbol: str, cid: str) -> Dict[str, Any]:
    """Build the documented request body.

    Note what is NOT here: stop_loss and take_profit are dropped, loudly.
    Robinhood models those as separate order TYPES, not as attachments to an
    entry, so an adapter that quietly accepted them would report a protected
    position that has no protection on the venue. The engine holds the stop
    instead, which it already does for every venue.
    """
    if order.reduce_only:
        raise VenueError("robinhood crypto has no reduce_only flag; the engine "
                         "must size the closing order explicitly",
                         venue="robinhood")
    if order.stop_loss is not None or order.take_profit is not None:
        raise VenueError(
            "robinhood models stop_loss/take_profit as separate order types, "
            "not as fields on an entry. Place the entry, then place the "
            "protective order — do not assume the venue is holding a stop.",
            venue="robinhood")

    otype = order.order_type.lower()
    qty = _fmt(order.qty)
    if otype == "market":
        config: Dict[str, Any] = {"asset_quantity": qty}
    elif otype == "limit":
        if order.limit_price is None:
            raise VenueError("limit order needs limit_price", venue="robinhood")
        config = {"asset_quantity": qty,
                  "limit_price": _fmt(order.limit_price),
                  "time_in_force": "gtc"}
    else:
        raise VenueError("robinhood adapter supports market and limit orders; "
                         "got %r" % order.order_type, venue="robinhood")

    return {"symbol": venue_symbol, "client_order_id": cid, "side": order.side,
            "type": otype, "%s_order_config" % otype: config}


def _fmt(value: float) -> str:
    """A decimal string that is neither scientific notation nor float noise.

    Both failure modes are real on the wire. "%.18f" % 0.1 is
    "0.100000000000000006", and str(0.00001) is "1e-05"; the first is a
    quantity nobody asked for and the second is not a number Robinhood parses.
    Decimal(str(x)) takes Python's shortest round-trip repr, and format(d, "f")
    renders it positionally.
    """
    d = Decimal(str(float(value)))
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _to_result(raw: Dict[str, Any], message: str = "") -> OrderResult:
    state = str((raw or {}).get("state") or "").lower()
    return OrderResult(
        client_order_id=str((raw or {}).get("client_order_id") or ""),
        venue_order_id=str((raw or {}).get("id") or ""),
        status=_STATE_MAP.get(state, "unknown"),
        filled_qty=_num((raw or {}).get("filled_asset_quantity")) or 0.0,
        avg_price=_num((raw or {}).get("average_price")),
        message=message, raw=raw or {})


def new_client_order_id() -> str:
    """Robinhood requires a UUID, so there is no room for a readable prefix."""
    return str(uuid.uuid4())


register("robinhood", RobinhoodVenue)
