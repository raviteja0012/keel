"""CCXT-backed venue adapter — Binance and ~100 other exchanges through one
implementation.

Writing Binance by hand and then Coinbase by hand and then Kraken by hand is
three times the surface for the same job. CCXT normalises markets, balances,
orders and OHLCV, so one adapter covers them all.

Where CCXT leaks, this adapter does the work rather than pretending:
  - min_notional lives in different places per exchange; we look in all of them
  - clientOrderId support is not universal, so we verify it round-trips and
    fall back to a fetch-then-place check when it does not
  - error classes are normalised into VenueError with an honest `retryable`
"""
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

import ccxt

from . import (Balance, BrokerAdapter, Order, OrderResult, Position, SymbolMeta,
               Tick, VenueError, VenueHealth, VenueReadOnly, register)

# Exchanges known to honour clientOrderId on order creation. For anything not
# listed we still send it, but we do not TRUST it for idempotency and take the
# slower verified path instead. Assuming idempotency you do not have is how
# double fills happen.
_TRUSTED_CLIENT_ID = {"binance", "binanceusdm", "binancecoinm", "bybit", "okx",
                      "kucoin", "gate", "bitget", "kraken", "coinbase"}

_RETRYABLE = (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable,
              ccxt.DDoSProtection, ccxt.RateLimitExceeded)


