"""3Commas bots — a STRATEGY HOST, not a venue.

brokers/threecommas.py wires 3Commas as an ORDER VENUE: the engine decides
and 3Commas transmits. This module is the other integration shape for the
same platform: 3Commas' own DCA and Grid bots run their strategies and OWN
their positions, and Keel only starts them, stops them and reads their
state. The two shapes must never point at the same account-position; see
strategy_host.py for the one-owner rule this module exists to honour.

Written against https://developers.3commas.io read in August 2026.
Everything below that says "documented" was read there; anything marked
"undocumented" was looked for and not found, which is a different and much
more dangerous thing.

AUTH — HMAC-SHA256, verified against
https://developers.3commas.io/quick-start/signing-a-request-using-hmac-sha256/
  The signed message is the FULL request path including the /public/api
  prefix and the query string, concatenated with the request body:
  "totalParams consists of the query string (parameters in the URL)
  concatenated with the request body". Their worked example signs
  "/public/api/ver1/users/change_mode?mode=paper" — the whole prefixed path,
  not just the query — which matches what brokers/threecommas.py already
  does. The key rides in an "Apikey" header (the docs' current spelling;
  brokers/threecommas.py sends "APIKEY", and header names are
  case-insensitive on the wire, so both authenticate) and the hex digest in
  "Signature". The secret keys the HMAC and appears nowhere else: not in a
  URL, header, log, repr or error string — _redact() scrubs every message
  that leaves this adapter, because what a venue or proxy echoes back at us
  is not under our control, but what we put in an exception is.

ENDPOINTS — the complete allowlist, each cited (scope in brackets)
  GET  /ver1/bots                    list DCA bots       [BOTS_READ]
       developers.3commas.io/dca-bot/get-the-list-of-dca-bots
       (limit/offset paging; per-bot: id, name, is_enabled, pairs, ...)
  GET  /ver1/bots/{id}/show          one DCA bot         [BOTS_READ]
       developers.3commas.io/dca-bot — "Get DCA Bot".
  POST /ver1/bots/{id}/enable        start a DCA bot     [BOTS_WRITE]
       developers.3commas.io/dca-bot/enable-dca-bot — 201, returns the bot
       entity with is_enabled=true.
  POST /ver1/bots/{id}/disable       stop a DCA bot      [BOTS_WRITE]
       developers.3commas.io/dca-bot/disable-dca-bot — 201, bot entity.
  GET  /ver1/deals                   deals, ?bot_id=     [BOTS_READ]
       developers.3commas.io/dca-bot/deals/get-list-of-deals
       (limit max 1000 default 50, offset; per-deal: pair, status, strategy,
       bought_amount, bought_average_price, actual_usd_profit, ...)
  GET  /ver1/bots/{id}/deals_stats   per-bot P&L         [BOTS_READ]
       developers.3commas.io/dca-bot/dca-bot-deals-stats — documents
       active_deals_usd_profit and completed_deals_usd_profit, in USD.
  GET  /ver1/grid_bots               list grid bots      [BOTS_READ]
       developers.3commas.io/grid-bot/get-the-list-of-grid-bots
       (limit/offset listed; bare array of grid bot entities)
  GET  /ver1/grid_bots/{id}          one grid bot        [BOTS_WRITE per docs]
       developers.3commas.io/grid-bot — "Get Grid Bot". Note the docs mark
       this READ endpoint BOTS_WRITE; that is the vendor's scoping, cited
       as found.
  POST /ver1/grid_bots/{id}/enable   start a grid bot    [BOTS_WRITE]
  POST /ver1/grid_bots/{id}/disable  stop a grid bot     [BOTS_WRITE]
       developers.3commas.io/grid-bot — "Enable/Disable Grid Bot".

WHY A HARDCODED ALLOWLIST IS THE TRANSPORT
  3Commas' credential scopes are BOTS_READ / BOTS_WRITE, and BOTS_WRITE —
  the scope start/stop requires — is the SAME scope that authorises
  panic_sell_all_deals, cancel_all_deals, deal editing, and bot
  create/update/delete. The platform cannot express "may start and stop,
  may never manage", so this adapter expresses it: the one private request
  method checks every call against _ALLOWED before any network I/O, and
  the frozenset holds exactly the ten read/start/stop endpoints above.
  Order-placement and deal-management endpoints exist on this platform;
  they are deliberately absent here and must stay absent.

STOP DISPOSITION — declared from evidence (R6)
  help.3commas.io article 6722124, "DCA Bot: Manage your DCA bots": "If you
  disable the bot, all active trades will still run until canceled or
  closed manually." Disabling stops NEW deals; deals already open keep
  running under 3Commas' own deal management — their take-profit and
  safety-order logic continues — until they close. That is
  STOP_ORPHANS_POSITIONS in our vocabulary, with one point worth naming:
  the PLATFORM, not nobody, keeps managing the orphans, which is why
  orphaning is an acceptable state here rather than an emergency. For grid
  bots the docs do not state what disable does to acquired inventory
  (undocumented); orphans is already the conservative reading, so the
  declaration stands for both families.

BOT IDS
  DCA bots and grid bots are separate id spaces served by separate endpoint
  families, so ids are namespaced: "dca:123" and "grid:456". A bare number
  is ambiguous between the two and is refused rather than guessed.
"""
import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from . import Position, VenueHealth
from .strategy_host import (STOP_ORPHANS_POSITIONS, Bot, BotAction, BotState,
                            HostError, HostReadOnly, register_host)

