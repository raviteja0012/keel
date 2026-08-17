"""Altrady — a signal-bot strategy host: Keel starts bots, stops bots and
reads their positions, and the transport makes anything else unreachable.

Written against the live Swagger saved locally as alt.json ("Altrady API v2",
swagger 2.0, host api.altrady.com), read in full. It is small enough to know
completely: FIVE paths, SEVEN operations, and that is the entire v2 surface.
Everything below that says "documented" appears in that file; everything that
says "undocumented" was looked for there and not found, which is a different
and much more dangerous thing.

KEEL IS THE DIRECTORY, BY NECESSITY
  There is no list-bots endpoint — not omitted from this adapter, absent from
  the platform. Credentials are PER BOT: one api_key/api_secret pair
  identifies exactly one signal bot, and every operation carries the pair of
  the bot it addresses. A pair IS a bot. So the registry lives in Keel's
  config:

      {"name": ..., "read_only": ..., "bots": [
          {"bot_id": ..., "name": ..., "api_key": ..., "api_secret": ...},
          ...]}

  and bots() returns that registry, not a venue listing. If the operator
  creates a bot on Altrady and does not add it here, Keel cannot see it —
  no call exists that would reveal it.

NO SCOPES EXIST ON THIS PLATFORM, SO THE ALLOWLIST IS THE ONLY WALL
  The same api_key/api_secret that reads a bot's P&L will open, increase,
  reduce, reverse and close positions if POSTed to /v2/signal_bot_positions —
  the very path the read uses, one HTTP verb apart. Altrady offers no
  read-only credential to hold instead. "See but never manage" therefore
  cannot be enforced by the platform and is enforced HERE: the single private
  request method checks every (METHOD, path) pair against _ALLOWED before any
  network I/O, and the positions POST is not in it. Neither is
  /v2/signal_bot/filters (rewriting a bot's market filters is managing it),
  /v2/signal_bot/bulk (start/stop we already have, one bot at a time and
  attributably; update_filters we refuse), nor the smart_positions webhook
  (cancelling a position is managing it).

SECRETS RIDE IN THE QUERY STRING (landmine, documented)
  GET /v2/signal_bot_positions takes api_key AND api_secret as required QUERY
  parameters — the swagger says so, and offers no header alternative for this
  endpoint. A URL is the one string everything loves to log: access logs,
  proxies, APM traces, and requests' own exception text, which embeds the
  full URL on a connection failure. So this adapter never logs a URL, error
  messages are built from `path` (which never carries the query), and every
  exception caught in the transport has _scrub() applied to its str() before
  the text can escape. `cause=` is deliberately NOT forwarded on transport
  failures: the original exception object still holds the URL in its args,
  and an innocent log line rendering exc.cause would leak both values.

WHAT STOPPING DOES TO OPEN POSITIONS — STOP_ORPHANS_POSITIONS, from evidence
  The webhook documentation in alt.json states, for action "stop_bot": "the
  bot will stop accepting new signals. Any open positions will remain open."
  A separate action "stop_and_close" exists precisely because plain stop does
  not close. That is direct vendor evidence that stopping ORPHANS the book,
  so the disposition is declared from it, not guessed. stop_and_close is NOT
  implemented here: closing a position is managing it, and Keel never manages
  a hosted position.

WHAT CANNOT BE READ, AND HOW THAT IS REPORTED
  * Whether a bot is active. No operation returns it — the seven operations
    are open/close, start/stop, bulk, filters and one positions read, and
    none of them answers "is this bot running?". `running` is therefore the
    last state Keel itself commanded in this process (which a change made in
    the Altrady UI would silently invalidate), and before any command it is
    PRESUMED True. Presuming live is the fail-closed direction: a bot wrongly
    believed running attracts attention and at worst a harmless stop; a bot
    wrongly believed stopped is how live money escapes the risk rails.
  * Lifetime realized P&L. The positions read returns "the 100 most recently
    updated positions" with no pagination, so any realized total computed
    from it is a WINDOW sum that silently understates history. realized_pnl
    is therefore always None rather than a partial number dressed as a total.
  * Unrealized P&L in USD. Rows carry realizedProfitUsd and netProfitUsd, but
    unrealizedProfit exists only in QUOTE currency — a USD variant was looked
    for in the schema and is absent (undocumented). The sum reported here is
    quote-currency, and is only a meaningful single number while all open
    positions share a quote. The caveat is real and belongs here rather than
    discovered in a drawdown.
"""
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlencode

import requests

from . import Position, VenueHealth
from .strategy_host import (STOP_ORPHANS_POSITIONS, Bot, BotAction, BotState,
                            HostError, HostReadOnly, register_host)

# Swagger: host api.altrady.com, basePath "/". No scheme is declared in
# alt.json; https is the only defensible assumption for credentials on the
# wire, and the vendor's own examples use it.
BASE_URL = "https://api.altrady.com"

_POSITIONS_PATH = "/v2/signal_bot_positions"
_START_STOP_PATH = "/v2/signal_bot/start_stop"

# THE WALL (rule R2, landmine L2). Altrady has no credential scopes: the pair
# that reads P&L also opens and closes positions via POST to the SAME path the
# read uses. Every request is checked against this frozenset before any
# network I/O. Note what is absent and must stay absent:
#   POST /v2/signal_bot_positions   opens/closes/reverses REAL positions
#   POST /v2/signal_bot/filters     rewrites a bot's market filters
#   POST /v2/signal_bot/bulk        start/stop/update_filters in bulk
#   /v2/smart_positions/.../cancel  cancels a position (managing it)
_ALLOWED = frozenset({
    ("GET", _POSITIONS_PATH),        # alt.json getV2SignalBotPositions
    ("POST", _START_STOP_PATH),      # alt.json postV2SignalBotStartStop
})