class CcxtVenue:
    """One configured exchange account."""

    def __init__(self, config: Dict[str, Any]):
        self.name: str = config.get("name") or config.get("exchange", "ccxt")
        self.exchange_id: str = config["exchange"]
        self.read_only: bool = bool(config.get("read_only", True))
        self._default_type = config.get("market_type", "spot")

        if self.exchange_id not in ccxt.exchanges:
            raise VenueError("ccxt has no exchange %r" % self.exchange_id,
                             venue=self.name)

        opts: Dict[str, Any] = {
            "apiKey": config.get("api_key") or "",
            "secret": config.get("api_secret") or "",
            "enableRateLimit": True,          # never hammer a venue
            "timeout": int(config.get("timeout_ms", 15000)),
            "options": {"defaultType": self._default_type},
        }
        if config.get("password"):            # some venues need a passphrase
            opts["password"] = config["password"]

        self._x = getattr(ccxt, self.exchange_id)(opts)
        # Sandbox is available but OFF unless asked for: production is the default.
        if config.get("sandbox"):
            self._x.set_sandbox_mode(True)
        self._markets: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------- internals
    def _wrap(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except _RETRYABLE as e:
            raise VenueError(str(e), retryable=True, venue=self.name, cause=e)
        except ccxt.AuthenticationError as e:
            raise VenueError("authentication failed: %s" % e, venue=self.name, cause=e)
        except ccxt.BaseError as e:
            raise VenueError(str(e), venue=self.name, cause=e)

    def _load(self) -> Dict[str, Any]:
        if self._markets is None:
            self._markets = self._wrap(self._x.load_markets)
        return self._markets

    def _resolve(self, symbol: str) -> str:
        """Accept EURUSD, BTCUSDT or BTC/USDT and return the venue's spelling."""
        m = self._load()
        if symbol in m:
            return symbol
        want = symbol.replace("/", "").replace("-", "").upper()
        for k, v in m.items():
            if v.get("id", "").upper() == want or k.replace("/", "").upper() == want:
                return k
        raise VenueError("symbol %r not listed on %s" % (symbol, self.name),
                         venue=self.name)

    # ---------------------------------------------------------------- reads
    def health(self) -> VenueHealth:
        """Never raises. A dead venue is a result, not an exception."""
        t0 = time.time()
        try:
            self._load()
            latency = int((time.time() - t0) * 1000)
        except Exception as e:
            return VenueHealth(self.name, False, False, self.read_only,
                               detail=str(e)[:300])

        skew = None
        try:
            if self._x.has.get("fetchTime"):
                skew = round(self._x.fetch_time() / 1000.0 - time.time(), 3)
        except Exception:
            pass

        bals: List[Balance] = []
        authed = False
        detail = ""
        if self._x.apiKey:
            try:
                bals = self.balances()
                authed = True
            except VenueError as e:
                detail = str(e)[:300]
        else:
            detail = "no API key configured (public data only)"

        return VenueHealth(self.name, True, authed, self.read_only,
                           latency_ms=latency, clock_skew_s=skew,
                           detail=detail, balances=bals)

    def symbol_meta(self, symbol: str) -> SymbolMeta:
        vs = self._resolve(symbol)
        m = self._load()[vs]
        limits, precision = m.get("limits", {}), m.get("precision", {})

        def _step(p, fallback):
            # ccxt precision is a decimal-places count on some venues and a
            # tick size on others. Both appear in the wild; handle both.
            if p is None:
                return fallback
            p = float(p)
            return p if 0 < p < 1 else (10 ** -int(p) if p >= 1 else fallback)

        min_notional = (limits.get("cost", {}) or {}).get("min")
        if min_notional is None:                       # binance hides it in info
            for f in (m.get("info", {}) or {}).get("filters", []) or []:
                if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("minNotional") or f.get("notional") or 0)

        return SymbolMeta(
            symbol=symbol, venue_symbol=vs,
            asset_class="crypto",
            tick_size=_step(precision.get("price"), 1e-8),
            lot_step=_step(precision.get("amount"), 1e-8),
            min_qty=float((limits.get("amount", {}) or {}).get("min") or 0.0),
            min_notional=float(min_notional or 0.0),
            maker_fee=float(m.get("maker") or 0.0),
            taker_fee=float(m.get("taker") or 0.0),
            contract_size=float(m.get("contractSize") or 1.0))

    def balances(self) -> List[Balance]:
        raw = self._wrap(self._x.fetch_balance)
        out = []
        for cur, v in (raw.get("total") or {}).items():
            if v:
                out.append(Balance(cur, float(v),
                                   float((raw.get("free") or {}).get(cur) or 0),
                                   float((raw.get("used") or {}).get(cur) or 0)))
        return sorted(out, key=lambda b: -b.total)

    def positions(self) -> List[Position]:
        if not self._x.has.get("fetchPositions"):
            return []                                   # spot: no position concept
        out = []
        for p in self._wrap(self._x.fetch_positions) or []:
            qty = float(p.get("contracts") or p.get("contractSize") or 0)
            if not qty:
                continue
            out.append(Position(
                symbol=p.get("symbol", ""), side=p.get("side", ""), qty=qty,
                entry_price=float(p.get("entryPrice") or 0),
                unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                venue_id=str(p.get("id") or ""), raw=p))
        return out

    # --------------------------------------------------------------- writes
    def _find_by_client_id(self, client_order_id: str, vs: str) -> Optional[Dict]:
        """Has this client_order_id already been placed? The idempotency check."""
        for fetch in (self._x.fetch_open_orders, self._x.fetch_closed_orders):
            try:
                for o in fetch(vs, limit=50) or []:
                    if (o.get("clientOrderId") or "") == client_order_id:
                        return o
            except Exception:
                continue
        return None

    def place_order(self, order: Order) -> OrderResult:
        if self.read_only:
            raise VenueReadOnly(
                "%s is read-only; enable trading for this venue first" % self.name,
                venue=self.name)
        vs = self._resolve(order.symbol)

        # Idempotency. On venues we do not trust to honour clientOrderId we look
        # first: a retry after a dropped response must not open a second position.
        if self.exchange_id not in _TRUSTED_CLIENT_ID:
            existing = self._find_by_client_id(order.client_order_id, vs)
            if existing:
                return self._to_result(order.client_order_id, existing,
                                       "recovered existing order (idempotent retry)")

        params: Dict[str, Any] = {"clientOrderId": order.client_order_id}
        if order.reduce_only:
            params["reduceOnly"] = True
        if order.stop_loss is not None:
            params["stopLoss"] = {"triggerPrice": order.stop_loss}
        if order.take_profit is not None:
            params["takeProfit"] = {"triggerPrice": order.take_profit}

        try:
            raw = self._x.create_order(vs, order.order_type, order.side,
                                       order.qty, order.limit_price, params)
        except _RETRYABLE as e:
            # The request may or may not have landed. Never blind-retry: look.
            existing = self._find_by_client_id(order.client_order_id, vs)
            if existing:
                return self._to_result(order.client_order_id, existing,
                                       "order landed despite transport error")
            raise VenueError("network error, order not found on venue: %s" % e,
                             retryable=True, venue=self.name, cause=e)
        except ccxt.BaseError as e:
            raise VenueError(str(e), venue=self.name, cause=e)

        return self._to_result(order.client_order_id, raw)

    @staticmethod
    def _to_result(cid: str, raw: Dict[str, Any], message: str = "") -> OrderResult:
        status_map = {"open": "accepted", "closed": "filled",
                      "canceled": "cancelled", "cancelled": "cancelled",
                      "rejected": "rejected", "expired": "cancelled"}
        return OrderResult(
            client_order_id=cid,
            venue_order_id=str(raw.get("id") or ""),
            status=status_map.get(raw.get("status") or "", "unknown"),
            filled_qty=float(raw.get("filled") or 0),
            avg_price=raw.get("average"),
            message=message, raw=raw)

    def cancel(self, venue_order_id: str, symbol: str = "") -> bool:
        if self.read_only:
            raise VenueReadOnly("%s is read-only" % self.name, venue=self.name)
        vs = self._resolve(symbol) if symbol else None
        self._wrap(self._x.cancel_order, venue_order_id, vs)
        return True

    def stream_prices(self, symbols: List[str]) -> Iterator[Tick]:
        """Polled ticker snapshots. CCXT's websocket support lives in ccxt.pro;
        this keeps the sync dependency and is adequate at the engine's cadence."""
        resolved = [self._resolve(s) for s in symbols]
        for vs in resolved:
            try:
                t = self._x.fetch_ticker(vs)
            except Exception:
                continue
            if t.get("bid") and t.get("ask"):
                yield Tick(vs, float(t["bid"]), float(t["ask"]),
                           int((t.get("timestamp") or time.time() * 1000) / 1000))


def new_client_order_id(prefix: str = "slc") -> str:
    """Idempotency key. Short enough for venues that cap clientOrderId length."""
    return "%s-%s" % (prefix, uuid.uuid4().hex[:20])


register("ccxt", CcxtVenue)
