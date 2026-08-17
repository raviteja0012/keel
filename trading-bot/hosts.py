"""Strategy-host registry and credential store.

The host analogue of venues.py, and deliberately the same shape: credentials
live in the runtime DB, entered through the dashboard, and nowhere else; a
host is read-only until bot control is explicitly armed; secrets are masked at
the edge with a fingerprint so the UI can say WHICH key is loaded without
showing it.

One difference earns its own handling. Venue credentials are flat fields.
Altrady's are not: it has no list-bots endpoint and no account-level key, so a
host config carries a `bots` list in which EVERY entry holds its own
api_key/api_secret pair. Redaction walks that nesting; forgetting it would
hand the dashboard a JSON blob with live secrets in the middle.

The other job of this module is `exposure()` — the one number the engine's
kill switches read. Its discipline is the same as engine._open_pnl:

    a bot whose P&L cannot be read is COUNTED, not zeroed, and a host that
    cannot be reached makes the whole snapshot untrustworthy. The engine
    treats untrustworthy exactly as it treats an unvaluable open position:
    new entries stand aside.

Hosted positions are money at risk that Keel cannot close — the host's bot
owns them. They therefore count toward drawdown and the loss limits, and are
invisible to every rail that would adjust, hedge or flatten a position.
"""
import hashlib
import threading
import time
from typing import Any, Dict, List, Optional

import storage
from brokers import strategy_host as sh

_SETTING = "strategy_hosts"            # list of host config dicts
_SECRET_FIELDS = ("api_key", "api_secret", "access_token", "app_key",
                  "password")
_MASK = "••••set••••"

_cache: Dict[str, Any] = {}            # name -> StrategyHost instance
_cache_stamp: Dict[str, float] = {}
_CACHE_TTL_S = 300

# The exposure snapshot the engine reads. Refreshing it means real network
# calls against rate-limited APIs (Cryptohopper documents 2 req/s), so it is
# polled at most once per _EXPOSURE_TTL_S and the engine reads the cache.
# Staleness is bounded: a snapshot older than _EXPOSURE_MAX_AGE_S is not
# reported as data, it is reported as absence-of-data, which fails closed.
_EXPOSURE_TTL_S = 60
_EXPOSURE_MAX_AGE_S = 300
_exp_lock = threading.Lock()
_exp_snapshot: Optional[Dict[str, Any]] = None
_exp_taken_t = 0.0


# ------------------------------------------------------------------ storage
def _all() -> List[Dict[str, Any]]:
    return storage.get_setting(_SETTING, []) or []


def _save(rows: List[Dict[str, Any]]) -> None:
    storage.set_setting(_SETTING, rows)
    _cache.clear()
    _cache_stamp.clear()


def fingerprint(secret: str) -> str:
    if not secret:
        return ""
    return hashlib.sha256(secret.encode()).hexdigest()[:6]


