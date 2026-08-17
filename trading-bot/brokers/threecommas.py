"""3Commas venue adapter.

3Commas is a bot platform, not an exchange, and that shapes how it is used here.
It is wired as an EXECUTION DESTINATION and a source of account state: the engine
decides, 3Commas transmits to the underlying exchange.

The rule that keeps this safe is one owner per position. A position this engine
opened through 3Commas must not also be managed by a 3Commas DCA/grid bot, or two
risk engines end up fighting over the same stop. Use 3Commas bots on accounts this
engine does not trade, or use 3Commas purely as a router here. `manages_positions`
is recorded on the venue so the operator's intent is explicit and visible.

Auth is HMAC-SHA256 over the request path, signed with the API secret, sent as
the Signature header alongside APIKEY. Endpoints used:
  GET  /public/api/ver1/validate           credential check
  GET  /public/api/ver1/accounts           connected exchange accounts
  GET  /public/api/ver1/accounts/{id}/... balances
  GET  /public/api/ver1/smart_trades/v2    open smart trades (positions)
  POST /public/api/ver1/smart_trades/v2    place a smart trade
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from . import (PROBE_ABSENT, PROBE_FOUND, PROBE_INDETERMINATE, Balance, Order,
               OrderResult, Position, Probe, SymbolMeta, Tick, VenueError,
               VenueHealth, VenueIndeterminate, VenueReadOnly, register)

BASE = "https://api.3commas.io"
_PREFIX = "/public/api"

# The idempotency probe reads smart trades one page at a time. 3Commas has NO
# clientOrderId, so the only key is a marker written into the note field, and
# the only way to check it is to scan the smart-trade list. A page that comes
# back this full may be hiding our trade past its end, so absence is unproven
# until a page returns short. Reading one page and calling absence is exactly
# how a trade at index 120 got a duplicate.
_PROBE_PER_PAGE = 100


def _finite(value: Any) -> Optional[float]:
    """A finite float or None. NaN and +/-inf are never returned: they slip
    through every comparison guard and must not reach a real venue."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


