"""Cryptohopper — a strategy host. Keel starts hoppers, stops them, reads them.

TWO OFFICIAL, MUTUALLY CONTRADICTORY API CONTRACTS
  Cryptohopper publishes two descriptions of the same API that do not agree
  with each other:

    Source A ("legacy" here) — cryptohopper.com/api-documentation:
        auth:  an `access-token: <token>` header
        shape: REST-style paths keyed by id, e.g. PATCH /v1/hopper/{id}

    Source B ("openapi" here) — github.com/cryptohopper (org-verified): an
    OpenAPI 3.1 spec and the official Python SDK generated from it:
        auth:  `Authorization: Bearer <token>`, optionally paired with an
               `x-api-app-key` header carrying the OAuth client_id (the spec
               says the server uses it for per-app rate-limit attribution)
        shape: RPC-style paths, e.g. GET /v1/hopper/list,
               GET /v1/hopper/get?hopper_id=..., POST /v1/hopper/update

  Which one a given account actually answers to has NOT been settled against
  a live account. This adapter builds against Source B by default (newer,
  org-verified, machine-readable) and isolates everything contract-specific
  in a _Contract value selected by the config key `api_contract`, default
  "openapi", alternative "legacy". THE FIRST health() CALL IS THE PROBE that
  settles it: a 401/404 pattern on one contract while credentials are known
  good is the signal that the account lives behind the other contract.
  Switching is the OPERATOR'S decision, made in config — never automatic.
  An adapter that auto-falls-back on 401 is an adapter that sprays a live
  credential at endpoints it was not issued for.

THE KEY IS MORE POWERFUL THAN WHAT KEEL USES — BY VENDOR DESIGN
  Start/stop is a FIELD WRITE, not a verb: POST /v1/hopper/update with
  {hopper_id, enabled: 1|0} on Source B, PATCH /v1/hopper/{id} with the same
  field on Source A. Neither source has a dedicated start/stop endpoint.
  That write requires the "manage" scope, and Cryptohopper's scopes cannot
  express "may flip `enabled` and nothing else": "manage" ALSO grants
  /hopper/delete, /hopper/create and every config write. So the credential
  created for Keel can do strictly more than Keel is permitted to do, and
  the vendor offers no way to narrow it. The transport allowlist below is
  the actual enforcement: ONE private request method, a hardcoded frozenset
  of (METHOD, path) pairs per contract, everything else refused before any
  network I/O. Deliberately absent from both allowlists: /hopper/buy,
  /hopper/sell, /hopper/{id}/order, /hopper/panic, delete, create and all
  config endpoints. Source B documents buy/sell `amount` and `price` as
  OPTIONAL — omit them and the hopper sizes the order itself. That is a
  second decision-maker, exactly what strategy_host.py exists to forbid.

WHAT STOPPING DOES TO OPEN POSITIONS: UNKNOWN
  Neither source states whether disabling a hopper closes its open
  positions, keeps managing them, or parks them. Looked for; not found.
  So stop_disposition is STOP_UNKNOWN — declared, not guessed — and callers
  treat unknown as ORPHANS: stopping a hopper may leave open positions with
  no bot steering them and no Keel rail permitted to touch them. Do not
  "upgrade" this to STOP_CLOSES_POSITIONS without written vendor evidence.
  (/hopper/panic does liquidate, but it needs the `trade` scope and IS the
  kind of order-flow this adapter exists to refuse; it is not in the
  allowlist and must never be.)

P&L: NOT REPORTED, THEREFORE None, NEVER 0
  Neither contract documents any realized/unrealized P&L field on hopper or
  position rows (looked; absent — the Source B Position schema is hopper_id,
  coin, amount, rate, additionalProperties). BotState therefore carries
  realized_pnl=None and unrealized_pnl=None. hosted_exposure() counts these
  bots as unvalued instead of silently flat, which is the whole point.

RATE LIMIT — the sources disagree here too
  Source A documents 2 requests/second per user. Source B's spec documents a
  "normal" bucket of 30 requests/minute (all endpoints this adapter uses),
  plus stricter buckets for order/backtest paths Keel never touches. We
  enforce the SLOWER of the two claims — one request per 2.0 seconds — as a
  simple min-interval in the transport, which keeps us inside both stories.
"""
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from . import Position, VenueHealth
from .strategy_host import (STOP_UNKNOWN, Bot, BotAction, BotState, HostError,
                            HostReadOnly, register_host)

