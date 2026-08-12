"""Multi-asset control dashboard — FastAPI backend (Phase 4).

Separate process from server.py by design:
  * server.py (Flask, LAN) keeps serving the EA protocol — the EA must reach
    it from another machine, so it cannot bind localhost.
  * THIS app is the control surface and binds 127.0.0.1 ONLY by default
    (constraint 9). It talks to the same SQLite/WAL database.

Security model:
  * GET endpoints are read-only analysis views (no secrets — the analysis
    layer never returns credentials).
  * Every mutating endpoint requires the X-Dashboard-Token header. The token
    comes from the DASHBOARD_TOKEN env var, else an auto-generated one stored
    at state/dashboard_token (0600). Never stored in the repo or the DB.
  * The live-trading switch is NOT here — it lives in live_switch.py (own
    module, two-step confirm, promotion-gate checked) and is mounted under
    /api/live. Code isolation per constraint 7.

Run:  python3 dashboard_api.py         # http://127.0.0.1:8767
"""
import os
import re
import threading
import time
from typing import Any, Dict, Optional

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

import alerts
import analysis
import decisions
import instruments
import news_calendar
import params_store
import reconcile
import storage
from dash_auth import get_token, require_token, _TOKEN_FILE

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_cfg() -> Dict[str, Any]:
    try:
        with open(os.path.join(BASE, "config.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    d = cfg.get("dashboard") or {}
    return {"host": d.get("host", "127.0.0.1"), "port": int(d.get("port", 8767))}


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    storage.init()
    instruments.seed_defaults()
    start_watchdog()
    yield


app = FastAPI(title="Keel multi-asset dashboard", docs_url=None, redoc_url=None,
              lifespan=_lifespan)


# ======================================================================
# Dead-man, second arm (S3) + standby alert relay
#
# server.py watches the engine heartbeat from inside the process that hosts
# the engine, which catches a dead engine THREAD and cannot catch a dead
# PROCESS. This process is the other side of that: it is separate, it reads
# the same two heartbeats out of the shared database, and it can therefore say
# "server.py is gone" — which is the sentence the in-process watchdog can never
# produce about itself.
#
# It also stands by on the alerts relay. alerts.own_relay() is a lease: while
# server.py renews it every tick this process delivers nothing, and about two
# minutes after server.py stops renewing, this process takes over. That matters
# specifically here, because the alert about server.py dying is otherwise
# recorded by a process that no longer has anyone to deliver it. Taking the
# relay means owning a notifier worker — queueing into a process that drains
# nothing is exactly the live_switch bug, so notifier.start() is called before
# the first relay attempt, not hoped for.
#
# The heartbeat-staleness logic is a deliberate ~15-line duplicate of
# server.py's. The alternative is importing server.py here, which would drag
# Flask, the engine and the agent into the control-plane process purely to
# share a subtraction.
# ======================================================================
WATCHDOG_TICK_S = 30
DEADMAN_STALE_S = 180            # same window analysis.health() calls "alive"
MONITOR_STALE_S = 120            # server.py's monitor ticks every 15s
RECON_EXPECTED_EVERY_S = 60      # server.py's reconcile cadence

_watch: Dict[str, Any] = {"started_t": time.time(), "ticks": 0,
                          "last_tick_t": 0.0, "relay": {}, "started": False}


def _stamp(key: str) -> Optional[float]:
    """A timestamp out of settings, or None when missing/unreadable. Never a
    numeric default: the absence of a heartbeat is the thing being detected."""
    try:
        v = storage.get_setting(key, None)
    except Exception:
        return None
    if v in (None, "", 0):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _age(key: str, now: float) -> Optional[float]:
    hb = _stamp(key)
    return None if hb is None else now - hb


def deadman_state(now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    eng, mon = _age("engine_heartbeat_t", now), _age("safety_monitor_t", now)
    return {
        "engine_heartbeat_age_s": None if eng is None else round(eng),
        "engine_alive": eng is not None and eng <= DEADMAN_STALE_S,
        "safety_monitor_age_s": None if mon is None else round(mon),
        "safety_monitor_alive": mon is not None and mon <= MONITOR_STALE_S,
        "watched_from": "dashboard process (separate from the engine)",
        "blind_spot": "if this host dies, nothing on it can page — that needs "
                      "an outbound ping from the engine cycle",
    }


def deadman_tick(now: Optional[float] = None, send=None) -> Dict[str, Any]:
    """One watchdog pass. Records; delivers only if it holds the relay lease."""
    now = time.time() if now is None else now
    grace = now - _watch["started_t"] <= MONITOR_STALE_S
    out: Dict[str, Any] = {"engine": "ok", "monitor": "ok", "relay": {}}

    eng = _age("engine_heartbeat_t", now)
    if eng is None and not grace:
        out["engine"] = "never"
        alerts.raise_alert(
            "engine_deadman",
            "no engine heartbeat has EVER been written, seen from the "
            "dashboard process — the engine is not running.",
            severity=alerts.P1, dedupe_key="never")
    elif eng is not None and eng > DEADMAN_STALE_S:
        out["engine"] = "stalled"
        alerts.raise_alert(
            "engine_deadman",
            "engine heartbeat is %ds old (limit %ds), seen from the dashboard "
            "process — engine loop stopped, or server.py is gone."
            % (int(eng), DEADMAN_STALE_S),
            severity=alerts.P1, dedupe_key="stalled")

    mon = _age("safety_monitor_t", now)
    if mon is None and not grace:
        out["monitor"] = "never"
        alerts.raise_alert(
            "safety_monitor_down",
            "the safety monitor in server.py has never checked in — kill "
            "switches, reconciliation and the dead-man are unwatched.",
            severity=alerts.P1, dedupe_key="never")
    elif mon is not None and mon > MONITOR_STALE_S:
        out["monitor"] = "stalled"
        alerts.raise_alert(
            "safety_monitor_down",
            "the safety monitor in server.py last checked in %ds ago (limit "
            "%ds) — nothing is watching the rails." % (int(mon), MONITOR_STALE_S),
            severity=alerts.P1, dedupe_key="stalled")

    if send is None:
        import notifier                  # only the relay owner needs it
        notifier.start()
    out["relay"] = alerts.relay_once(send=send)
    _watch["ticks"] += 1
    _watch["last_tick_t"] = now
    _watch["relay"] = out["relay"]
    return out


def _watchdog_loop(interval: int = WATCHDOG_TICK_S) -> None:
    _watch["started_t"] = time.time()
    while True:
        try:
            deadman_tick()
        except Exception as e:            # a watchdog may never die quietly
            print("[watchdog] tick error:", e)
        time.sleep(interval)


def start_watchdog(interval: int = WATCHDOG_TICK_S) -> None:
    if _watch["started"]:
        return
    _watch["started"] = True
    threading.Thread(target=_watchdog_loop, args=(interval,),
                     daemon=True).start()


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "dashboard", "multiasset.html"),
                        media_type="text/html")


# ---------------------------------------------------------------- read views
@app.get("/api/health")
def api_health():
    h = analysis.health()
    # A growing undelivered backlog or an unacknowledged page is health
    # information: it means the relay owner is gone, or a human has not looked.
    h["alerts"] = alerts.health()
    h["deadman"] = deadman_state()
    return h


@app.get("/api/performance")
def api_performance(mode: str = "paper", days: Optional[int] = None):
    perf = analysis.regime_session_performance(mode, days)
    # per strategy×class cells for the performance table
    rows = storage.query(
        "SELECT COALESCE(strategy,'slc') strategy, "
        "COALESCE(asset_class,'') asset_class, COUNT(*) n, "
        "ROUND(AVG(r_multiple),3) expectancy_r, ROUND(SUM(pnl),2) pnl, "
        "ROUND(100.0*SUM(CASE WHEN r_multiple>0 THEN 1 ELSE 0 END)/COUNT(*),1) win_rate "
        "FROM trades WHERE status='closed' AND mode=? "
        "GROUP BY strategy, asset_class", (mode,))
    perf["by_strategy_class"] = rows
    return perf


@app.get("/api/studies/regime_session")
def api_regime_session(mode: str = "paper", days: Optional[int] = None):
    return analysis.regime_session_performance(mode, days)


@app.get("/api/studies/exposure")
def api_exposure(mode: str = "paper"):
    base_risk = float(storage.get_setting("risk_pct", 1.0))
    snap = analysis.exposure_snapshot(mode, base_risk)
    snap["cap"] = float(storage.get_setting("max_bucket_exposure", 2.0))
    return snap


@app.get("/api/studies/drawdown")
def api_drawdown(mode: str = "paper", days: Optional[int] = None):
    return analysis.drawdown_forensics(mode, days)


@app.get("/api/studies/promotion")
def api_promotion():
    return analysis.promotion_status()


@app.get("/api/decisions")
def api_decisions(limit: int = 200, hours: int = 24,
                  strategy: Optional[str] = None, symbol: Optional[str] = None,
                  stage: Optional[str] = None, action: Optional[str] = None,
                  since: Optional[int] = None):
    return {"funnel": decisions.funnel(hours),
            "recent": decisions.recent(limit, strategy=strategy, symbol=symbol,
                                       stage=stage, action=action, since=since)}


@app.get("/api/param_changes")
def api_param_changes(limit: int = 100, unacked: bool = False):
    return {"changes": params_store.recent_changes(limit, unacked_only=unacked)}


@app.get("/api/instruments")
def api_instruments():
    return {"instruments": instruments.all_rows()}


@app.get("/api/news_calendar")
def api_news_calendar(hours: int = 48):
    return {"status": news_calendar.status(),
            "upcoming": news_calendar.upcoming(hours)}


@app.get("/api/trades")
def api_trades(mode: Optional[str] = None, symbol: Optional[str] = None,
               days: Optional[int] = None, status: Optional[str] = None,
               limit: int = Query(200, le=1000)):
    where, args = ["1=1"], []
    for col, v in (("mode", mode), ("symbol", symbol), ("status", status)):
        if v and v != "all":
            where.append("%s=?" % col)
            args.append(v)
    if days:
        where.append("entry_time >= ?")
        args.append(int(time.time()) - days * 86400)
    args.append(limit)
    return {"trades": storage.query(
        "SELECT * FROM trades WHERE %s ORDER BY entry_time DESC LIMIT ?"
        % " AND ".join(where), tuple(args))}


@app.get("/api/analysis_now")
def api_analysis_now(minutes: int = 90):
    """What the engine is looking at right now: the latest decision row per
    symbol×speed within the window (analysis notes, stand-asides, skips,
    executions alike). Empty until the engine is cycling with a live feed."""
    since = int(time.time()) - minutes * 60
    rows = storage.query(
        "SELECT d.* FROM decisions d JOIN (SELECT symbol, trade_mode, MAX(id) mid "
        "FROM decisions WHERE t>=? GROUP BY symbol, trade_mode) x ON d.id=x.mid "
        "ORDER BY d.symbol, d.trade_mode", (since,))
    return {"rows": rows, "window_min": minutes}


# ------------------------------------------------------- alerts (S4)
# Reads are open like every other read view here: an alert row carries a kind,
# a severity and a sentence about what broke, never a credential. Acknowledging
# one is a mutation — it is a human saying "I have this" and it silences the
# unacknowledged-pages count — so it is token-gated like every other control.
@app.get("/api/alerts")
def api_alerts(limit: int = Query(100, le=1000),
               severity: Optional[str] = None, kind: Optional[str] = None,
               since: Optional[int] = None, undelivered: bool = False):
    rows = alerts.recent(limit, severity=severity, kind=kind, since=since,
                         undelivered=undelivered)
    return {"alerts": rows, "health": alerts.health(),
            "tiers": {"P1": "page", "P2": "alert", "P3": "digest",
                      "P4": "log only"}}


@app.post("/api/alerts/{alert_id}/ack", dependencies=[Depends(require_token)])
def api_alert_ack(alert_id: int):
    return {"ok": alerts.acknowledge(alert_id), "id": alert_id}


# ------------------------------------------------- reconciliation (S4)
@app.get("/api/reconcile")
def api_reconcile():
    """The last reconciliation report, as produced by the engine process.

    This endpoint deliberately does NOT run a reconciliation. Two reasons, and
    both matter: this process has no MT5 drop copy, so a report built here
    would compare the book against a strictly smaller set of sources and could
    reach a different verdict from the one the engine acts on; and
    venues.positions_all() is a network call, which has no business on a read
    view a dashboard polls.

    When there is no report, or the report is old, this says so and reports
    what reconcile.should_halt_entries would make of what we have — which for
    "no report" is True, because a verdict we do not have is not a clean one.
    """
    rep = storage.get_setting("recon_last_report", None)
    rep = rep if isinstance(rep, dict) else None
    now = int(time.time())
    age = (now - int(rep.get("t") or 0)) if rep else None
    return {
        "report": rep,
        "age_s": age,
        "state": "never run" if rep is None else (
            "stale" if age is not None and age > 3 * RECON_EXPECTED_EVERY_S
            else "current"),
        # Consumed, not reimplemented: the same function the engine gates on.
        "halt_if_consumed_now": reconcile.should_halt_entries(rep),
        "halt_reason": reconcile.halt_reason(rep),
        "halt_new_entries": bool(storage.get_setting("halt_new_entries", False)),
        "note": "reports are produced by the engine process; resuming after a "
                "halt is a human act (/api/resume) — reconciliation never heals",
    }


# Anything whose KEY looks like a credential is masked, wherever it appears in the
# structure. The previous version masked a hardcoded list of four names, which was
# fine until venue credentials arrived nested inside the "venues" setting: exchange
# api_key and api_secret were returned in plaintext by this endpoint, which is an
# open GET. A name list only protects the names someone remembered to add. This
# fails the other way - a new secret is masked before anyone thinks about it, and
# the cost of over-masking is a dull dashboard field, not a drained account.
_SECRET_NAME = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|token|password|passwd|passphrase"
    r"|webhook|credential|private[_-]?key)")