class ThreeCommasVenue:
    def __init__(self, config: Dict[str, Any]):
        self.name: str = config.get("name", "3commas")
        self.read_only: bool = bool(config.get("read_only", True))
        self._key: str = config.get("api_key") or ""
        self._secret: str = config.get("api_secret") or ""
        self._account_id = config.get("account_id")
        self._timeout = int(config.get("timeout_ms", 15000)) / 1000.0
        # Operator's declaration: are 3Commas' own bots also trading this account?
        # If they are, this engine must not open positions here.
        self.manages_positions: bool = bool(config.get("manages_positions", False))

    # ------------------------------------------------------------- internals
    def _sign(self, path_with_query: str, body: str = "") -> str:
        msg = (path_with_query + body).encode()
        return hmac.new(self._secret.encode(), msg, hashlib.sha256).hexdigest()

    def _call(self, method: str, path: str, params: Optional[Dict] = None,
              body: Optional[Dict] = None) -> Any:
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        signed_path = _PREFIX + path + qs
        payload = json.dumps(body) if body else ""
        req = urllib.request.Request(
            BASE + signed_path, method=method,
            data=payload.encode() if payload else None,
            headers={"APIKEY": self._key,
                     "Signature": self._sign(signed_path, payload),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                raw = r.read().decode()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            retryable = e.code in (429, 500, 502, 503, 504)
            raise VenueError("3Commas HTTP %d: %s" % (e.code, detail),
                             retryable=retryable, venue=self.name, cause=e)
        except (urllib.error.URLError, TimeoutError) as e:
            raise VenueError("3Commas unreachable: %s" % e, retryable=True,
                             venue=self.name, cause=e)

    # ---------------------------------------------------------------- reads
    def health(self) -> VenueHealth:
        t0 = time.time()
        if not (self._key and self._secret):
            return VenueHealth(self.name, False, False, self.read_only,
                               detail="API key and secret not configured")
        try:
            self._call("GET", "/ver1/validate")
            latency = int((time.time() - t0) * 1000)
        except VenueError as e:
            reachable = "unreachable" not in str(e)
            return VenueHealth(self.name, reachable, False, self.read_only,
                               detail=str(e)[:300])
        bals, detail = [], ""
        if self.manages_positions:
            detail = ("3Commas bots manage this account: engine will not open "
                      "positions here (one owner per position)")
        try:
            bals = self.balances()
        except VenueError as e:
            detail = (detail + " | " if detail else "") + str(e)[:200]
        return VenueHealth(self.name, True, True, self.read_only,
                           latency_ms=latency, detail=detail, balances=bals)

    def accounts(self) -> List[Dict[str, Any]]:
        """Exchange accounts connected to this 3Commas profile."""
        return self._call("GET", "/ver1/accounts") or []

    def symbol_meta(self, symbol: str) -> SymbolMeta:
        # 3Commas routes to an underlying exchange and does not publish filters
        # in a usable form. Guessing tick/lot size here would produce rejected or
        # wrongly-sized orders, so refuse: size against the exchange adapter.
        raise VenueError(
            "3Commas does not expose instrument filters; take symbol_meta from "
            "the underlying exchange adapter and route execution here",
            venue=self.name)

    def balances(self) -> List[Balance]:
        if not self._account_id:
            accts = self.accounts()
            if not accts:
                return []
            self._account_id = accts[0].get("id")
        data = self._call("GET", "/ver1/accounts/%s/account_table_data" % self._account_id)
        out = []
        for row in data or []:
            total = float(row.get("position") or 0)
            if total:
                out.append(Balance(row.get("currency_code", "?"), total,
                                   float(row.get("on_orders") or 0) * -1 + total))
        return sorted(out, key=lambda b: -b.total)

    def positions(self) -> List[Position]:
        trades = self._call("GET", "/ver1/smart_trades/v2",
                            {"status": "active", "per_page": 100}) or []
        out = []
        for t in trades:
            pos = (t.get("position") or {})
            qty = float((pos.get("units") or {}).get("value") or 0)
            if not qty:
                continue
            out.append(Position(
                symbol=t.get("pair", ""), side=pos.get("type", ""), qty=qty,
                entry_price=float((pos.get("price") or {}).get("value") or 0),
                unrealized_pnl=float((t.get("profit") or {}).get("usd") or 0),
                venue_id=str(t.get("id") or ""), raw=t))
        return out

    # --------------------------------------------------------------- writes
    def _probe_by_note(self, client_order_id: str) -> Probe:
        """Has a smart trade tagged with this client_order_id already been
        placed? Three states, never a bare list scan.

        3Commas has NO clientOrderId. The marker lives in the note field, and
        the only check is scanning the smart-trade list — which is exactly the
        weakness: unlike a real idempotency key, a list read can be paginated,
        capped, or filtered, and a trade beyond what the venue returns is
        invisible. Reading one page and calling absence is how a trade at
        index 120 got a duplicate.

        So the rule is conservative and matches the ccxt probe: read a page; if
        the trade is in it, FOUND; if the page comes back FULL, the trade could
        be beyond it and absence is UNPROVEN — INDETERMINATE, never 'no'; only
        a SHORT page (fewer rows than we asked for) is a real, confirmed end of
        list and therefore a real absence. Consequence: on an account already
        holding more than _PROBE_PER_PAGE open smart trades, a placement stands
        aside as indeterminate rather than risk a double fill. That is the
        correct trade for a router whose idempotency cannot be trusted to a key.
        """
        try:
            rows = self._call("GET", "/ver1/smart_trades/v2",
                              {"status": "all", "per_page": _PROBE_PER_PAGE}) or []
        except VenueError as e:
            return Probe(PROBE_INDETERMINATE,
                         reason="smart-trade list unreadable: %s" % e,
                         retryable=e.retryable)
        for t in rows:
            if client_order_id in str(t.get("note") or ""):
                return Probe(PROBE_FOUND, order=t, reason="matched by note")
        if len(rows) >= _PROBE_PER_PAGE:
            return Probe(PROBE_INDETERMINATE,
                         reason="the smart-trade list returned a full page of "
                                "%d, so the trade could be beyond it"
                                % _PROBE_PER_PAGE)
        return Probe(PROBE_ABSENT,
                     reason="not present in a list of %d (fewer than a full "
                            "page)" % len(rows))

    def place_order(self, order: Order) -> OrderResult:
        if self.read_only:
            raise VenueReadOnly(
                "%s is read-only; enable trading for this venue first" % self.name,
                venue=self.name)
        if self.manages_positions:
            raise VenueError(
                "this 3Commas account is marked as bot-managed; the engine will "
                "not open a position a 3Commas bot also owns",
                venue=self.name)
        if not self._account_id:
            raise VenueError("no 3Commas account_id configured", venue=self.name)

        # Last gate before the wire: a non-finite qty or price must never reach
        # the venue. NaN passes every numeric guard upstream, so it stops here.
        for fld in ("qty", "limit_price", "stop_loss", "take_profit"):
            v = getattr(order, fld, None)
            if v is not None and _finite(v) is None:
                raise VenueError("%s: refusing to transmit a non-finite %s (%r)"
                                 % (self.name, fld, v), venue=self.name)
        if order.qty <= 0:
            raise VenueError("%s: refusing to transmit a non-positive qty (%r)"
                             % (self.name, order.qty), venue=self.name)

        # Idempotency before placing. INDETERMINATE must not be read as absent.
        probe = self._probe_by_note(order.client_order_id)
        if probe.outcome == PROBE_FOUND:
            return OrderResult(order.client_order_id, str(probe.order.get("id")),
                               "accepted", message="idempotent: already placed",
                               raw=probe.order)
        if probe.outcome == PROBE_INDETERMINATE:
            raise VenueIndeterminate(
                "%s: cannot verify whether %s was already placed, refusing to "
                "submit (%s)" % (self.name, order.client_order_id, probe.reason),
                venue=self.name, client_order_id=order.client_order_id)

        body: Dict[str, Any] = {
            "account_id": self._account_id,
            "pair": order.symbol,
            "note": order.client_order_id,
            "position": {"type": order.side, "units": {"value": str(order.qty)},
                         "order_type": order.order_type},
        }
        if order.limit_price is not None:
            body["position"]["price"] = {"value": str(order.limit_price)}
        if order.stop_loss is not None:
            body["stop_loss"] = {"enabled": True, "order_type": "market",
                                 "price": {"value": str(order.stop_loss)}}
        if order.take_profit is not None:
            body["take_profit"] = {"enabled": True, "steps": [
                {"order_type": "market", "price": {"value": str(order.take_profit),
                                                   "type": "last"},
                 "volume": 100}]}

        try:
            raw = self._call("POST", "/ver1/smart_trades/v2", body=body)
        except VenueError as e:
            # The POST may have landed. A blind retry on a transport error is
            # the double fill. Probe by note, and only re-raise as retryable if
            # the venue confirms the trade is NOT there.
            after = self._probe_by_note(order.client_order_id)
            if after.outcome == PROBE_FOUND:
                return OrderResult(order.client_order_id,
                                   str(after.order.get("id")), "accepted",
                                   message="landed despite transport error",
                                   raw=after.order)
            if after.is_absent:
                raise VenueError("3Commas: venue confirms trade not placed: %s"
                                 % e, retryable=True, venue=self.name, cause=e)
            raise VenueIndeterminate(
                "%s: smart trade %s may or may not have been placed — the POST "
                "failed (%s) and the probe could not settle it (%s). Reconcile "
                "before any retry." % (self.name, order.client_order_id, e,
                                       after.reason),
                venue=self.name, cause=e, client_order_id=order.client_order_id)
        return OrderResult(order.client_order_id, str(raw.get("id") or ""),
                           "accepted", raw=raw)

    def cancel(self, venue_order_id: str, symbol: str = "") -> bool:
        """True only when 3Commas confirms the smart trade is closed.

        close_by_market is a REQUEST to close at market; the trade is still
        working when the call returns. Returning True there tells the engine
        the position is flat while it is live. We read the trade back and
        confirm its status before claiming the cancel succeeded.
        """
        if self.read_only:
            raise VenueReadOnly("%s is read-only" % self.name, venue=self.name)
        self._call("POST",
                   "/ver1/smart_trades/v2/%s/close_by_market" % venue_order_id)
        # close_by_market is async; confirm the resulting state rather than
        # trust the acknowledgement.
        try:
            t = self._call("GET", "/ver1/smart_trades/v2/%s" % venue_order_id)
        except VenueError:
            return False        # cannot confirm -> do not claim success
        status = str((t or {}).get("status", {})
                     if isinstance((t or {}).get("status"), str)
                     else ((t or {}).get("status") or {}).get("type") or "").lower()
        return status in ("closed", "cancelled", "canceled", "finished")

    def _probe(self, client_order_id: str) -> Probe:   # test/introspection alias
        return self._probe_by_note(client_order_id)

    def stream_prices(self, symbols: List[str]) -> Iterator[Tick]:
        return iter(())      # price data comes from the exchange, not the router


register("3commas", ThreeCommasVenue)