BASE_URL = "https://api.cryptohopper.com"

# See "RATE LIMIT" in the module docstring: 30/min (Source B) vs 2/s (Source
# A). One request per 2.0s satisfies the stricter claim.
_MIN_REQUEST_INTERVAL_S = 2.0

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


# ------------------------------------------------------------------ contracts

@dataclass(frozen=True)
class _Contract:
    """One of the two published spellings of the Cryptohopper API.

    `ops` maps the four logical operations this adapter performs onto
    (METHOD, path template). `allowlist` is the R2 transport gate: the ONLY
    (METHOD, path template) pairs _request will ever send. The two are kept
    as separate hardcoded values on purpose — an op added to `ops` without a
    matching allowlist entry is refused at the transport, not trusted.
    """
    key: str                 # config spelling of this contract
    other: str               # the contract to suggest when this one 401/404s
    auth_style: str          # "bearer" (Source B) | "access-token" (Source A)
    ops: Dict[str, Tuple[str, str]]
    allowlist: frozenset


# Source B — github.com/cryptohopper OpenAPI 3.1 spec + official Python SDK
# (Hoppers.list/get/positions/update). Every path verified in both artifacts.
_OPENAPI = _Contract(
    key="openapi", other="legacy", auth_style="bearer",
    ops={
        "list_bots":   ("GET", "/v1/hopper/list"),       # SDK Hoppers.list
        "get_bot":     ("GET", "/v1/hopper/get"),        # SDK Hoppers.get
        "positions":   ("GET", "/v1/hopper/positions"),  # SDK Hoppers.positions
        "set_enabled": ("POST", "/v1/hopper/update"),    # SDK Hoppers.update;
        # requires "manage" scope — see module docstring on the over-grant
    },
    allowlist=frozenset({
        ("GET", "/v1/hopper/list"),
        ("GET", "/v1/hopper/get"),
        ("GET", "/v1/hopper/positions"),
        ("POST", "/v1/hopper/update"),
    }),
)

# Source A — cryptohopper.com/api-documentation. REST-style, id in the path.
# Only PATCH /v1/hopper/{id} was positively established from Source A while
# writing this; the read paths below follow Source A's resource layout and
# are otherwise UNVERIFIED against a live account — one more reason the
# openapi contract is the default and this one is opt-in via config.
_LEGACY = _Contract(
    key="legacy", other="openapi", auth_style="access-token",
    ops={
        "list_bots":   ("GET", "/v1/hopper"),
        "get_bot":     ("GET", "/v1/hopper/{id}"),
        "positions":   ("GET", "/v1/hopper/{id}/position"),
        "set_enabled": ("PATCH", "/v1/hopper/{id}"),     # the one Source A
        # path named in evidence; {enabled: 1|0} is the same field write
    },
    allowlist=frozenset({
        ("GET", "/v1/hopper"),
        ("GET", "/v1/hopper/{id}"),
        ("GET", "/v1/hopper/{id}/position"),
        ("PATCH", "/v1/hopper/{id}"),
    }),
)

_CONTRACTS = {c.key: c for c in (_OPENAPI, _LEGACY)}