def _redact_flat(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    for f in _SECRET_FIELDS:
        if out.get(f):
            out[f + "_fp"] = fingerprint(str(cfg[f]))
            out[f] = _MASK
        else:
            out.pop(f, None)
    return out


def redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Masks secrets at every level, including per-bot credentials (Altrady
    stores one key/secret pair inside each entry of `bots`)."""
    out = _redact_flat(cfg)
    if isinstance(out.get("bots"), list):
        out["bots"] = [_redact_flat(b) if isinstance(b, dict) else b
                       for b in out["bots"]]
    return out


def list_hosts() -> List[Dict[str, Any]]:
    """Safe for the API: secrets masked, at every nesting level."""
    return [redact(h) for h in _all()]


def get(name: str) -> Optional[Dict[str, Any]]:
    """Full config INCLUDING secrets. Never return this over HTTP."""
    return next((h for h in _all() if h.get("name") == name), None)


def _merge_secrets(new: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
    """A blank or masked secret in an update keeps the stored value, so the UI
    can re-save without the operator retyping keys. Applied flat and per-bot;
    bots are matched by bot_id, then by name."""
    merged = dict(old)
    for k, v in new.items():
        if k in _SECRET_FIELDS and (not v or v == _MASK):
            continue
        merged[k] = v

    if isinstance(new.get("bots"), list):
        old_bots = old.get("bots") or []

        def _match(nb: Dict[str, Any]) -> Dict[str, Any]:
            for ob in old_bots:
                if not isinstance(ob, dict):
                    continue
                if nb.get("bot_id") and nb.get("bot_id") == ob.get("bot_id"):
                    return ob
                if nb.get("name") and nb.get("name") == ob.get("name"):
                    return ob
            return {}

        rebuilt = []
        for nb in new["bots"]:
            if not isinstance(nb, dict):
                continue
            ob = _match(nb)
            row = dict(ob)
            for k, v in nb.items():
                if k in _SECRET_FIELDS and (not v or v == _MASK):
                    continue
                row[k] = v
            rebuilt.append(row)
        merged["bots"] = rebuilt
    return merged


def upsert(cfg: Dict[str, Any]) -> Dict[str, Any]:
    name = str(cfg.get("name", "")).strip()
    kind = str(cfg.get("kind", "")).strip()
    if not name:
        raise ValueError("host needs a name")
    if kind not in sh.host_kinds():
        raise ValueError("unknown host kind %r (have: %s)"
                         % (kind, ", ".join(sh.host_kinds()) or "none"))

    rows = _all()
    existing = next((h for h in rows if h.get("name") == name), None)
    merged = _merge_secrets(cfg, existing or {})
    merged.setdefault("read_only", True)
    merged.setdefault("created_t", int(time.time()))
    merged["name"], merged["kind"] = name, kind

    rows = [h for h in rows if h.get("name") != name] + [merged]
    _save(rows)
    storage.log_agent("info", "host_upsert",
                      "%s (%s) read_only=%s" % (name, kind, merged["read_only"]))
    return redact(merged)


def remove(name: str) -> bool:
    rows = _all()
    kept = [h for h in rows if h.get("name") != name]
    if len(kept) == len(rows):
        return False
    _save(kept)
    storage.log_agent("info", "host_remove", name)
    return True


def set_control_enabled(name: str, enabled: bool) -> Dict[str, Any]:
    """Arming bot control. Starting and stopping someone's grid bot moves real
    money, so it gets the same explicit, logged act as arming a venue."""
    h = get(name)
    if not h:
        raise ValueError("no host %r" % name)
    h["read_only"] = not enabled
    rows = [x for x in _all() if x.get("name") != name] + [h]
    _save(rows)
    storage.log_agent("change", "host_control",
                      "%s bot control %s" % (name,
                                             "ENABLED" if enabled else "disabled"))
    return redact(h)


# ------------------------------------------------------------------ adapters
def host(name: str) -> sh.StrategyHost:
    """Built lazily and cached. A construction failure raises: a host that
    cannot be built must not look healthy."""
    now = time.time()
    if name in _cache and now - _cache_stamp.get(name, 0) < _CACHE_TTL_S:
        return _cache[name]
    cfg = get(name)
    if not cfg:
        raise ValueError("no host %r" % name)
    h = sh.build_host(cfg["kind"], cfg)
    _cache[name] = h
    _cache_stamp[name] = now
    return h


def health(name: str) -> Dict[str, Any]:
    """Never raises: a broken host is a health row, not a 500."""
    try:
        h = host(name).health()
        return {"name": h.name, "reachable": h.reachable,
                "authenticated": h.authenticated, "read_only": h.read_only,
                "latency_ms": h.latency_ms, "detail": h.detail}
    except Exception as e:                                # noqa: BLE001
        return {"name": name, "reachable": False, "authenticated": False,
                "read_only": True, "detail": str(e)[:300]}


def health_all() -> List[Dict[str, Any]]:
    return [health(h["name"]) for h in _all()]


def bots_all() -> List[Dict[str, Any]]:
    """Every bot on every host, with running state. A host that cannot answer
    contributes an error row, not an empty list — absence of rows must never
    read as absence of bots."""
    out: List[Dict[str, Any]] = []
    for cfg in _all():
        name = str(cfg.get("name"))
        try:
            for b in host(name).bots():
                out.append({"host": name, "bot_id": b.bot_id, "name": b.name,
                            "running": b.running, "strategy": b.strategy,
                            "pairs": b.pairs})
        except Exception as e:                            # noqa: BLE001
            out.append({"host": name, "error": str(e)[:200]})
    return out


def start_bot(host_name: str, bot_id: str) -> Dict[str, Any]:
    a = host(host_name).start_bot(bot_id)
    storage.log_agent("change", "host_bot_start",
                      "%s/%s changed=%s %s" % (host_name, bot_id, a.changed,
                                               a.detail))
    _invalidate_exposure()
    return {"bot_id": a.bot_id, "running": a.running, "changed": a.changed,
            "detail": a.detail}


def stop_bot(host_name: str, bot_id: str) -> Dict[str, Any]:
    a = host(host_name).stop_bot(bot_id)
    storage.log_agent("change", "host_bot_stop",
                      "%s/%s changed=%s %s" % (host_name, bot_id, a.changed,
                                               a.detail))
    _invalidate_exposure()
    return {"bot_id": a.bot_id, "running": a.running, "changed": a.changed,
            "detail": a.detail}


# ------------------------------------------------------------------ exposure
def _invalidate_exposure() -> None:
    global _exp_taken_t
    with _exp_lock:
        _exp_taken_t = 0.0


def _take_snapshot() -> Dict[str, Any]:
    """One real poll of every configured host. Failures are counted, never
    absorbed: `unreadable_hosts` > 0 means some money at risk is invisible."""
    states: List[sh.BotState] = []
    unreadable = 0
    details: List[str] = []
    for cfg in _all():
        name = str(cfg.get("name"))
        try:
            h = host(name)
            for b in h.bots():
                states.append(h.bot_state(b.bot_id))
        except Exception as e:                            # noqa: BLE001
            unreadable += 1
            details.append("%s: %s" % (name, str(e)[:150]))

    ex = sh.hosted_exposure(states)
    return {"unrealized_pnl": ex.unrealized_pnl,
            "unvalued_bots": ex.unvalued_bots,
            "open_positions": ex.open_positions,
            "unreadable_hosts": unreadable,
            "hosts": len(_all()),
            "trustworthy": ex.trustworthy and unreadable == 0,
            "detail": "; ".join(details)[:400],
            "as_of": int(time.time())}


def exposure() -> Dict[str, Any]:
    """The engine's view of hosted money at risk. Cached, rate-limit friendly,
    and honest about age:

      - no hosts configured  -> a zero snapshot marked trustworthy, no network
      - snapshot fresh       -> served from cache
      - refresh fails        -> the FAILURE is the result (unreadable_hosts),
                                not the previous numbers dressed up as current
      - older than max age   -> reported untrustworthy even if refresh keeps
                                failing silently; time alone rots trust
    """
    global _exp_snapshot, _exp_taken_t
    if not _all():
        return {"unrealized_pnl": 0.0, "unvalued_bots": 0, "open_positions": 0,
                "unreadable_hosts": 0, "hosts": 0, "trustworthy": True,
                "detail": "", "as_of": int(time.time())}

    with _exp_lock:
        age = time.time() - _exp_taken_t
        if _exp_snapshot is not None and age < _EXPOSURE_TTL_S:
            return dict(_exp_snapshot)
        try:
            snap = _take_snapshot()
            _exp_snapshot, _exp_taken_t = snap, time.time()
            return dict(snap)
        except Exception as e:                            # noqa: BLE001
            # _take_snapshot already absorbs per-host failures; reaching here
            # means something structural broke. That is not a zero.
            return {"unrealized_pnl": 0.0, "unvalued_bots": 0,
                    "open_positions": 0, "unreadable_hosts": len(_all()),
                    "hosts": len(_all()), "trustworthy": False,
                    "detail": "snapshot failed: %s" % str(e)[:200],
                    "as_of": int(time.time())}