def _redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if value in (None, "", 0, False):
        return value                       # nothing to hide, and "unset" is useful
    if key and _SECRET_NAME.search(key):
        return "•••set•••"
    return value


@app.get("/api/settings")
def api_settings():
    return {"settings": _redact(storage.all_settings()),
            "bounds": {k: list(v) for k, v in params_store.BOUNDS.items()}}


# -------------------------------------------------------------- venues
# Reads are open like every other read view. Everything that changes a venue,
# and above all arming one for execution, is token-gated.
@app.get("/api/venues")
def api_venues():
    """Every ccxt exchange is offered, deliberately. Which ones are reachable
    depends on where the bot is running, not on this code: binance.com is 451
    from the US and fine from India. The health check reports per-location
    reality; nothing here is allow-listed by geography."""
    import venues
    import brokers
    exchanges = []
    try:
        import ccxt
        exchanges = sorted(ccxt.exchanges)
    except ImportError:
        pass
    return {"venues": venues.list_venues(), "kinds": brokers.kinds(),
            "exchanges": exchanges, "health": venues.health_all()}


@app.get("/api/venues/positions")
def api_venue_positions():
    """What the VENUES say we hold, for reconciliation against the local book."""
    import venues
    return {"positions": venues.positions_all()}


@app.post("/api/venues", dependencies=[Depends(require_token)])
def api_venue_upsert(body: Dict[str, Any] = Body(...)):
    import venues
    try:
        v = venues.upsert(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "venue": v, "health": venues.health(v["name"])}