class CryptohopperHost:
    """One Cryptohopper account. Sees hoppers; starts and stops them; nothing
    else — enforced by the transport allowlist rather than by good
    intentions, because the vendor's scopes cannot enforce it."""

    # Declared from evidence: neither source says what disabling a hopper
    # does to its open positions, so this stays STOP_UNKNOWN and callers
    # treat a stopped hopper's book as ORPHANS until a human reconciles it.
    stop_disposition = STOP_UNKNOWN

    def __init__(self, config: Dict[str, Any]):
        self.name: str = config.get("name") or "cryptohopper"
        self.read_only: bool = bool(config.get("read_only", True))

        token = config.get("access_token") or ""
        if not token:
            # Note the message names the missing field, never its value.
            raise HostError("cryptohopper needs access_token", venue=self.name)
        self._token = token
        self._app_key: str = config.get("app_key") or ""

        key = config.get("api_contract") or "openapi"
        if key not in _CONTRACTS:
            raise HostError(
                "cryptohopper api_contract must be one of %s, got %r "
                "(the two published contracts contradict each other; see "
                "the module docstring)" % (sorted(_CONTRACTS), key),
                venue=self.name)
        self._contract = _CONTRACTS[key]

        self._timeout = float(config.get("timeout_s", 10))
        self._session = requests.Session()
        self._min_interval = _MIN_REQUEST_INTERVAL_S
        self._last_request = 0.0
        # Injectable clock so the rate limiter is testable without sleeping.
        self._now = time.time
        self._sleep = time.sleep

    def __repr__(self) -> str:
        # Credentials never appear in a repr (R3). This is the repr that ends
        # up in tracebacks and log lines, so it carries identity only.
        return ("<CryptohopperHost %r contract=%s read_only=%s>"
                % (self.name, self._contract.key, self.read_only))

    # ------------------------------------------------------------- transport

    def _scrub(self, text: Any) -> str:
        """Strip credential material out of any message we did not write
        ourselves (server bodies, transport exceptions). Belt and braces:
        the token only ever travels in a header, but a proxy or a chatty
        server error can still echo it back at us."""
        out = str(text)
        for secret in (self._token, self._app_key):
            if secret:
                out = out.replace(secret, "***")
        return out

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json; charset=utf-8"}
        if self._contract.auth_style == "bearer":
            # Source B: Authorization: Bearer <40-char OAuth token>, plus the
            # optional x-api-app-key (OAuth client_id) for rate attribution.
            h["Authorization"] = "Bearer %s" % self._token
            if self._app_key:
                h["x-api-app-key"] = self._app_key
        else:
            # Source A: a bare access-token header.
            h["access-token"] = self._token
        return h

    def _request(self, method: str, template: str,
                 path_args: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None) -> Any:
        """The ONE way out of this adapter, and the R2 gate.

        Every call is checked against the current contract's hardcoded
        allowlist BEFORE any network I/O. The platform cannot scope a key
        down to "read + flip enabled", so this frozenset is the only thing
        standing between a "manage"-scoped credential and the order, panic,
        delete and config endpoints that share that scope.
        """
        method = method.upper()
        if (method, template) not in self._contract.allowlist:
            raise HostError(
                "cryptohopper refuses %s %s: not in the %s contract's "
                "read/start/stop allowlist. Keel never places or manages "
                "orders through a strategy host." % (method, template,
                                                     self._contract.key),
                venue=self.name)

        # Simple min-interval rate limit; see module docstring for why 2.0s.
        # The allowlist above checks the TEMPLATE, but path_args are
        # substituted into it below. A bot_id of "1/config" would format into
        # /v1/hopper/1/config -- an on-the-wire path that is NOT one of the
        # allowlisted concrete paths, defeating the whole gate. bot_ids come
        # from str(row["id"]) in the venue's own response, so a quirky or
        # compromised value flows straight back into the path. Any arg that
        # could change the path's shape is refused here, BEFORE the rate-limit
        # sleep and before any I/O.
        for _k, _v in (path_args or {}).items():
            _s = str(_v)
            if any(c in _s for c in "/?#%\\") or ".." in _s or _s.strip() != _s:
                raise HostError(
                    "cryptohopper refuses a path argument that could alter the "
                    "request path: %s=%r. A bot id is not a path." % (_k, _s),
                    venue=self.name)

        gap = self._min_interval - (self._now() - self._last_request)
        if gap > 0:
            self._sleep(gap)
        self._last_request = self._now()

        path = template.format(**path_args) if path_args else template
        if params:
            path += "?" + urlencode([(k, str(v)) for k, v in params.items()
                                     if v is not None])
        data = (json.dumps(body, separators=(",", ":")).encode("utf-8")
                if body is not None else None)
        try:
            r = self._session.request(method, BASE_URL + path,
                                      headers=self._headers(), data=data,
                                      timeout=self._timeout)
        except requests.RequestException as e:
            # No answer at all. Retryable is fine here: everything this
            # adapter sends is naturally idempotent (reads, and a field
            # write that sets an absolute state rather than toggling one).
            raise HostError("cryptohopper transport failure: %s"
                            % self._scrub(e), retryable=True, venue=self.name,
                            cause=e, http_status=None)

        if r.status_code >= 400:
            raise HostError(
                "cryptohopper %s on %s %s: %s"
                % (r.status_code, method, template, self._scrub(_detail(r))),
                retryable=r.status_code in _RETRYABLE_STATUS,
                venue=self.name, http_status=r.status_code)
        try:
            doc = r.json()
        except ValueError as e:
            raise HostError("cryptohopper returned non-JSON on %s %s"
                            % (method, template), venue=self.name, cause=e,
                            http_status=r.status_code)
        if not isinstance(doc, dict):
            raise HostError(
                "cryptohopper %s %s returned %s where an object envelope was "
                "expected" % (method, template, type(doc).__name__),
                venue=self.name, http_status=r.status_code)
        # Source B documents every success as {"data": ...}. The legacy
        # envelope is UNDOCUMENTED; a bare object is accepted as the payload.
        return doc.get("data", doc)

    def _op(self, op: str, bot_id: Optional[str] = None,
            body: Optional[Dict[str, Any]] = None) -> Any:
        """Route a logical operation through the current contract's spelling:
        the id rides in the path on legacy, in the query string on openapi."""
        method, template = self._contract.ops[op]
        params, path_args = None, None
        if bot_id is not None:
            if "{id}" in template:
                path_args = {"id": bot_id}
            else:
                params = {"hopper_id": bot_id}
        return self._request(method, template, path_args=path_args,
                             params=params, body=body)

    # ----------------------------------------------------------------- reads

    def health(self) -> VenueHealth:
        """Never raises: a dead host is a health result, not an exception.

        This is also the contract probe named in the module docstring. It
        REPORTS a 401/404 pattern and names the alternative contract; it
        never switches by itself, because auto-fallback with credentials is
        a way to spray a token at endpoints it was not issued for.
        """
        started = time.time()
        try:
            rows = _rows(self._op("list_bots"), "hopper list", self.name)
        except HostError as e:
            detail = str(e)
            if e.http_status in (401, 404):
                detail += (
                    " | Contract probe: if this credential is known-good, "
                    "the account may live behind the other official "
                    "contract (%r). Switching is an operator decision: set "
                    "api_contract=%r in config. This adapter never retries "
                    "a credential against alternate paths on its own."
                    % (self._contract.other, self._contract.other))
            elif e.http_status == 403:
                # Source B documents 403 as missing scope OR an OAuth-app
                # IP-whitelist mismatch — both operator problems, not ours.
                detail += (" | 403 is documented as a missing scope or an "
                           "IP-whitelist mismatch on the OAuth app.")
            return VenueHealth(name=self.name,
                               reachable=e.http_status is not None,
                               authenticated=False, read_only=self.read_only,
                               detail=detail)
        except Exception as e:                            # noqa: BLE001
            return VenueHealth(name=self.name, reachable=False,
                               authenticated=False, read_only=self.read_only,
                               detail="unexpected: %s" % self._scrub(e))
        return VenueHealth(
            name=self.name, reachable=True, authenticated=True,
            read_only=self.read_only,
            latency_ms=int((time.time() - started) * 1000),
            detail="contract %r answered; %d hopper(s) visible"
                   % (self._contract.key, len(rows)))

    def bots(self) -> List[Bot]:
        out: List[Bot] = []
        for row in _rows(self._op("list_bots"), "hopper list", self.name):
            out.append(self._bot(row))
        return out

    def _bot(self, row: Dict[str, Any]) -> Bot:
        bot_id = row.get("id")
        if bot_id is None or str(bot_id) == "":
            # A hopper we cannot address is a hopper we cannot stop.
            raise HostError("cryptohopper returned a hopper row without an "
                            "id; refusing to report a bot Keel cannot "
                            "control", venue=self.name)
        running = _enabled_flag(row.get("enabled"))
        if running is None:
            # R1: an unreadable state is not a stopped bot. Reporting
            # running=False here would hide live money from the kill
            # switches, which is the expensive direction to be wrong in.
            raise HostError(
                "cryptohopper hopper %s has no readable enabled flag "
                "(got %r); an unreadable state is not a stopped bot"
                % (bot_id, row.get("enabled")), venue=self.name)
        # `strategy` and `pairs` are not in either source's hopper schema
        # (undocumented); left empty rather than guessed from stray fields.
        return Bot(bot_id=str(bot_id), name=str(row.get("name") or ""),
                   host=self.name, running=running, strategy="", pairs=[],
                   raw=row)

    def bot_state(self, bot_id: str) -> BotState:
        bot = self._bot(_row(self._op("get_bot", bot_id=bot_id),
                             "hopper", self.name))
        positions = [self._position(r) for r in
                     _rows(self._op("positions", bot_id=bot_id),
                           "position list", self.name)]
        # Neither contract documents any P&L field on hopper or position
        # rows (looked; absent). R1: what the host did not tell us stays
        # None — hosted_exposure() then counts this bot as UNVALUED instead
        # of silently flat.
        return BotState(bot_id=str(bot_id), running=bot.running,
                        positions=positions, realized_pnl=None,
                        unrealized_pnl=None, as_of=int(time.time()),
                        detail="cryptohopper reports no P&L fields; "
                               "exposure is unvalued by design")

    def _position(self, row: Dict[str, Any]) -> Position:
        """Source B Position schema: hopper_id, coin, amount, rate.

        A row we cannot size is refused rather than skipped: silently
        dropping it would report a smaller book than the host is running,
        which fails open. `side` is "buy" because hoppers hold spot bought
        into a position — the schema has no side field (undocumented, but
        there is no short spelling anywhere in either source). The quote
        currency of `coin` is the hopper's own base currency and is NOT in
        the row (undocumented), so the coin is reported as spelled.
        """
        coin = str(row.get("coin") or "")
        qty = _num(row.get("amount"))
        entry = _num(row.get("rate"))
        if not coin or qty is None or entry is None:
            raise HostError(
                "cryptohopper position row unreadable (coin=%r amount=%r "
                "rate=%r); refusing to report a partial book"
                % (row.get("coin"), row.get("amount"), row.get("rate")),
                venue=self.name)
        # Position.unrealized_pnl has no way to say "unknown" (it is a plain
        # float defaulting to 0.0). The authoritative unknown lives on
        # BotState.unrealized_pnl=None, which is what hosted_exposure reads.
        # Do not sum position-level zeros from a host.
        return Position(symbol=coin, side="buy", qty=qty, entry_price=entry,
                        venue_id="%s:%s" % (self.name, coin), raw=row)

    # ----------------------------------------------------------- bot control

    def start_bot(self, bot_id: str) -> BotAction:
        return self._set_enabled(bot_id, True)

    def stop_bot(self, bot_id: str) -> BotAction:
        # See stop_disposition above: what this does to open positions is
        # UNKNOWN, and callers must treat the stopped book as orphans.
        return self._set_enabled(bot_id, False)

    def _set_enabled(self, bot_id: str, want: bool) -> BotAction:
        if self.read_only:
            raise HostReadOnly(
                "%s is read-only; bot control has not been armed for this "
                "host" % self.name, venue=self.name)

        # Read first. Two reasons: R5 idempotency (already-there is a no-op
        # success, and skipping the write avoids burning a "manage"-scoped
        # call we do not need), and R1 (a state we cannot read is a state we
        # refuse to write over blind).
        row = _row(self._op("get_bot", bot_id=bot_id), "hopper", self.name)
        current = _enabled_flag(row.get("enabled"))
        if current is None:
            raise HostError(
                "cryptohopper hopper %s: cannot read the current enabled "
                "state (got %r); refusing a blind write"
                % (bot_id, row.get("enabled")), venue=self.name)
        if current == want:
            return BotAction(str(bot_id), running=current, changed=False,
                             detail="already %s"
                                    % ("running" if want else "stopped"))

        # The field write. No dedicated start/stop verb exists in either
        # source. openapi: POST /v1/hopper/update {hopper_id, enabled: 1|0}
        # (SDK Hoppers.update). legacy: PATCH /v1/hopper/{id} {enabled: 1|0}.
        # Setting an absolute state (not toggling) keeps a retry harmless.
        method, template = self._contract.ops["set_enabled"]
        flag = 1 if want else 0
        if "{id}" in template:
            payload = self._request(method, template,
                                    path_args={"id": bot_id},
                                    body={"enabled": flag})
        else:
            payload = self._request(method, template,
                                    body={"hopper_id": str(bot_id),
                                          "enabled": flag})

        # Confirm the write took. Source B documents the update response as
        # the hopper object, but every field in that schema is optional, so
        # an unconfirming body triggers one explicit re-read. If the state
        # still cannot be read, or reads back wrong, that is an error — a
        # write we cannot verify is not a write that succeeded (R1).
        after = (_enabled_flag(payload.get("enabled"))
                 if isinstance(payload, dict) else None)
        if after is None:
            reread = _row(self._op("get_bot", bot_id=bot_id), "hopper",
                          self.name)
            after = _enabled_flag(reread.get("enabled"))
        if after is None:
            raise HostError(
                "cryptohopper hopper %s: wrote enabled=%d but cannot read "
                "the state back; treat the bot's state as unknown"
                % (bot_id, flag), venue=self.name)
        if after != want:
            raise HostError(
                "cryptohopper hopper %s: wrote enabled=%d but the host "
                "reports the bot is still %s"
                % (bot_id, flag, "running" if after else "stopped"),
                venue=self.name)
        return BotAction(str(bot_id), running=after, changed=True,
                         detail="enabled -> %d" % flag)