BASE_URL = "https://api.3commas.io"
_PREFIX = "/public/api"

# The transport allowlist. (METHOD, path template), exact-match checked in
# _request before any network I/O. Every entry is cited in the module
# docstring. NOT here, ever: panic_sell_all_deals, cancel_all_deals,
# create/update/delete bot, anything under smart_trades — those are how a
# "see but never manage" credential turns into a market dump.
_ALLOWED = frozenset({
    ("GET", "/ver1/bots"),
    ("GET", "/ver1/bots/{id}/show"),
    ("POST", "/ver1/bots/{id}/enable"),
    ("POST", "/ver1/bots/{id}/disable"),
    ("GET", "/ver1/deals"),
    ("GET", "/ver1/bots/{id}/deals_stats"),
    ("GET", "/ver1/grid_bots"),
    ("GET", "/ver1/grid_bots/{id}"),
    ("POST", "/ver1/grid_bots/{id}/enable"),
    ("POST", "/ver1/grid_bots/{id}/disable"),
})

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Documented page caps: the DCA list docs cap limit at 100; /ver1/deals takes
# limit max 1000, default 50; /ver1/grid_bots lists limit/offset without
# stating a max, so the conservative 100 is used for it too.
_PAGE_LIMIT_BOTS = 100
_PAGE_LIMIT_DEALS = 1000
_MAX_PAGES = 50

# Quote currencies close enough to USD to be summed into USD exposure. Used
# only for the grid-bot unrealized number, whose unit the vendor does not
# document — see _grid_state.
_DOLLAR_QUOTES = frozenset({"USD", "USDT", "USDC", "BUSD", "TUSD", "DAI",
                            "FDUSD", "USDD", "PYUSD"})