@app.post("/api/venues/{name}/test", dependencies=[Depends(require_token)])
def api_venue_test(name: str):
    import venues
    return {"health": venues.health(name)}


@app.post("/api/venues/{name}/trading", dependencies=[Depends(require_token)])
def api_venue_trading(name: str, body: Dict[str, Any] = Body(...)):
    """Arm or disarm execution on a venue. Separate from adding it on purpose:
    pasting an API key must never be the same act as enabling live orders."""
    import venues
    try:
        return {"ok": True, "venue": venues.set_trading_enabled(
            name, bool(body.get("enabled", False)))}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/venues/{name}/delete", dependencies=[Depends(require_token)])
def api_venue_delete(name: str):
    import venues
    return {"ok": venues.remove(name)}


# ------------------------------------------------------------- controls
@app.post("/api/params", dependencies=[Depends(require_token)])
def api_set_params(body: Dict[str, Any] = Body(...)):
    """Risk-parameter adjustment WITHIN pre-set safe bounds only — writes go
    through params_store (origin=human): whitelist + bounds + code ceilings
    enforced there, every change recorded with old/new values."""
    changed, rejected = {}, {}
    for k, v in body.items():
        try:
            changed[k] = params_store.set_param(k, v, origin="human",
                                                reason="dashboard (multiasset)")
        except params_store.ParamRejected as e:
            rejected[k] = str(e)
    return {"ok": not rejected, "changed": changed, "rejected": rejected}


