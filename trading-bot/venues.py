"""Venue registry and credential store.

Credentials live in the runtime DB, entered through the dashboard, and nowhere
else: not in source, not in config.yaml, not in an env file that gets committed
by accident. This is the same rule the Telegram and Discord credentials already
follow, extended to every exchange and router.

Three deliberate properties:

  read-only by default   Adding a venue lets you SEE the account. Trading through
                         it is a separate, explicit act. Nobody pastes an API key
                         and accidentally arms an execution path.

  redaction at the edge  Secrets are returned as a masked marker with a fingerprint
                         so you can tell WHICH key is loaded without exposing it.

  no silent adoption     A venue the engine has never traded holds no positions it
                         believes in. Reconciliation is explicit, never implicit.
"""
import hashlib
import time
from typing import Any, Dict, List, Optional

import brokers
import storage

_SETTING = "venues"                    # list of venue config dicts
_SECRET_FIELDS = ("api_key", "api_secret", "password")
_MASK = "••••set••••"

_cache: Dict[str, Any] = {}            # name -> adapter instance
_cache_stamp: Dict[str, float] = {}
_CACHE_TTL_S = 300


# ------------------------------------------------------------------ storage
def _all() -> List[Dict[str, Any]]:
    return storage.get_setting(_SETTING, []) or []


def _save(rows: List[Dict[str, Any]]) -> None:
    storage.set_setting(_SETTING, rows)
    _cache.clear()
    _cache_stamp.clear()


def fingerprint(secret: str) -> str:
    """Last-4-of-hash, so the UI can show WHICH key is loaded without showing it."""
    if not secret:
        return ""
    return hashlib.sha256(secret.encode()).hexdigest()[:6]


def redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    for f in _SECRET_FIELDS:
        if out.get(f):
            out[f] = _MASK
            out[f + "_fp"] = fingerprint(cfg[f])
        else:
            out.pop(f, None)
    return out


def list_venues() -> List[Dict[str, Any]]:
    """Safe for the API: secrets masked."""
    return [redact(v) for v in _all()]


def get(name: str) -> Optional[Dict[str, Any]]:
    """Full config INCLUDING secrets. Never return this over HTTP."""
    return next((v for v in _all() if v.get("name") == name), None)


def upsert(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Add or update a venue. A blank/masked secret keeps the stored one, so the
    UI can re-save a venue without the operator retyping the key every time."""
    name = str(cfg.get("name", "")).strip()
    kind = str(cfg.get("kind", "")).strip()
    if not name:
        raise ValueError("venue needs a name")
    if kind not in brokers.kinds():
        raise ValueError("unknown kind %r (have: %s)"
                         % (kind, ", ".join(brokers.kinds()) or "none"))

    rows = _all()
    existing = next((v for v in rows if v.get("name") == name), None)
    merged: Dict[str, Any] = dict(existing or {})
    for k, v in cfg.items():
        if k in _SECRET_FIELDS and (not v or v == _MASK):
            continue                    # keep what is already stored
        merged[k] = v
    merged.setdefault("read_only", True)
    merged.setdefault("created_t", int(time.time()))
    merged["name"], merged["kind"] = name, kind

    rows = [v for v in rows if v.get("name") != name] + [merged]
    _save(rows)
    storage.log_agent("info", "venue_upsert",
                      "%s (%s) read_only=%s" % (name, kind, merged["read_only"]))
    return redact(merged)


def remove(name: str) -> bool:
    rows = _all()
    kept = [v for v in rows if v.get("name") != name]
    if len(kept) == len(rows):
        return False
    _save(kept)
    storage.log_agent("info", "venue_remove", name)
    return True


def set_trading_enabled(name: str, enabled: bool) -> Dict[str, Any]:
    """Arming or disarming execution on a venue. Logged either way: this is the
    switch that decides whether an adapter can place an order at all."""
    v = get(name)
    if not v:
        raise ValueError("no venue %r" % name)
    v["read_only"] = not enabled
    rows = [x for x in _all() if x.get("name") != name] + [v]
    _save(rows)
    storage.log_agent("change", "venue_trading",
                      "%s trading %s" % (name, "ENABLED" if enabled else "disabled"))
    return redact(v)


# ------------------------------------------------------------------ adapters
def adapter(name: str):
    """Built lazily and cached. A construction failure is raised, not swallowed:
    a venue that cannot be built must not look healthy."""
    now = time.time()
    if name in _cache and now - _cache_stamp.get(name, 0) < _CACHE_TTL_S:
        return _cache[name]
    cfg = get(name)
    if not cfg:
        raise ValueError("no venue %r" % name)
    a = brokers.build(cfg["kind"], cfg)
    _cache[name] = a
    _cache_stamp[name] = now
    return a


def health(name: str) -> Dict[str, Any]:
    """Never raises: a broken venue is a health row, not a 500."""
    try:
        h = adapter(name).health()
        return {"name": h.name, "reachable": h.reachable,
                "authenticated": h.authenticated, "read_only": h.read_only,
                "latency_ms": h.latency_ms, "clock_skew_s": h.clock_skew_s,
                "detail": h.detail,
                "balances": [{"currency": b.currency, "total": b.total,
                              "free": b.free} for b in h.balances[:12]]}
    except Exception as e:
        return {"name": name, "reachable": False, "authenticated": False,
                "read_only": True, "detail": str(e)[:300], "balances": []}


def health_all() -> List[Dict[str, Any]]:
    return [health(v["name"]) for v in _all()]


def positions_all() -> List[Dict[str, Any]]:
    """Live positions as the VENUES report them. Compared against the local
    trades table, this is the reconciliation signal: drift means the engine's
    picture and reality disagree, and new entries should stop until it is
    explained."""
    out = []
    for v in _all():
        try:
            for p in adapter(v["name"]).positions():
                out.append({"venue": v["name"], "symbol": p.symbol, "side": p.side,
                            "qty": p.qty, "entry_price": p.entry_price,
                            "unrealized_pnl": p.unrealized_pnl,
                            "venue_id": p.venue_id})
        except Exception as e:
            out.append({"venue": v["name"], "error": str(e)[:200]})
    return out
