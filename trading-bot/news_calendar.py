"""Scheduled-event calendar + per-asset-class news blackout gate (Phase 3).

This is the component the Phase 1 review found entirely missing: the existing
news agent is REACTIVE (headline sentiment on open positions); this module is
PROACTIVE — it knows this week's scheduled data releases and blocks NEW
entries in a window around events that matter to the instrument.

Per-class policy (explicit, constraint 5 — configurable via config.yaml
`news_calendar.windows`, minutes before/after a release):
    forex/metals/energies : high-impact events of the instrument's currencies
                            or entities, default ±30 min
    indices               : high-impact events of the index's country
                            currency, default ±30 min
    crypto                : NO scheduled blackout by default (explicit policy:
                            24/7 market; the session layer already halves
                            weekend risk). Add windows in config to change.

Event source: the ForexFactory weekly JSON mirror (same feed the legacy bot's
news layer used). Fetch failures degrade gracefully: existing events keep
working, an empty calendar means NO blackout (matching the legacy allow-on-
failure semantics) but the staleness is visible via /api/news_calendar and
the calendar_fresh flag. Manual events can be injected through add_manual().

Every blocked entry is logged twice: a `decisions` row (engine) with the
exact event in news_ctx, and a `news_actions` row here — the audit trail
answers "which headline/data point caused this" (constraint 5).
"""
import json
import hashlib
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import instruments
import storage

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# minutes (before, after) per asset class per impact level
DEFAULT_WINDOWS: Dict[str, Dict[str, List[int]]] = {
    "forex":    {"high": [30, 30]},
    "metals":   {"high": [30, 30]},
    "energies": {"high": [30, 30]},
    "indices":  {"high": [30, 30]},
    "crypto":   {},                  # explicit: no scheduled blackout
}

_cfg_lock = threading.Lock()
_cfg: Dict[str, Any] = {"windows": DEFAULT_WINDOWS, "enabled": True,
                        "refresh_min": 60,
                        # fail-safe policy when the calendar cannot be
                        # refreshed (constraint 4: don't trade blind):
                        #   halt = block new entries for classes that HAVE
                        #          blackout windows once stale (crypto etc.
                        #          with no windows are unaffected)
                        #   warn = keep trading, surface staleness only
                        "on_stale": "halt",
                        "stale_after_hours": 48}
_last_fetch = {"t": 0.0, "ok": False, "error": ""}
_LAST_OK_KEY = "news_calendar_last_ok"   # persisted across restarts


def configure(cfg: Optional[Dict[str, Any]]) -> None:
    """Apply the config.yaml `news_calendar` block (missing keys keep defaults)."""
    if not cfg:
        return
    with _cfg_lock:
        if "enabled" in cfg:
            _cfg["enabled"] = bool(cfg["enabled"])
        if "refresh_min" in cfg:
            _cfg["refresh_min"] = int(cfg["refresh_min"])
        if cfg.get("on_stale") in ("halt", "warn"):
            _cfg["on_stale"] = cfg["on_stale"]
        if "stale_after_hours" in cfg:
            _cfg["stale_after_hours"] = int(cfg["stale_after_hours"])
        for cls, wins in (cfg.get("windows") or {}).items():
            _cfg["windows"][cls] = {str(k): [int(v[0]), int(v[1])]
                                    for k, v in (wins or {}).items()}


def _hash(title: str, t: int, entity: str) -> str:
    return hashlib.sha1(("%s|%d|%s" % (title, t, entity)).encode()).hexdigest()[:16]


def _store_event(t: int, kind: str, source: str, entities: List[str],
                 impact: str, title: str, payload: Any = None) -> Optional[int]:
    h = _hash(title, t, ",".join(entities))
    try:
        if storage.query_one("SELECT id FROM news_events WHERE hash=?", (h,)):
            return None                      # already known — dedupe
        return storage.execute(
            "INSERT OR IGNORE INTO news_events(t,kind,source,entities,impact,"
            "title,payload,hash) VALUES(?,?,?,?,?,?,?,?)",
            (t, kind, source, json.dumps(entities), impact, title,
             json.dumps(payload, default=str) if payload else None, h))
    except Exception as e:
        print("news_calendar store error:", e)
        return None


def fetch_forexfactory(url: str = FF_URL, timeout: int = 15) -> int:
    """Pull this week's scheduled releases; returns number of NEW rows.
    Failure is non-fatal — the previous calendar keeps serving."""
    import requests
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "slc-bot-calendar/1.0"})
        r.raise_for_status()
        items = r.json()
    except Exception as e:
        _last_fetch.update({"t": time.time(), "ok": False, "error": str(e)[:200]})
        print("news_calendar fetch failed:", e)
        return 0
    n = 0
    for it in items if isinstance(items, list) else []:
        try:
            title = str(it.get("title") or "")[:200]
            country = str(it.get("country") or "").upper()
            impact = str(it.get("impact") or "").lower()
            date = it.get("date")
            if not (title and country and date):
                continue
            t = int(datetime.fromisoformat(str(date)).timestamp())
            row = _store_event(t, "scheduled", "forexfactory", [country], impact,
                               title, {"forecast": it.get("forecast"),
                                       "previous": it.get("previous")})
            if row:
                n += 1
        except Exception:
            continue
    _last_fetch.update({"t": time.time(), "ok": True, "error": ""})
    try:
        storage.set_setting(_LAST_OK_KEY, int(time.time()))
    except Exception:
        pass
    if n:
        print("news_calendar: %d new scheduled events" % n)
    return n


