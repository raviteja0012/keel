"""Strategy hosts — platforms that run their OWN bots, which Keel merely triggers.

A BrokerAdapter is a venue Keel trades through: Keel decides, sizes, places and
manages, and the adapter only transmits. A StrategyHost is the opposite shape.
Cryptohopper, Bitsgap, Altrady and 3Commas each run strategies of their own —
grid bots, DCA bots, signal bots. Those are strategies Keel does not have to
write, which is the entire reason to integrate them.

The distinction is not cosmetic, and modelling a bot platform as a broker is
how you get two systems managing one position. Keel's engine sizes to a risk
budget and manages a stop; a grid bot ladders in and out on its own schedule.
Point both at the same position and neither is in control: Keel's stop moves
under the grid's feet, the grid's rungs fire against Keel's sizing, and the
loss is bounded by nothing.

So the rule this module exists to enforce, in one line:

    ONE POSITION HAS EXACTLY ONE OWNER, AND FOR A HOSTED BOT THAT OWNER IS THE
    HOST. Keel starts it, stops it, and watches it. Keel never manages it.

Positions read back from a host are REPORTED, never MANAGED. They count toward
exposure, drawdown and the kill switches — money at risk is money at risk,
whoever is steering — but no engine rail may adjust, hedge or close them. The
only controls Keel has over a hosted position are start and stop.

Two properties worth naming, because they make this integration shape safer
than order routing rather than merely different:

  naturally idempotent   Starting a running bot is a no-op; stopping a stopped
                         one likewise. Bot control has no equivalent of the
                         double fill, which is the defect class that has cost
                         this codebase the most. A dropped response to
                         start_bot can be safely retried. That is a real and
                         unusual luxury, and it is why triggering a bot is a
                         better integration than routing an order through the
                         same platform.

  no sizing surrender    A host that lets Keel submit an order WITHOUT a size,
                         and then picks the size itself, is not a host being
                         helpful — it is a second decision-maker. Adapters here
                         must not use those endpoints. If Keel is placing the
                         order, the venue is a broker and belongs in a
                         BrokerAdapter; if the host is placing it, Keel does
                         not get to specify anything but start and stop.
"""
from dataclasses import dataclass, field
from typing import (Any, Dict, List, NamedTuple, Optional, Protocol,
                    runtime_checkable)

from . import Position, VenueError, VenueHealth


class HostError(VenueError):
    """A strategy host could not complete the call."""


class HostReadOnly(HostError):
    """Bot control was attempted on a host not enabled for it.

    Reading a host's bots and positions is always allowed once credentials
    exist. Starting or stopping one is a state change on real money and
    requires the same explicit arming a venue does.
    """


# What stopping a bot actually does to its open positions. Vendors differ, the
# difference is expensive, and nothing in an API response announces it.
#
# If Keel calls stop_bot believing the book goes flat, and the host instead
# parks an open position and walks away, that position is now unmanaged: no bot
# steering it and no Keel rail permitted to touch it. That is the worst state
# in this whole design, so the disposition is declared per adapter and Keel
# reasons about it explicitly rather than discovering it during a drawdown.
STOP_UNKNOWN = "unknown"            # not established — treat as ORPHANS
STOP_CLOSES_POSITIONS = "closes"    # stopping liquidates; the book goes flat
STOP_ORPHANS_POSITIONS = "orphans"  # stopping halts new entries, leaves what is open


@dataclass(frozen=True)
class Bot:
    """One strategy configured on a host."""
    bot_id: str
    name: str
    host: str                         # which host it lives on
    running: bool
    strategy: str = ""                # "grid" | "dca" | "signal" | vendor's own
    pairs: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BotState:
    """A bot's current condition. Everything here is read-only by construction.

    `positions` are the host's positions. They are visible to Keel's exposure
    and drawdown maths and invisible to every other rail.
    """
    bot_id: str
    running: bool
    positions: List[Position] = field(default_factory=list)
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    as_of: int = 0                    # epoch seconds this was read
    detail: str = ""

    @property
    def valued(self) -> bool:
        """Whether P&L is actually known.

        None means the host did not tell us, and None must not be summed as
        zero. An unvalued hosted position is exactly as dangerous as an
        unvalued local one — the difference is only that we cannot close it.
        """
        return self.unrealized_pnl is not None


class BotAction(NamedTuple):
    """Result of a start or stop. `changed` is False for a no-op, which is a
    success: the bot was already in the requested state."""
    bot_id: str
    running: bool
    changed: bool
    detail: str = ""


@runtime_checkable
class StrategyHost(Protocol):
    """What every bot platform must implement.

    Note what is absent and must stay absent: place_order and symbol_meta.
    A host that grows those is a broker, and belongs behind BrokerAdapter with
    the idempotency obligations that carries.
    """

    name: str
    read_only: bool                   # False only when bot control is armed

    #: One of the STOP_* constants above. Declared, not guessed.
    stop_disposition: str

    def health(self) -> VenueHealth:
        """Reachability and auth. Must never raise: a dead host is a health
        result, not an exception."""

    def bots(self) -> List[Bot]:
        """Every bot configured on this account."""

    def bot_state(self, bot_id: str) -> BotState:
        """Current state of one bot, including its open positions."""

    def start_bot(self, bot_id: str) -> BotAction:
        """Start a bot. Idempotent: starting a running bot returns
        changed=False, not an error."""

    def stop_bot(self, bot_id: str) -> BotAction:
        """Stop a bot. Idempotent. See stop_disposition for what this does to
        open positions — it does not necessarily flatten them."""


# ---------------------------------------------------------------- registry
#
# Deliberately separate from the BrokerAdapter registry in __init__.py. Two
# registries means `build_host("cryptohopper")` and `build("cryptohopper")`
# cannot be confused for one another, and a host can never be handed to code
# expecting something it can place an order through.

_HOSTS: Dict[str, Any] = {}


def register_host(kind: str, factory: Any) -> None:
    _HOSTS[kind] = factory


def host_kinds() -> List[str]:
    return sorted(_HOSTS)


def build_host(kind: str, config: Dict[str, Any]) -> StrategyHost:
    if kind not in _HOSTS:
        raise HostError("unknown strategy host %r (have: %s)"
                        % (kind, ", ".join(host_kinds()) or "none"))
    return _HOSTS[kind](config)


def hosted_exposure(states: List[BotState]) -> "HostedExposure":
    """Total money at risk on hosted bots, and how much of it we cannot value.

    The second number is the point. `sum(s.unrealized_pnl or 0)` would report a
    host we cannot read as a host that is flat, which is the same mistake this
    codebase has now made four times in different clothes. Callers get the
    count and decide; they do not get a reassuring total that hides it.
    """
    total, unvalued, positions = 0.0, 0, 0
    for s in states:
        positions += len(s.positions)
        if s.valued:
            total += float(s.unrealized_pnl or 0.0)
        else:
            unvalued += 1
    return HostedExposure(round(total, 2), unvalued, positions)


class HostedExposure(NamedTuple):
    unrealized_pnl: float
    unvalued_bots: int                # bots whose P&L could not be read
    open_positions: int

    @property
    def trustworthy(self) -> bool:
        return self.unvalued_bots == 0
