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
import json
import os
import secrets
from typing import Any, Dict, Optional

import yaml
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

import analysis
import decisions
import instruments
import news_calendar
import params_store
import storage

BASE = os.path.dirname(os.path.abspath(__file__))
_TOKEN_FILE = os.path.join(BASE, "state", "dashboard_token")


def _load_cfg() -> Dict[str, Any]:
    try:
        with open(os.path.join(BASE, "config.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    d = cfg.get("dashboard") or {}
    return {"host": d.get("host", "127.0.0.1"), "port": int(d.get("port", 8767))}


def get_token() -> str:
    """Control token: env wins; else a generated secret persisted with 0600
    perms under state/ (gitignored). Never in the repo, config, or the DB."""
    env = os.environ.get("DASHBOARD_TOKEN")
    if env:
        return env
    try:
        with open(_TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(_TOKEN_FILE, 0o600)
    return tok


def require_token(x_dashboard_token: Optional[str] = Header(None)) -> None:
    if not x_dashboard_token or not secrets.compare_digest(
            x_dashboard_token, get_token()):
        raise HTTPException(401, "missing/invalid X-Dashboard-Token")


app = FastAPI(title="SLC multi-asset dashboard", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    storage.init()
    instruments.seed_defaults()


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "dashboard", "multiasset.html"),
                        media_type="text/html")


# ---------------------------------------------------------------- read views
@app.get("/api/health")
def api_health():
    return analysis.health()


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
        args.append(int(__import__("time").time()) - days * 86400)
    args.append(limit)
    return {"trades": storage.query(
        "SELECT * FROM trades WHERE %s ORDER BY entry_time DESC LIMIT ?"
        % " AND ".join(where), tuple(args))}


@app.get("/api/settings")
def api_settings():
    s = storage.all_settings()
    for k in ("telegram_bot_token", "telegram_chat_id", "discord_webhook_url",
              "tradingview_webhook_token"):
        if s.get(k):
            s[k] = "•••set•••"
    return {"settings": s, "bounds": {k: list(v) for k, v in params_store.BOUNDS.items()}}


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
    print(" SLC multi-asset dashboard")
    print(" URL   : http://%s:%d" % (cfg["host"], cfg["port"]))
    print(" Token : %s (controls only; env DASHBOARD_TOKEN overrides)"
          % (_TOKEN_FILE if not os.environ.get("DASHBOARD_TOKEN") else "from env"))
    print("=" * 60)
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="warning")