def calendar_stale(now: Optional[float] = None) -> bool:
    """True when the calendar hasn't refreshed successfully within the grace
    window (persisted across restarts; a fresh install that has NEVER fetched
    is stale by definition — fail safe until the first successful pull)."""
    now = now if now is not None else time.time()
    last_ok = float(storage.get_setting(_LAST_OK_KEY, 0) or 0)
    with _cfg_lock:
        grace = _cfg["stale_after_hours"] * 3600
    return (now - last_ok) > grace


def add_manual(t: int, title: str, entities: List[str],
               impact: str = "high") -> Optional[int]:
    """Inject an event by hand (dashboard/ops): e.g. an unscheduled central
    bank speech, or a crypto-specific event like an ETF decision."""
    return _store_event(int(t), "scheduled", "manual",
                        [e.upper() for e in entities], impact, title)


def upcoming(hours: int = 48) -> List[Dict[str, Any]]:
    now = int(time.time())
    rows = storage.query(
        "SELECT * FROM news_events WHERE kind='scheduled' AND t BETWEEN ? AND ? "
        "ORDER BY t", (now - 2 * 3600, now + hours * 3600))
    for r in rows:
        try:
            r["entities"] = json.loads(r["entities"] or "[]")
        except (json.JSONDecodeError, TypeError):
            r["entities"] = []
    return rows


def is_blackout(symbol: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Return blackout context if NEW entries on `symbol` are blocked right
    now by a scheduled event, else None. Pure read — never writes."""
    with _cfg_lock:
        if not _cfg["enabled"]:
            return None
        windows = dict(_cfg["windows"].get(
            instruments.asset_class(symbol), {}))
    if not windows:
        return None                      # e.g. crypto: explicit no-blackout
    now = now if now is not None else time.time()
    # fail safe: a class that RELIES on blackout windows may not trade while
    # the calendar is unrefreshable (we would be blind to scheduled releases)
    with _cfg_lock:
        on_stale = _cfg["on_stale"]
        stale_h = _cfg["stale_after_hours"]
    if on_stale == "halt" and calendar_stale(now):
        return {"event_id": None, "title": None, "impact": None,
                "event_t": None, "entities": [],
                "reason": "news calendar stale (>%dh without a successful "
                          "refresh) — failing safe, no new entries for this "
                          "asset class" % stale_h}
    ents = {e.upper() for e in instruments.news_entities(symbol)}
    if not ents:
        return None                      # no declared entities = no matches
                                         # (never "matches everything" — the
                                         # legacy falsy-filter bug, inverted)
    lo = int(now - 24 * 3600)
    hi = int(now + 24 * 3600)
    for ev in storage.query(
            "SELECT * FROM news_events WHERE kind='scheduled' AND t BETWEEN ? AND ?",
            (lo, hi)):
        win = windows.get((ev["impact"] or "").lower())
        if not win:
            continue
        try:
            ev_ents = {str(x).upper() for x in json.loads(ev["entities"] or "[]")}
        except (json.JSONDecodeError, TypeError):
            continue
        if not (ev_ents & ents):
            continue
        before_s, after_s = win[0] * 60, win[1] * 60
        if ev["t"] - before_s <= now <= ev["t"] + after_s:
            mins = (ev["t"] - now) / 60.0
            return {
                "event_id": ev["id"], "title": ev["title"],
                "impact": ev["impact"], "event_t": ev["t"],
                "entities": sorted(ev_ents & ents),
                "reason": "news blackout: %s [%s] %s (%+d min)"
                          % (ev["title"], ev["impact"],
                             "/".join(sorted(ev_ents & ents)), int(mins)),
            }
    return None


def log_action(event_id: Optional[int], action: str, symbol: str,
               reason: str, ticket: Optional[int] = None) -> None:
    """Audit every action the news layer causes (constraint 5)."""
    try:
        storage.execute(
            "INSERT INTO news_actions(t,event_id,action,symbol,ticket,reason) "
            "VALUES(?,?,?,?,?,?)",
            (int(time.time()), event_id, action, symbol, ticket, reason[:400]))
    except Exception as e:
        print("news_actions log error:", e)


def status() -> Dict[str, Any]:
    with _cfg_lock:
        cfg = json.loads(json.dumps(_cfg))
    return {"enabled": cfg["enabled"], "windows": cfg["windows"],
            "on_stale": cfg["on_stale"],
            "last_fetch_t": int(_last_fetch["t"]), "last_fetch_ok": _last_fetch["ok"],
            "last_fetch_error": _last_fetch["error"],
            "calendar_stale": calendar_stale(),
            "upcoming_48h": len(upcoming(48))}


def start_refresher() -> None:
    """Daemon thread: refresh the calendar every `refresh_min` minutes."""
    def loop():
        while True:
            try:
                if _cfg["enabled"]:
                    fetch_forexfactory()
            except Exception as e:
                print("news_calendar refresher error:", e)
            time.sleep(max(600, _cfg["refresh_min"] * 60))
    threading.Thread(target=loop, daemon=True).start()
    print("news_calendar: refresher started (every %d min)" % _cfg["refresh_min"])