# -------------------------------------------------------------------- helpers

def _enabled_flag(value: Any) -> Optional[bool]:
    """The hopper `enabled` field, or None when it cannot be read.

    Source B types it `integer | boolean`. The backend is PHP and the
    official user schema types `is_trial_user` as a string, so the string
    spellings "0"/"1" are accepted too. Anything else is None — never a
    guessed False — and every caller treats None as an error (R1).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return value.strip() == "1"
    return None


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


def _rows(payload: Any, what: str, venue: str) -> List[Dict[str, Any]]:
    """A list of dict rows, or HostError. A payload that is not the list we
    were promised is an unreadable answer, and unreadable is never empty."""
    if not isinstance(payload, list):
        raise HostError("cryptohopper %s: expected a list, got %s"
                        % (what, type(payload).__name__), venue=venue)
    for row in payload:
        if not isinstance(row, dict):
            raise HostError("cryptohopper %s: expected object rows, got %s"
                            % (what, type(row).__name__), venue=venue)
    return payload


def _row(payload: Any, what: str, venue: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HostError("cryptohopper %s: expected an object, got %s"
                        % (what, type(payload).__name__), venue=venue)
    return payload


def _detail(r: Any) -> str:
    """Source B's ApiErrorBody: {status, code, error: 1, message}. `message`
    is the human text; `code` is a numeric bucket diagnostic."""
    try:
        body = r.json()
    except Exception:                                     # noqa: BLE001
        return (getattr(r, "text", "") or "")[:200]
    if isinstance(body, dict) and body.get("message"):
        return str(body.get("message"))[:300]
    return str(body)[:200]


register_host("cryptohopper", CryptohopperHost)