@app.post("/api/toggle", dependencies=[Depends(require_token)])
def api_toggle(body: Dict[str, Any] = Body(...)):
    """Enable/disable a strategy plugin or an instrument."""
    kind, name = body.get("kind"), str(body.get("name", "")).strip()
    enabled = bool(body.get("enabled", True))
    if kind == "strategy" and name:
        storage.set_setting("strategy_%s_enabled" % name.lower(), enabled)
        storage.log_agent("info", "strategy_toggle",
                          "%s -> %s (dashboard)" % (name, enabled))
        return {"ok": True}
    if kind == "pair" and name:
        pairs = storage.get_setting("enabled_pairs", []) or []
        sym = name.upper()
        if enabled and sym not in pairs:
            pairs = pairs + [sym]
        if not enabled and sym in pairs:
            pairs = [p for p in pairs if p != sym]
        storage.set_setting("enabled_pairs", pairs)
        storage.log_agent("info", "pair_toggle",
                          "%s -> %s (dashboard)" % (sym, enabled))
        return {"ok": True, "enabled_pairs": pairs}
    raise HTTPException(400, "kind must be strategy|pair with a name")


@app.post("/api/halt", dependencies=[Depends(require_token)])
def api_halt():
    params_store.set_param("halt_new_entries", True, origin="human",
                           reason="manual halt (multiasset dashboard)")
    return {"ok": True}


@app.post("/api/resume", dependencies=[Depends(require_token)])
def api_resume():
    params_store.set_param("halt_new_entries", False, origin="human",
                           reason="manual resume (multiasset dashboard)")
    return {"ok": True}


@app.post("/api/param_changes/ack/{change_id}",
          dependencies=[Depends(require_token)])
def api_ack(change_id: int):
    return {"ok": params_store.ack_change(change_id)}


# Live-trading switch: separate module, mounted only if present (Phase 5).
try:
    import live_switch
    app.include_router(live_switch.router, prefix="/api/live")
except ImportError:
    pass


if __name__ == "__main__":
    import uvicorn
    cfg = _load_cfg()
    tok = get_token()
    print("=" * 60)
    print(" Keel multi-asset dashboard")
    print(" URL   : http://%s:%d" % (cfg["host"], cfg["port"]))
    print(" Token : %s (controls only; env DASHBOARD_TOKEN overrides)"
          % (_TOKEN_FILE if not os.environ.get("DASHBOARD_TOKEN") else "from env"))
    print("=" * 60)
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="warning")