class ThreeCommasBots:
    """3Commas DCA and grid bots behind the StrategyHost contract.

    Config: {name, api_key, api_secret, read_only}. read_only defaults to
    True: credentials let Keel SEE the bots; starting and stopping them is a
    state change on real money and must be armed deliberately.
    """

    # Evidence in the module docstring: disabling stops NEW deals, and deals
    # already open keep running under 3Commas' management until they close.
    stop_disposition = STOP_ORPHANS_POSITIONS

    def __init__(self, config: Dict[str, Any]):
        self.name: str = config.get("name") or "3commas-bots"
        self.read_only: bool = bool(config.get("read_only", True))
        self._key: str = config.get("api_key") or ""
        self._secret: str = config.get("api_secret") or ""
        if not self._key or not self._secret:
            raise HostError("3commas-bots needs api_key and api_secret",
                            venue=self.name)
        self._timeout = float(config.get("timeout_s", 10))
        self._session = requests.Session()

    def __repr__(self) -> str:
        # No credential material, not even a suffix: reprs end up in logs.
        return "<ThreeCommasBots %s read_only=%s>" % (self.name, self.read_only)

    # ------------------------------------------------------------- transport

    def _redact(self, text: Any) -> str:
        """Scrub credentials from anything that leaves this adapter.

        Error messages include response bodies and transport exception text,
        and whether the venue, a proxy, or a library echoes our own header
        back is not under our control. What we put in an exception is.
        Redact BEFORE truncating, so a cut cannot leave a partial secret.
        """
        out = str(text)
        for cred in (self._secret, self._key):
            if cred:
                out = out.replace(cred, "***")
        return out

    def _request(self, method: str, template: str,
                 bot_id: Optional[int] = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        """The ONE way out of this adapter, and where "see but never manage"
        is enforced. 3Commas cannot scope a key to start/stop-only (see the
        module docstring), so the allowlist check happens here, before any
        network I/O, against the exact (METHOD, template) pair.
        """
        if (method, template) not in _ALLOWED:
            raise HostError(
                "%s %s is not in the 3commas-bots allowlist; this adapter "
                "only reads bots and starts or stops them" % (method, template),
                venue=self.name)

        if "{id}" in template:
            # The id is interpolated into a signed path. Accept nothing but a
            # positive int, so no id can smuggle extra path segments or query
            # keys past the allowlist check above. bool is excluded because
            # it is an int subclass and True would spell bot 1.
            if (not isinstance(bot_id, int) or isinstance(bot_id, bool)
                    or bot_id <= 0):
                raise HostError("bot id must be a positive integer, got %r"
                                % (bot_id,), venue=self.name)
            path = template.replace("{id}", str(bot_id))
        else:
            path = template

        # Built once, sorted for determinism; the identical string is signed
        # and requested, so the two cannot drift apart.
        qs = ("?" + urlencode(sorted(params.items()))) if params else ""
        signed_path = _PREFIX + path + qs
        # Documented signing rule: HMAC-SHA256 hex over the full prefixed
        # path with query, concatenated with the body. Our POSTs (enable/
        # disable) carry no body, so the message is the path alone.
        sig = hmac.new(self._secret.encode("utf-8"),
                       signed_path.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {"Apikey": self._key, "Signature": sig}

        try:
            r = self._session.request(method, BASE_URL + signed_path,
                                      headers=headers, data=None,
                                      timeout=self._timeout)
        except requests.RequestException as e:
            # No answer at all. `from None` on purpose: the original
            # exception's text is already folded in (redacted), and chaining
            # it would let a printed traceback resurrect the unredacted one.
            raise HostError("3commas transport failure on %s %s: %s"
                            % (method, path, self._redact(e)),
                            retryable=True, venue=self.name,
                            http_status=None) from None

        if r.status_code >= 400:
            raise HostError(
                "3commas %s on %s %s: %s"
                % (r.status_code, method, path,
                   self._redact(r.text or "")[:300]),
                retryable=r.status_code in _RETRYABLE_STATUS,
                venue=self.name, http_status=r.status_code)
        if not r.content:
            # Every allowlisted 3commas endpoint returns a body: the reads
            # return the bot object or a JSON array, and enable/disable return
            # the updated bot. So an empty 2xx body is never legitimate here.
            # Returning {} let a deals read decay through `or []` into an empty
            # page — a bot reported flat because the response was truncated,
            # not because it holds nothing. A read that must have content and
            # got none is a failure, not an empty book (R1).
            raise HostError(
                "3commas returned an empty body on %s %s where a result was "
                "required — refusing to read that as 'no bots' or 'no deals'"
                % (method, path), retryable=True, venue=self.name,
                http_status=r.status_code)
        try:
            return r.json()
        except ValueError:
            raise HostError("3commas returned non-JSON on %s %s"
                            % (method, path), venue=self.name,
                            http_status=r.status_code) from None

    def _paged(self, template: str, params: Dict[str, Any], limit: int,
               what: str) -> List[Dict[str, Any]]:
        """Offset pagination that fails closed.

        If the page budget runs out while pages are still coming back full,
        the tail is UNREAD, not empty — and a bot or deal missing from a
        silently truncated list would escape exposure accounting entirely.
        Raising is the only honest answer (R1).
        """
        rows: List[Dict[str, Any]] = []
        for page in range(_MAX_PAGES):
            batch = self._request("GET", template,
                                  params=dict(params, limit=limit,
                                              offset=page * limit)) or []
            if not isinstance(batch, list):
                raise HostError("3commas returned a non-list for %s" % what,
                                venue=self.name)
            rows.extend(batch)
            if len(batch) < limit:
                return rows
        raise HostError(
            "%s did not end within %d pages of %d rows; refusing to return "
            "a silently truncated list" % (what, _MAX_PAGES, limit),
            venue=self.name)

    # ----------------------------------------------------------------- reads

    def health(self) -> VenueHealth:
        """Never raises: a dead host is a health result, not an exception.

        Probes GET /ver1/bots (limit=1) — the cheapest allowlisted call that
        exercises both reachability and the HMAC credential.
        """
        t0 = time.time()
        try:
            self._request("GET", "/ver1/bots", params={"limit": 1})
        except HostError as e:
            # A status code means the venue answered (reachable, but not
            # provably authenticated); None means it never answered at all.
            return VenueHealth(name=self.name,
                               reachable=e.http_status is not None,
                               authenticated=False, read_only=self.read_only,
                               detail=self._redact(e))
        except Exception as e:                            # noqa: BLE001
            return VenueHealth(name=self.name, reachable=False,
                               authenticated=False, read_only=self.read_only,
                               detail=self._redact("unexpected: %s" % e))
        return VenueHealth(name=self.name, reachable=True, authenticated=True,
                           read_only=self.read_only,
                           latency_ms=int((time.time() - t0) * 1000),
                           detail="bots endpoint readable")

    def bots(self) -> List[Bot]:
        """Every DCA bot and grid bot on the account, one list.

        Either family failing to read raises rather than returning the other
        half: a list silently missing a whole bot family would let that
        family's exposure escape the kill switches (R1).
        """
        out: List[Bot] = []
        for row in self._paged("/ver1/bots", {}, _PAGE_LIMIT_BOTS,
                               "DCA bot list"):
            out.append(Bot(bot_id="dca:%d" % self._id_of(row, "DCA bot"),
                           name=str(row.get("name") or ""),
                           host=self.name,
                           running=self._enabled_of(row, "DCA bot"),
                           strategy="dca",
                           pairs=[str(p) for p in (row.get("pairs") or [])],
                           raw=row))
        for row in self._paged("/ver1/grid_bots", {}, _PAGE_LIMIT_BOTS,
                               "grid bot list"):
            pair = str(row.get("pair") or "")
            out.append(Bot(bot_id="grid:%d" % self._id_of(row, "grid bot"),
                           name=str(row.get("name") or ""),
                           host=self.name,
                           running=self._enabled_of(row, "grid bot"),
                           strategy="grid",
                           pairs=[pair] if pair else [],
                           raw=row))
        return out

    def bot_state(self, bot_id: str) -> BotState:
        family, num = self._split(bot_id)
        if family == "dca":
            return self._dca_state(bot_id, num)
        return self._grid_state(bot_id, num)

    def _dca_state(self, bot_id: str, num: int) -> BotState:
        # An unreadable show or deals answer RAISES (R1): "could not fetch"
        # must never come back spelled "running=False" or "no positions".
        show = self._request("GET", "/ver1/bots/{id}/show", bot_id=num) or {}
        running = self._enabled_of(show, "DCA bot %d" % num)

        # scope=active narrows to open deals; documented on the deals list
        # endpoint. If this read fails the whole state read fails — a book
        # we cannot read is not a flat book.
        deals = self._paged("/ver1/deals",
                            {"bot_id": num, "scope": "active"},
                            _PAGE_LIMIT_DEALS,
                            "active deals of DCA bot %d" % num)
        positions = [self._deal_position(d) for d in deals]

        detail = str(show.get("name") or "")
        realized: Optional[float] = None
        unrealized: Optional[float] = None
        try:
            stats = self._request("GET", "/ver1/bots/{id}/deals_stats",
                                  bot_id=num) or {}
        except HostError as e:
            # R1: a bot whose P&L cannot be read gets None, never 0. The
            # state we DID read is still returned — hosted_exposure counts
            # this bot as unvalued rather than flat, which is the point.
            detail = ((detail + " | ") if detail else "") + \
                "P&L unreadable: %s" % self._redact(e)
        else:
            # Documented USD fields on deals_stats (see module docstring).
            unrealized = _num(stats.get("active_deals_usd_profit"))
            realized = _num(stats.get("completed_deals_usd_profit"))

        return BotState(bot_id=bot_id, running=running, positions=positions,
                        realized_pnl=realized, unrealized_pnl=unrealized,
                        as_of=int(time.time()), detail=detail)

    def _grid_state(self, bot_id: str, num: int) -> BotState:
        g = self._request("GET", "/ver1/grid_bots/{id}", bot_id=num) or {}
        running = self._enabled_of(g, "grid bot %d" % num)
        pair = str(g.get("pair") or "")

        # Documented on the grid bot entity: current_profit is realized
        # profit in the QUOTE currency, current_profit_usd its USD
        # conversion. unrealized_profit_loss is documented only as paper
        # gains/losses on open positions with NO unit stated (undocumented);
        # it sits beside the quote-currency fields, so quote currency is the
        # likely unit. A quote-currency number summed into USD exposure is a
        # real-money error for a BTC-quoted grid, so the figure is reported
        # only when the quote is dollar-pegged, and left None — unvalued and
        # visible, not zero — otherwise (R1).
        quote = pair.split("_", 1)[0].upper() if "_" in pair else ""
        unrealized: Optional[float] = None
        detail = str(g.get("name") or "")
        if quote in _DOLLAR_QUOTES:
            unrealized = _num(g.get("unrealized_profit_loss"))
        else:
            detail = ((detail + " | ") if detail else "") + (
                "unrealized P&L withheld: quote %r is not dollar-pegged and "
                "the venue does not document the unit of "
                "unrealized_profit_loss" % (quote or pair))
        realized = _num(g.get("current_profit_usd"))

        # The grid bot API does not itemize held inventory as positions
        # (only bought_volume/sold_volume totals are published), so none are
        # invented here; the bot-level P&L above carries the risk signal.
        return BotState(bot_id=bot_id, running=running, positions=[],
                        realized_pnl=realized, unrealized_pnl=unrealized,
                        as_of=int(time.time()), detail=detail)

    def _deal_position(self, deal: Dict[str, Any]) -> Position:
        """One active DCA deal, REPORTED (never managed).

        The bot-level USD figure from deals_stats is the authoritative P&L;
        Position's float fields cannot say "unknown", so unparseable numbers
        are carried as 0.0 with the truth flagged in raw — and the deal is
        reported rather than dropped, because dropping it would hide an open
        position from the exposure count.
        """
        qty = _num(deal.get("bought_amount"))
        entry = _num(deal.get("bought_average_price"))
        upnl = _num(deal.get("actual_usd_profit"))
        # Documented deal field `strategy` is "long" or "short".
        strategy = str(deal.get("strategy") or "").lower()
        side = {"long": "buy", "short": "sell"}.get(strategy, "")
        return Position(symbol=_keel_pair(str(deal.get("pair") or "")),
                        side=side,
                        qty=qty if qty is not None else 0.0,
                        entry_price=entry if entry is not None else 0.0,
                        unrealized_pnl=upnl if upnl is not None else 0.0,
                        venue_id="%s:deal:%s" % (self.name, deal.get("id")),
                        raw={"deal": deal,
                             "qty_known": qty is not None,
                             "entry_price_known": entry is not None,
                             "upnl_known": upnl is not None})

    # --------------------------------------------------------------- control

    def start_bot(self, bot_id: str) -> BotAction:
        return self._set_running(bot_id, True)

    def stop_bot(self, bot_id: str) -> BotAction:
        # See stop_disposition: after this, deals already open keep running
        # under 3Commas' management until they close; only NEW deals stop.
        return self._set_running(bot_id, False)

    def _set_running(self, bot_id: str, target: bool) -> BotAction:
        verb = "enable" if target else "disable"
        if self.read_only:
            # Refused before any parsing or network I/O (R4). Reading is
            # always allowed; changing a bot's state is armed separately.
            raise HostReadOnly(
                "%s is read-only; arm bot control before trying to %s a bot"
                % (self.name, verb), venue=self.name)
        family, num = self._split(bot_id)
        show_tpl = ("/ver1/bots/{id}/show" if family == "dca"
                    else "/ver1/grid_bots/{id}")
        flip_tpl = ("/ver1/bots/{id}/%s" if family == "dca"
                    else "/ver1/grid_bots/{id}/%s") % verb

        current = self._enabled_of(
            self._request("GET", show_tpl, bot_id=num) or {},
            "%s bot %d" % (family, num))
        if current == target:
            # Idempotent (R5): the state was already what was asked for.
            # That is a success, not an error, and nothing is sent — bot
            # control has no double-fill to fear, but there is still no
            # reason to touch what is already right.
            return BotAction(bot_id=bot_id, running=current, changed=False,
                             detail="already %sd" % verb)

        # Documented: enable/disable answer 201 with the bot entity.
        resp = self._request("POST", flip_tpl, bot_id=num) or {}
        if isinstance(resp, dict) and "is_enabled" in resp:
            after = bool(resp["is_enabled"])
        else:
            # A 2xx without the flag is an acceptance, not a confirmation.
            # Re-read rather than assume (fail closed).
            after = self._enabled_of(
                self._request("GET", show_tpl, bot_id=num) or {},
                "%s bot %d" % (family, num))
        if after != target:
            # The venue accepted the call and then reported the opposite
            # state. Claiming success would leave a bot running that Keel
            # believes stopped, or the reverse; both are unbounded.
            raise HostError(
                "3commas accepted %s of %s but still reports is_enabled=%s"
                % (verb, bot_id, after), venue=self.name)
        return BotAction(bot_id=bot_id, running=after, changed=True,
                         detail="%sd" % verb)

    # --------------------------------------------------------------- parsing

    def _split(self, bot_id: str) -> Tuple[str, int]:
        """"dca:123" / "grid:456" -> (family, int id). Anything else is
        refused: DCA and grid bots are separate id spaces on separate
        endpoints, and a bare number sent to the wrong family would start
        someone else's bot.
        """
        family, sep, num = str(bot_id).partition(":")
        if sep and family in ("dca", "grid") and num.isdigit() and int(num) > 0:
            return family, int(num)
        raise HostError(
            "bot id must be 'dca:<n>' or 'grid:<n>'; a bare number is "
            "ambiguous between the two id spaces and will not be guessed "
            "(got %r)" % (bot_id,), venue=self.name)

    def _id_of(self, row: Dict[str, Any], what: str) -> int:
        try:
            n = int(row.get("id"))
        except (TypeError, ValueError):
            raise HostError("%s row has no usable id (got %r)"
                            % (what, row.get("id")),
                            venue=self.name) from None
        if n <= 0:
            raise HostError("%s row has a non-positive id %d" % (what, n),
                            venue=self.name)
        return n

    def _enabled_of(self, row: Dict[str, Any], what: str) -> bool:
        # R1: an entity without is_enabled is a state we could not read, and
        # bool(missing) would spell it "stopped". Refuse instead.
        if "is_enabled" not in row:
            raise HostError(
                "%s did not report is_enabled; cannot know whether it is "
                "running" % what, venue=self.name)
        return bool(row["is_enabled"])


# ------------------------------------------------------------------ helpers

def _num(value: Any) -> Optional[float]:
    """float() or None — copied from robinhood.py (the house rule, R7).

    Never a default, never NaN/inf: a missing number must stay missing so
    the caller decides, rather than being handed a confident zero.
    """
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _keel_pair(pair: str) -> str:
    """3Commas spells pairs QUOTE_BASE ("USDT_ETH"); Keel reads BASE/QUOTE.
    Anything without an underscore is passed through untouched — a spelling
    we do not recognise is better reported verbatim than mangled."""
    if "_" in pair:
        quote, base = pair.split("_", 1)
        if quote and base:
            return "%s/%s" % (base, quote)
    return pair


register_host("3commas-bots", ThreeCommasBots)
