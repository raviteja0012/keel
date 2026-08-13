"""Venue adapters — the one door orders leave through.

The engine decides WHAT to do. An adapter only knows HOW to say it to a
particular venue. Nothing in here may size a position, widen a stop, or
decide whether a trade is allowed: those are engine rails and they stay
in engine.py. An adapter that makes a decision is a bug.

Every adapter is constructed from credentials held in the runtime DB
(see venues.py) and never from source or config.yaml.

Two properties are mandatory for anything that touches real money:

  idempotency   place_order() carries a caller-generated client_order_id.
                A dropped response must be safe to retry. Double fills are
                the most expensive bug class in automated trading and no
                downstream rail can undo one.

  read_only     A venue is read-only until trading is explicitly enabled
                for it. Adding credentials lets you SEE the account; it
                does not let the engine trade it.
"""
from dataclasses import dataclass, field
from typing import (Any, Dict, Iterator, List, NamedTuple, Optional, Protocol,
                    runtime_checkable)


class VenueError(RuntimeError):
    """Adapter could not complete the call. Carries whether a retry is safe.

    `http_status` is the venue's own status code when there was one, and None
    when the call never got an answer (DNS, timeout, connection refused). An
    adapter deciding whether an order exists needs that distinction: a status
    code is the venue saying something, None is the venue saying nothing, and
    the two must never be handled alike.
    """

    def __init__(self, message: str, retryable: bool = False,
                 venue: str = "", cause: Optional[BaseException] = None,
                 http_status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.venue = venue
        self.cause = cause
        self.http_status = http_status


class VenueReadOnly(VenueError):
    """Trading was attempted on a venue that has not been enabled for it."""


class VenueIndeterminate(VenueError):
    """We do not know whether the order exists, and therefore must not act.

    This is the exception that keeps the promise made by `idempotency` below.
    It is raised when a submission may or may not have landed and the probe
    that would settle it could not be completed. It is deliberately NOT
    retryable: a retry is exactly the action that turns "might have landed"
    into two positions.

    The only correct responses are to reconcile against the venue, or to ask
    a human. Both are slower than a retry. Both are cheaper than a double fill.
    """

    def __init__(self, message: str, venue: str = "",
                 cause: Optional[BaseException] = None,
                 http_status: Optional[int] = None,
                 client_order_id: str = ""):
        super().__init__(message, retryable=False, venue=venue, cause=cause,
                         http_status=http_status)
        self.client_order_id = client_order_id


# The idempotency probe has exactly three answers, and "indeterminate" is not a
# spelling of "absent". Every adapter that probes before or after submitting
# MUST use this vocabulary rather than Optional[Dict], because Optional collapses
# "the venue says no such order" and "I could not find out" into one value —
# and an adapter reading the second as the first is the single most expensive
# bug this codebase has produced. It shipped in webull (disarmed) and again in
# ccxt_venue (fixed), both times undetected by a full green test suite.
PROBE_FOUND = "found"
PROBE_ABSENT = "absent"
PROBE_INDETERMINATE = "indeterminate"


class Probe(NamedTuple):
    outcome: str                       # PROBE_FOUND | PROBE_ABSENT | PROBE_INDETERMINATE
    order: Optional[Dict[str, Any]] = None   # only ever set for PROBE_FOUND
    reason: str = ""                   # why, in words, for the refusal message
    retryable: bool = False            # is the *cause* of indeterminacy transient

    @property
    def is_absent(self) -> bool:
        """True only for a definite venue-confirmed absence. Read this rather
        than `not probe.order`, which is true for indeterminate too."""
        return self.outcome == PROBE_ABSENT


@dataclass(frozen=True)
class SymbolMeta:
    """Everything needed to size an order correctly on this venue. Getting any
    of these wrong is a real-money error, so an adapter that cannot supply them
    must raise rather than guess."""
    symbol: str
    venue_symbol: str                 # the venue's own spelling (BTC/USDT vs BTCUSDT)
    asset_class: str
    tick_size: float                  # minimum price increment
    lot_step: float                   # minimum quantity increment
    min_qty: float
    min_notional: float = 0.0
    maker_fee: float = 0.0
    taker_fee: float = 0.0
    contract_size: float = 1.0


@dataclass(frozen=True)
class Balance:
    currency: str
    total: float
    free: float
    used: float = 0.0


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str                         # "buy" | "sell"
    qty: float
    entry_price: float
    unrealized_pnl: float = 0.0
    venue_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Order:
    """An instruction the engine has already approved. The adapter transmits it."""
    client_order_id: str              # caller-generated; the idempotency key
    symbol: str
    side: str                         # "buy" | "sell"
    qty: float
    order_type: str = "market"        # "market" | "limit"
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reduce_only: bool = False


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str
    venue_order_id: str
    status: str                       # accepted|filled|partial|rejected|cancelled|unknown
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    ts: int


@dataclass(frozen=True)
class VenueHealth:
    name: str
    reachable: bool
    authenticated: bool
    read_only: bool
    latency_ms: Optional[int] = None
    clock_skew_s: Optional[float] = None
    detail: str = ""
    balances: List[Balance] = field(default_factory=list)


@runtime_checkable
class BrokerAdapter(Protocol):
    """What every venue must implement. Kept deliberately small: each method
    added here is a method every future venue has to get right."""

    name: str
    read_only: bool

    def health(self) -> VenueHealth:
        """Reachability, auth, latency and clock skew. Must never raise -- a
        dead venue is a health result, not an exception."""

    def symbol_meta(self, symbol: str) -> SymbolMeta: ...

    def balances(self) -> List[Balance]: ...

    def positions(self) -> List[Position]: ...

    def place_order(self, order: Order) -> OrderResult:
        """MUST be idempotent on order.client_order_id. Resubmitting the same
        id must return the existing order, never create a second one."""

    def cancel(self, venue_order_id: str, symbol: str = "") -> bool: ...

    def stream_prices(self, symbols: List[str]) -> Iterator[Tick]: ...


# ---------------------------------------------------------------- registry
_ADAPTERS: Dict[str, Any] = {}


def register(kind: str, factory: Any) -> None:
    """Register an adapter factory under a kind ('ccxt', '3commas', 'mt5')."""
    _ADAPTERS[kind] = factory


def kinds() -> List[str]:
    return sorted(_ADAPTERS)


def build(kind: str, config: Dict[str, Any]) -> BrokerAdapter:
    if kind not in _ADAPTERS:
        raise VenueError("unknown venue kind %r (have: %s)"
                         % (kind, ", ".join(kinds()) or "none"))
    return _ADAPTERS[kind](config)


# Import side-effect registration. Each adapter registers itself and is
# optional: a missing dependency disables that venue kind, it does not stop
# the engine.
try:
    from . import ccxt_venue          # noqa: F401
except ImportError:                   # pragma: no cover - optional dependency
    pass
try:
    from . import threecommas         # noqa: F401
except ImportError:                   # pragma: no cover - optional dependency
    pass
try:
    from . import webull              # noqa: F401
except ImportError:                   # pragma: no cover - optional dependency
    pass