# Documented: the positions GET returns "the 100 most recently updated
# positions" — a window, not a page. No cursor exists. At exactly 100 rows we
# cannot know whether we saw the whole open book, so the P&L sum is refused.
_WINDOW = 100

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class AltradyHost:
    """One Altrady account's signal bots, as declared in Keel's config."""

    # Documented, quoted in the module docstring: plain stop leaves open
    # positions open. stop_and_close exists and is deliberately not used.
    stop_disposition = STOP_ORPHANS_POSITIONS

    def __init__(self, config: Dict[str, Any]):
        self.name: str = config.get("name") or "altrady"
        # R4: read-only until someone arms bot control deliberately.
        self.read_only: bool = bool(config.get("read_only", True))
        self._timeout = float(config.get("timeout_s", 10))
        self._session = requests.Session()
        # What Keel last commanded, per bot, in this process. See _running().
        self._last_commanded: Dict[str, bool] = {}

        entries = config.get("bots") or []
        if not isinstance(entries, list):
            raise HostError("altrady config key 'bots' must be a list of "
                            "{bot_id, name, api_key, api_secret}",
                            venue=self.name)
        self._bots: Dict[str, Dict[str, str]] = {}
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                raise HostError("altrady bot entry %d is not a mapping" % i,
                                venue=self.name)
            bot_id = str(e.get("bot_id") or "")
            api_key = str(e.get("api_key") or "")
            api_secret = str(e.get("api_secret") or "")
            missing = [k for k, v in (("bot_id", bot_id), ("api_key", api_key),
                                      ("api_secret", api_secret)) if not v]
            if missing:
                # Field NAMES only. Echoing the fields that were present would
                # put a live credential in an exception message (R3).
                raise HostError("altrady bot entry %d is missing %s"
                                % (i, ", ".join(missing)), venue=self.name)
            if bot_id in self._bots:
                # One pair is one bot; two entries under one id would make
                # start/stop ambiguous about which credentials fire.
                raise HostError("altrady bot_id %r appears twice in config"
                                % bot_id, venue=self.name)
            self._bots[bot_id] = {"bot_id": bot_id,
                                  "name": str(e.get("name") or bot_id),
                                  "api_key": api_key,
                                  "api_secret": api_secret}

        # Every value _scrub() must erase from any escaping text (R3/L1).
        self._secrets: Tuple[str, ...] = tuple(
            v for b in self._bots.values()
            for v in (b["api_key"], b["api_secret"]))

    # ------------------------------------------------------------- transport

    def _scrub(self, text: str) -> str:
        return _scrub(text, self._secrets)

    def _request(self, method: str, path: str,
                 query: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None,
                 expect_body: bool = False) -> Any:
        """The ONE door to the network, and it checks the allowlist first.

        The check happens before the URL is even built, so an order-placement
        path cannot be reached by any code path in this module, present or
        future, without editing _ALLOWED — which is the point: the platform
        cannot enforce "see but never manage", so this method does.

        Landmine L1 lives here too: the GET carries both credentials in the
        query string, and requests embeds the full URL in its exception text.
        Every caught exception is therefore scrubbed before re-raising, error
        messages are built from `path` (never `url`), and the original
        exception is not attached as cause because its args still hold the
        URL verbatim.
        """
        method = method.upper()
        if (method, path) not in _ALLOWED:
            raise HostError(
                "altrady transport refuses %s %s: not in the allowlist. "
                "This host is see-and-trigger only; order and position "
                "management endpoints are deliberately unreachable."
                % (method, path), venue=self.name)

        url = BASE_URL + path
        if query:
            url += "?" + urlencode(query)
        data = None
        headers = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        # Undocumented: alt.json's global securityDefinitions names an apiKey
        # "Authorization" header, but every signal-bot operation documents its
        # own required api_key/api_secret in the payload or query, and no
        # account-level token exists in this config. The per-bot pair is the
        # auth actually specified, so that is what is sent — and nothing else.
        # Scrubbing str(e) is not enough on its own: `raise ...` inside an
        # `except` chains the ORIGINAL requests exception as __context__, and
        # traceback.format_exc(), logging(exc_info=True) and str(e.__context__)
        # all resurrect the unscrubbed URL — which for Altrady's GET holds both
        # credentials in its query string. So the HostError is CONSTRUCTED
        # inside the except (to read str(e)) but RAISED outside it, where no
        # exception is being handled and __context__ is never populated.
        transport_err = None
        try:
            r = self._session.request(method, url, headers=headers,
                                      data=data, timeout=self._timeout)
        except Exception as e:                            # noqa: BLE001
            transport_err = HostError(
                "altrady transport failure: %s" % self._scrub(str(e)),
                retryable=True, venue=self.name, http_status=None)
        if transport_err is not None:
            raise transport_err

        if r.status_code >= 400:
            # Response bodies can echo the request that produced them, and
            # this request carried credentials. Scrub before quoting.
            raise HostError(
                "altrady %s %s: %s"
                % (r.status_code, path, self._scrub((r.text or "")[:300])),
                retryable=r.status_code in _RETRYABLE_STATUS,
                venue=self.name, http_status=r.status_code)
        if not r.content:
            # A body-less 2xx is a valid SUCCESS shape only where the endpoint
            # documents no response schema — start_stop answers 201 with none.
            # It is NOT valid for a GET that must return a list: an empty or
            # truncated 200 on the positions read would otherwise become {} ->
            # [] -> a confirmed-flat, trustworthy book. The caller says which
            # it expects; a read that needed content and got none is a failure,
            # not an empty book.
            if expect_body:
                raise HostError(
                    "altrady returned an empty body on %s where a result was "
                    "required — refusing to read that as 'nothing held'"
                    % path, retryable=True, venue=self.name,
                    http_status=r.status_code)
            return {}
        try:
            return r.json()
        except ValueError as e:
            raise HostError("altrady returned non-JSON on %s: %s"
                            % (path, self._scrub(str(e))),
                            venue=self.name, http_status=r.status_code) from None

    # -------------------------------------------------------------- registry

    def _entry(self, bot_id: str) -> Dict[str, str]:
        e = self._bots.get(str(bot_id))
        if e is None:
            # bot_ids are operator-chosen labels, safe to echo. Credentials
            # are not, and none appear here.
            raise HostError("altrady knows no bot %r (configured: %s)"
                            % (bot_id, ", ".join(sorted(self._bots)) or "none"),
                            venue=self.name)
        return e

    def _running(self, bot_id: str) -> Tuple[bool, str]:
        """The best obtainable answer to "is this bot active?".

        Undocumented — actually worse: unqueryable. None of the seven
        operations in alt.json returns a bot's active flag. So this is the
        last state Keel itself commanded (process-local; a toggle made in the
        Altrady UI is invisible to us), and before any command the bot is
        PRESUMED RUNNING. That is the fail-closed direction (R1): presuming
        live keeps the bot inside every risk rail; presuming stopped is how
        live money escapes attention.
        """
        if bot_id in self._last_commanded:
            return (self._last_commanded[bot_id],
                    "last Keel command; a change made in the Altrady UI "
                    "would not be visible here")
        return True, "presumed live: Altrady exposes no activity flag"

    # ----------------------------------------------------------------- reads

    def health(self) -> VenueHealth:
        """Never raises: a dead host is a health result, not an exception.

        The probe is the positions read of the first configured bot — the
        only read this platform has.
        """
        started = time.time()
        if not self._bots:
            return VenueHealth(name=self.name, reachable=False,
                               authenticated=False, read_only=self.read_only,
                               detail="no bots configured; no credentials "
                                      "to probe with")
        first = next(iter(self._bots.values()))
        try:
            self._positions_rows(first, "open")
        except HostError as e:
            # str(e) was scrubbed in the transport before it could escape.
            return VenueHealth(name=self.name,
                               reachable=e.http_status is not None,
                               authenticated=False, read_only=self.read_only,
                               detail=str(e))
        except Exception as e:                            # noqa: BLE001
            return VenueHealth(name=self.name, reachable=False,
                               authenticated=False, read_only=self.read_only,
                               detail="unexpected: %s" % self._scrub(str(e)))
        return VenueHealth(name=self.name, reachable=True, authenticated=True,
                           read_only=self.read_only,
                           latency_ms=int((time.time() - started) * 1000),
                           detail="probed positions of bot %r"
                                  % first["bot_id"])

    def bots(self) -> List[Bot]:
        """The config registry, because Keel IS the directory (see module
        docstring — no list-bots endpoint exists on this platform).

        pairs is empty because the market list is write-only on Altrady:
        filters can SET a white_list, nothing reads one back. raw carries no
        credentials, deliberately — Bot.raw is exactly the kind of dict that
        ends up in a log line.
        """
        out: List[Bot] = []
        for e in self._bots.values():
            running, _ = self._running(e["bot_id"])
            source = ("last_command" if e["bot_id"] in self._last_commanded
                      else "presumed_live")
            out.append(Bot(bot_id=e["bot_id"], name=e["name"], host=self.name,
                           running=running, strategy="signal", pairs=[],
                           raw={"running_source": source}))
        return out

    def _positions_rows(self, entry: Dict[str, str],
                        status: str) -> List[Dict[str, Any]]:
        # GET /v2/signal_bot_positions?api_key=&api_secret=&status=open|closed
        # (alt.json getV2SignalBotPositions; all three params required, and
        # yes, the credentials go in the query — landmine L1, handled in
        # _request). The swagger declares the response schema as a SINGLE
        # SignalBotPosition while the description promises "the 100 most
        # recently updated positions"; which is truthful is undocumented, so
        # both shapes are accepted rather than guessed between.
        # expect_body: this GET must return a list. A truly empty body is not
        # "no open positions" (that is []); it is a failed or truncated read,
        # and reading it as a flat book is the fail-open the reviewer caught.
        res = self._request("GET", _POSITIONS_PATH,
                            query={"api_key": entry["api_key"],
                                   "api_secret": entry["api_secret"],
                                   "status": status},
                            expect_body=True)
        if isinstance(res, list):
            return [r for r in res if isinstance(r, dict)]
        if isinstance(res, dict):
            return [res] if res else []
        raise HostError("altrady returned an unexpected positions payload "
                        "(%s)" % type(res).__name__, venue=self.name)

    def bot_state(self, bot_id: str) -> BotState:
        """One bot's open book. A failed read RAISES (R1): a state we could
        not fetch must never come back looking like a bot that is flat or
        stopped."""
        entry = self._entry(bot_id)
        rows = self._positions_rows(entry, "open")

        positions: List[Position] = []
        total = 0.0
        readable = True
        for row in rows:
            u = _num(row.get("unrealizedProfit"))
            if u is None:
                # One unreadable row poisons the whole sum. Skipping it and
                # summing the rest would report a smaller book as the book.
                readable = False
            else:
                # Quote-currency, not USD — the schema has realizedProfitUsd
                # and netProfitUsd but no unrealizedProfitUsd (looked for;
                # absent). Meaningful as a single number only while all open
                # positions share a quote. See module docstring.
                total += u
            side_raw = str(row.get("side") or "").lower()
            positions.append(Position(
                # coinraySymbol is the vendor's own spelling; re-spelling it
                # without a markets table would be a guess, so it passes
                # through verbatim.
                symbol=str(row.get("coinraySymbol") or ""),
                side={"long": "buy", "short": "sell"}.get(side_raw, side_raw),
                # The schema carries `invested` (quote currency) but no base
                # quantity and no entry price. 0.0 plus the flags in raw is
                # the same discipline robinhood uses for holdings: an unknown
                # number is marked unknown, not invented.
                qty=0.0, entry_price=0.0,
                unrealized_pnl=u if u is not None else 0.0,
                venue_id=str(row.get("id") or ""),
                raw={"qty_known": False, "entry_price_known": False,
                     "unrealized_known": u is not None, "row": dict(row)}))

        # Exactly _WINDOW rows means the documented 100-row window may have
        # truncated the open book; an unread position is not a flat one, so
        # the sum is refused (the pagination lesson, applied to a window).
        window_saturated = len(rows) >= _WINDOW
        if window_saturated:
            readable = False

        running, basis = self._running(bot_id)
        detail = "%d open positions; running=%s (%s)" % (len(positions),
                                                         running, basis)
        if window_saturated:
            detail += ("; 100-row window saturated, so the open book may be "
                       "truncated and the P&L sum is withheld")

        return BotState(
            bot_id=bot_id, running=running, positions=positions,
            # Always None, not laziness: the read is a 100-row recency
            # window, so any realized total computed from it understates
            # history by an unknowable amount. A partial sum presented as a
            # lifetime number is exactly the reassuring lie R1 forbids.
            realized_pnl=None,
            unrealized_pnl=(round(total, 8) if readable else None),
            as_of=int(time.time()), detail=detail)

    # --------------------------------------------------------------- control

    def start_bot(self, bot_id: str) -> BotAction:
        return self._set_active(bot_id, True)

    def stop_bot(self, bot_id: str) -> BotAction:
        # stop_disposition is STOP_ORPHANS_POSITIONS — documented: "Any open
        # positions will remain open." The stopped bot's book stays on the
        # exchange, visible through bot_state() and closable by nobody but a
        # human or the bot being started again.
        return self._set_active(bot_id, False)

    def _set_active(self, bot_id: str, active: bool) -> BotAction:
        if self.read_only:
            # R4: reading is always allowed; state changes on real money
            # require deliberate arming, checked before anything else.
            raise HostReadOnly("%s is read-only; bot control has not been "
                               "armed" % self.name, venue=self.name)
        entry = self._entry(bot_id)
        # POST /v2/signal_bot/start_stop {api_key, api_secret, active} -> 201
        # (alt.json postV2SignalBotStartStop; `active` is the only required
        # field beside the pair). `active` is a SET, not a toggle: the body
        # names the desired end state, so sending it to a bot already in that
        # state is harmless — which is what makes retrying a dropped response
        # safe. The 201 response declares no schema and no field reports what
        # the state previously was (looked for; undocumented), and no read
        # endpoint exposes it either, so a no-op cannot be OBSERVED. changed
        # is therefore asserted, not measured — the honest reading of R5 on a
        # platform that will not say.
        self._request("POST", _START_STOP_PATH,
                      body={"api_key": entry["api_key"],
                            "api_secret": entry["api_secret"],
                            "active": bool(active)})
        self._last_commanded[bot_id] = bool(active)
        return BotAction(
            bot_id=bot_id, running=bool(active), changed=True,
            detail="active=%s sent; Altrady treats this as a set, not a "
                   "toggle, and reports no prior state, so changed is "
                   "asserted, not observed" % bool(active))


# ------------------------------------------------------------------ helpers

def _scrub(text: str, secrets: Iterable[str]) -> str:
    """Erase every configured credential from a string before it can escape.

    Applied to str() of ANY exception the transport catches and to any quoted
    response body, because the GET carries both credentials in the URL and
    requests embeds the full URL in its error text (landmine L1). The
    urlencoded form is erased too: quote_plus can respell a secret that
    contains reserved characters, and a scrubber that only knows the raw
    spelling would wave the encoded one through.
    """
    out = text
    for s in secrets:
        if not s:
            continue
        out = out.replace(s, "***")
        q = quote_plus(s)
        if q != s:
            out = out.replace(q, "***")
    return out


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


register_host("altrady", AltradyHost)
