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

from . import (Balance, Order, OrderResult, Position, SymbolMeta, Tick,
               VenueError, VenueHealth, VenueReadOnly, register)

BASE = "https://api.3commas.io"
_PREFIX = "/public/api"


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

        # Idempotency: 3Commas has no clientOrderId, so check before placing.
        # note is the only field that round-trips, so the key lives there.
        for t in self._call("GET", "/ver1/smart_trades/v2",
                            {"status": "all", "per_page": 100}) or []:
            if order.client_order_id in str(t.get("note") or ""):
                return OrderResult(order.client_order_id, str(t.get("id")),
                                   "accepted", message="idempotent: already placed",
                                   raw=t)

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

        raw = self._call("POST", "/ver1/smart_trades/v2", body=body)
        return OrderResult(order.client_order_id, str(raw.get("id") or ""),
                           "accepted", raw=raw)

    def cancel(self, venue_order_id: str, symbol: str = "") -> bool:
        if self.read_only:
            raise VenueReadOnly("%s is read-only" % self.name, venue=self.name)
        self._call("POST", "/ver1/smart_trades/v2/%s/close_by_market" % venue_order_id)
        return True

    def stream_prices(self, symbols: List[str]) -> Iterator[Tick]:
        return iter(())      # price data comes from the exchange, not the router


register("3commas", ThreeCommasVenue)
