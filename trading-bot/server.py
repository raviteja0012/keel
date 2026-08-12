"""Keel server.

  python server.py            # that's it — one process runs everything:
    - HTTP endpoints for the SLCDataBridge EA (feed, bars, pairs, commands)
    - the SLC strategy engine (paper/live)
    - the self-evaluation agent
    - the Telegram + Discord notifier
    - the web dashboard  ->  http://localhost:8766
"""
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

import yaml
from flask import Flask, jsonify, request, send_from_directory

import agent
import alerts
import decisions
import engine
import exposure
import instruments
from dash_auth import get_token
import news_calendar
import notifier
import params_store
import reconcile
import storage
import tv_webhook
import venues

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)

with open(os.path.join(BASE, "config.yaml")) as f:
    CFG = yaml.safe_load(f)


def seed_settings():
    """Copy yaml defaults into the DB once; DB wins afterwards."""
    defaults = {
        "trading_mode": CFG["engine"]["trading_mode"],
        "modes": CFG["engine"]["modes"],
        "paper_balance": CFG["engine"]["paper_start_balance"],
        "enabled_pairs": CFG["pairs"]["enabled"],
        # Watch-only pairs: streamed + analyzed + outcome-tracked as SHADOW
        # trades (no money, no concurrency impact) to grow the study sample.
        "watch_pairs": [
            "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
            "EURAUD", "EURCAD", "EURCHF", "EURNZD",
            "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
            "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF", "CADCHF",
            "XPDUSD", "UKOIL", "ADAUSD", "DOGUSD", "LINKUSD", "BCHUSD",
        ],
        "agent_enabled": CFG["agent"]["enabled"],
        "telegram_enabled": CFG["telegram"]["enabled"],
        "telegram_bot_token": CFG["telegram"]["bot_token"],
        "telegram_chat_id": CFG["telegram"]["chat_id"],
        "notify_signals": CFG["telegram"]["notify_signals"],
        "discord_enabled": False,
        "discord_webhook_url": "",
        "agent_disabled_pairs": [],
        "agent_disabled_modes": [],
        "notify_agent": True,
        # multi-asset rails (Phase 2): per-class concurrency + factor buckets
        "max_concurrent_per_class": CFG.get("risk", {}).get("max_concurrent_per_class", 2),
        "max_bucket_exposure": CFG.get("risk", {}).get("max_bucket_exposure", 2.0),
        "halt_new_entries": False,
        # strategy-plugin flags (strategy_<name>_enabled); SLC default-on
        "strategy_slc_enabled": True,
        # TradingView / external webhook — OFF until enabled AND a token is set
        "tradingview_webhook_enabled": False,
        "tradingview_webhook_token": "",
        "tradingview_symbol_map": {},
    }
    for k, v in CFG["risk"].items():
        defaults[k] = v
    existing = storage.all_settings()
    for k, v in defaults.items():
        if k not in existing:
            storage.set_setting(k, v)


# ======================================================================
# EA-facing endpoints (contract fixed by SLCDataBridge.mq5 — do not change)
# ======================================================================
@app.route("/api/mt5_feed", methods=["POST"])
def mt5_feed():
    engine.ingest_feed(request.get_json(force=True, silent=True) or {})
    return jsonify({"ok": True})


@app.route("/api/mt5_bars", methods=["POST"])
def mt5_bars():
    engine.ingest_bars(request.get_json(force=True, silent=True) or {})
    return jsonify({"ok": True})


@app.route("/api/pairs", methods=["GET"])
def pairs():
    enabled = storage.get_setting("enabled_pairs", [])
    watch = storage.get_setting("watch_pairs", [])
    # EA streams everything; the engine decides what is tradable vs shadow
    combined = enabled + [w for w in watch if w not in enabled]
    return jsonify({"enabled_pairs": combined, "count": len(combined)})


@app.route("/api/commands/next", methods=["GET"])
def commands_next():
    cmd = storage.next_command()
    return jsonify({"command": cmd})


@app.route("/api/commands/ack/<cmd_id>", methods=["POST"])
def commands_ack(cmd_id):
    return jsonify({"ok": storage.ack_command(cmd_id)})


# ======================================================================
# News-agent-facing endpoints (news_agent.py runs as a separate process)
# ======================================================================
@app.route("/api/status", methods=["GET"])
def status():
    """Open positions snapshot for the news agent: live broker positions
    PLUS the bot's paper trades (as pseudo-positions with negative tickets,
    so news-driven SL protection covers paper trading too)."""
    feed = engine.get_feed()
    positions = list(feed.get("open_positions", []))
    prices = feed.get("prices", {})
    for tr in storage.open_trades("paper"):
        px = prices.get(tr["symbol"], {})
        cur = px.get("bid") if tr["side"] == "buy" else px.get("ask")
        positions.append({
            "ticket": -tr["id"],                  # negative = paper trade id
            "symbol": tr["symbol"], "side": tr["side"], "lots": tr["lots"],
            "entry": tr["entry"], "sl": tr["sl"], "tp": tr["tp2"],
            "current": cur or tr["entry"],
            "unrealized_pnl": engine.trade_upnl(tr),
            "magic": 770001, "comment": "SLC#%d(paper)" % tr["id"],
        })
    return jsonify({
        "open_positions": positions,
        "ea_connected": (time.time() - feed.get("last_feed_t", 0)) < 30,
    })


@app.route("/api/commands", methods=["POST"])
def commands_post():
    """Accept SL-management commands from the news agent and enqueue them
    for the EA. Only stop-tightening commands are allowed from here —
    open/close stay exclusive to the engine.

    AUTHENTICATED, because this route can close a live position and rewrite a
    live stop, and server.host is 0.0.0.0 so the EA can reach it from another
    machine. Unauthenticated, anything on the same network could flatten the
    book. The EA is unaffected and needs no recompile: it polls
    /api/commands/next and acks on /api/commands/ack, neither of which is this
    route. The news agent shares the dashboard token, which already exists on
    this host at state/dashboard_token."""
    supplied = request.headers.get("X-Dashboard-Token", "")
    if not supplied or not secrets.compare_digest(supplied, get_token()):
        return jsonify({"ok": False,
                        "error": "missing/invalid X-Dashboard-Token"}), 401
    body = request.get_json(force=True, silent=True) or {}
    cmd_type = body.get("type")
    if cmd_type not in ("trail_sl", "move_sl_be", "close_trade"):
        return jsonify({"ok": False,
                        "error": "type must be trail_sl|move_sl_be|close_trade"}), 400
    ticket = body.get("ticket")
    if not ticket:
        return jsonify({"ok": False, "error": "ticket required"}), 400
    reason = "[news] %s" % (body.get("reason") or "")[:380]

    # ---- news cut: close a losing trade at market -----------------------
    if cmd_type == "close_trade":
        if int(ticket) < 0:                       # paper trade
            tr = storage.query_one(
                "SELECT * FROM trades WHERE id=? AND status='open' AND mode='paper'",
                (-int(ticket),))
            if not tr:
                return jsonify({"ok": False, "error": "paper trade not found/open"}), 404
            px = engine.feed_state["prices"].get(tr["symbol"])
            if not px:
                return jsonify({"ok": False, "error": "no live price to close at"}), 409
            cur = px["bid"] if tr["side"] == "buy" else px["ask"]
            engine._close_paper(tr, cur, "news cut: %s" % reason[:140])
            storage.log_agent("info", "news_command",
                              "close_trade PAPER #%d at %.5f" % (tr["id"], cur))
            return jsonify({"ok": True, "id": 0})
        cmd_id = storage.enqueue_command("close_trade",
                                         {"ticket": ticket, "lots": 0, "reason": reason})
        storage.log_agent("info", "news_command",
                          "close_trade ticket=%s (full) queued for EA" % ticket)
        return jsonify({"ok": True, "id": cmd_id})

    new_sl = body.get("new_sl")
    if not new_sl:
        return jsonify({"ok": False, "error": "new_sl required"}), 400
    new_sl = float(new_sl)

    # negative ticket = paper trade -> apply the SL directly in the DB
    # (tighten-only; there is no broker position to command)
    if int(ticket) < 0:
        tr = storage.query_one(
            "SELECT * FROM trades WHERE id=? AND status='open' AND mode='paper'",
            (-int(ticket),))
        if not tr:
            return jsonify({"ok": False, "error": "paper trade not found/open"}), 404
        tighter = (new_sl > tr["sl"]) if tr["side"] == "buy" else (new_sl < tr["sl"])
        if not tighter:
            return jsonify({"ok": True, "id": 0, "note": "SL already tighter"})
        storage.update_trade(tr["id"], {"sl": new_sl})
        storage.log_agent("info", "news_command",
                          "%s PAPER #%d sl %.5f -> %.5f" % (cmd_type, tr["id"], tr["sl"], new_sl))
        notifier.send("🛡 <b>News SL update</b> (PAPER)\n<b>%s %s</b> #%d\nSL → <code>%.5f</code>\n<i>%s</i>"
                      % (tr["side"].upper(), tr["symbol"], tr["id"], new_sl, reason[:200]))
        return jsonify({"ok": True, "id": 0})

    cmd_id = storage.enqueue_command(cmd_type, {
        "ticket": ticket, "symbol": body.get("symbol", ""),
        "new_sl": new_sl, "reason": reason,
    })
    # keep the engine's mirror in sync so its trail logic doesn't fight this
    tr = storage.query_one(
        "SELECT * FROM trades WHERE ticket=? AND status='open'", (ticket,))
    if tr:
        tighter = (new_sl > tr["sl"]) if tr["side"] == "buy" else (new_sl < tr["sl"])
        if tighter:
            storage.update_trade(tr["id"], {"sl": new_sl})
    storage.log_agent("info", "news_command",
                      "%s ticket=%s new_sl=%s" % (cmd_type, ticket, new_sl))
    return jsonify({"ok": True, "id": cmd_id})


# ======================================================================
# Dashboard API
# ======================================================================
@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE, "dashboard"), "index.html")


_SECRET_KEYS = ("telegram_bot_token", "telegram_chat_id", "discord_webhook_url",
                "tradingview_webhook_token")


def _redact(settings):
    """Never hand credentials to a dashboard client (constraint 8): presence
    is signalled, values are not."""
    out = dict(settings)
    for k in _SECRET_KEYS:
        if out.get(k):
            out[k] = "•••set•••"
    return out


@app.route("/api/state")
def state():
    feed = engine.get_feed()
    p = engine.params()
    mode = p["trading_mode"]
    bal = engine._balance(mode if mode != "off" else "paper", p)
    open_trades = [t for t in storage.open_trades() if t["mode"] != "shadow"]
    for t in open_trades:                       # annotate live/unrealized P&L
        t["upnl"] = engine.trade_upnl(t)
    return jsonify({
        "feed": feed,
        "settings": _redact(storage.all_settings()),
        "open_trades": open_trades,
        "open_pnl": round(sum(t["upnl"] or 0 for t in open_trades), 2),
        "shadow_open": len(storage.open_trades("shadow")),
        "paper_balance": bal if mode != "live" else None,
        "analysis": engine.get_last_info(),
        "server_time": int(time.time()),
        "ea_connected": (time.time() - feed.get("last_feed_t", 0)) < 30,
    })


@app.route("/api/bars")
def bars():
    symbol = request.args.get("symbol", "EURUSD")
    tf = request.args.get("tf", "1h")
    limit = int(request.args.get("limit", 300))
    return jsonify({"symbol": symbol, "tf": tf,
                    "bars": storage.get_bars(symbol, tf, limit)})


@app.route("/api/trades")
def trades():
    where, args = ["1=1"], []
    for col in ("mode", "trade_mode", "symbol", "grade", "status"):
        v = request.args.get(col)
        if v and v != "all":
            where.append("%s=?" % col)
            args.append(v)
    res = request.args.get("result")
    if res == "win":
        where.append("pnl > 0")
    elif res == "loss":
        where.append("pnl <= 0")
    days = request.args.get("days")
    if days and days != "all":
        where.append("entry_time >= ?")
        args.append(int(time.time()) - int(days) * 86400)
    rows = storage.query(
        "SELECT * FROM trades WHERE %s ORDER BY entry_time DESC LIMIT 500"
        % " AND ".join(where), tuple(args))
    return jsonify({"trades": rows})


@app.route("/api/performance")
def performance():
    return jsonify(agent.evaluate())


@app.route("/api/equity")
def equity():
    mode = request.args.get("mode", "paper")
    return jsonify({"points": storage.query(
        "SELECT t, balance, equity FROM equity WHERE mode=? ORDER BY t", (mode,))})


@app.route("/api/signals")
def signals():
    return jsonify({"signals": storage.query(
        "SELECT * FROM signals ORDER BY t DESC LIMIT 100")})


@app.route("/api/agent_log")
def agent_log():
    return jsonify({"log": storage.query(
        "SELECT * FROM agent_log ORDER BY t DESC LIMIT 100")})


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Dashboard settings writes. Risk-relevant keys go through
    params_store.set_param (origin=human) so bounds and code ceilings are
    enforced server-side and every change lands in param_changes with its
    old/new values; violations are reported back per key, not applied.
    Non-risk keys (credentials, toggles, lists) write directly as before."""
    body = request.get_json(force=True) or {}
    allowed_direct = {
        "trading_mode", "modes", "enabled_pairs", "watch_pairs",
        "agent_enabled", "telegram_enabled",
        "telegram_bot_token", "telegram_chat_id", "notify_signals", "notify_agent",
        "discord_enabled", "discord_webhook_url",
        "paper_balance", "agent_disabled_pairs", "agent_disabled_modes",
        "strategy_slc_enabled",
        "tradingview_webhook_enabled", "tradingview_webhook_token",
        "tradingview_symbol_map", "halt_new_entries",
    }
    guarded = set(params_store.BOUNDS) | {"min_grade"}
    changed, rejected = {}, {}
    for k, v in body.items():
        if v == "•••set•••" and k in _SECRET_KEYS:
            continue                     # redacted echo — never overwrite a secret
        if k == "trading_mode" and str(v).lower() == "live":
            # going live is ONLY possible via the two-step, promotion-gated
            # switch (live_switch.py on the control dashboard). paper/off
            # stay one-click here — de-escalation must always be easy.
            rejected[k] = ("live requires the two-step switch on the control "
                           "dashboard (promotion gate + confirm phrase)")
            storage.log_agent("info", "trading_mode",
                              "REFUSED live via /api/settings — use the "
                              "two-step switch")
            continue
        if k in guarded:
            try:
                changed[k] = params_store.set_param(
                    k, v, origin="human", reason="dashboard settings")
            except params_store.ParamRejected as e:
                rejected[k] = str(e)
        elif k in allowed_direct:
            storage.set_setting(k, v)
            changed[k] = v
    if "trading_mode" in changed:
        storage.log_agent("info", "trading_mode",
                          "switched to %s via dashboard" % changed["trading_mode"])
        notifier.send("🔁 Trading mode switched to <b>%s</b>"
                      % str(changed["trading_mode"]).upper())
    return jsonify({"ok": not rejected, "changed": changed, "rejected": rejected})


@app.route("/api/param_changes")
def param_changes():
    unacked = request.args.get("unacked") == "1"
    return jsonify({"changes": params_store.recent_changes(
        int(request.args.get("limit", 100)), unacked_only=unacked)})


@app.route("/api/param_changes/ack/<int:change_id>", methods=["POST"])
def param_changes_ack(change_id):
    return jsonify({"ok": params_store.ack_change(change_id)})


@app.route("/api/exposure")
def api_exposure():
    p = engine.params()
    mode = p["trading_mode"]
    open_trades = storage.open_trades("paper" if mode != "live" else "live")
    return jsonify({
        "buckets": exposure.bucket_exposures(open_trades, p["risk_pct"]),
        "by_class": exposure.class_concurrency(open_trades),
        "cap": p["max_bucket_exposure"],
        "positions": [{"symbol": t["symbol"], "side": t["side"],
                       "risk_pct": t["risk_pct"],
                       "asset_class": instruments.asset_class(t["symbol"]),
                       "factors": instruments.factor_exposures(t["symbol"], t["side"])}
                      for t in open_trades],
    })


@app.route("/api/decisions")
def api_decisions():
    return jsonify({
        "funnel": decisions.funnel(int(request.args.get("hours", 24))),
        "recent": decisions.recent(
            int(request.args.get("limit", 100)),
            strategy=request.args.get("strategy"),
            symbol=request.args.get("symbol"),
            stage=request.args.get("stage"),
            action=request.args.get("action")),
    })


@app.route("/api/instruments")
def api_instruments():
    return jsonify({"instruments": instruments.all_rows()})


@app.route("/api/news_calendar")
def api_news_calendar():
    return jsonify({"status": news_calendar.status(),
                    "upcoming": news_calendar.upcoming(
                        int(request.args.get("hours", 48)))})


@app.route("/api/news_calendar/manual", methods=["POST"])
def api_news_calendar_manual():
    body = request.get_json(force=True, silent=True) or {}
    t, title = body.get("t"), body.get("title")
    entities = body.get("entities") or []
    if not (t and title and entities):
        return jsonify({"ok": False, "error": "t, title, entities required"}), 400
    row = news_calendar.add_manual(int(t), str(title)[:200], list(entities),
                                   str(body.get("impact", "high")))
    storage.log_agent("info", "news_calendar",
                      "manual event added: %s (%s)" % (title, entities))
    return jsonify({"ok": bool(row), "id": row})


@app.route("/api/halt", methods=["POST"])
def api_halt():
    params_store.set_param("halt_new_entries", True, origin="human",
                           reason="manual halt (dashboard)")
    notifier.send("⛔ <b>Manual halt</b> — new entries stopped (open trades still managed)")
    return jsonify({"ok": True})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    params_store.set_param("halt_new_entries", False, origin="human",
                           reason="manual resume (dashboard)")
    notifier.send("▶️ Manual halt lifted — new entries allowed again")
    return jsonify({"ok": True})


@app.route("/api/tv_webhook", methods=["POST"])
def tv_webhook_receive():
    """TradingView (or any external) alert receiver.

    OFF unless `tradingview_webhook_enabled` AND a `tradingview_webhook_token`
    are set. A valid, authenticated alert is a CANDIDATE routed through
    `engine.ingest_external_signal` -> the SAME rails as an SLC signal. It is
    never a raw market order and never flips `trading_mode` (invariant #6)."""
    if not storage.get_setting("tradingview_webhook_enabled", False):
        return jsonify({"ok": False, "error": "webhook disabled"}), 403
    expected = storage.get_setting("tradingview_webhook_token", "")
    parsed = tv_webhook.parse_payload(request.get_data())
    if not parsed["ok"]:
        return jsonify({"ok": False, "error": parsed["error"]}), 400
    fields = parsed["fields"]
    if not tv_webhook.check_token(fields.get("token"), expected):
        return jsonify({"ok": False, "error": "bad token"}), 401
    overrides = storage.get_setting("tradingview_symbol_map", {}) or {}
    symbol = tv_webhook.map_symbol(fields.get("ticker"), overrides)
    if not symbol:
        return jsonify({"ok": False,
                        "error": "unknown ticker %r" % fields.get("ticker")}), 422
    result = engine.ingest_external_signal(symbol, fields, engine.params())
    return jsonify({"ok": True, "result": result}), 200


@app.route("/api/trade/close/<int:trade_id>", methods=["POST"])
def close_trade(trade_id):
    return jsonify({"ok": engine.manual_close(trade_id)})


@app.route("/api/telegram_test", methods=["POST"])
def telegram_test():
    return jsonify(notifier.send_test())


@app.route("/api/agent/run", methods=["POST"])
def agent_run():
    threading.Thread(target=agent.run_once, args=(CFG["agent"], notifier.send),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/spread")
def api_spread():
    import json as _json
    feed = engine.get_feed()
    prices = feed.get("prices", {})
    try:
        with open(os.path.join(BASE, "state", "open_spread.json")) as f:
            osp = _json.load(f)
    except Exception:
        osp = {}

    opens = []
    for tr in storage.open_trades():
        if tr["mode"] not in ("paper", "shadow"):
            continue
        pr = prices.get(tr["symbol"], {})
        rec = osp.get(str(tr["id"]), {})
        opens.append({
            "id": tr["id"], "symbol": tr["symbol"], "side": tr["side"],
            "mode": tr["mode"], "entry": tr["entry"], "sl": tr["sl"],
            "cur_spread_pts": rec.get("cur_spread_pts", pr.get("spread")),
            "max_spread_pts": rec.get("max_spread_pts"),
        })

    total = induced = 0
    by_sym, recent = {}, []
    try:
        with open(os.path.join(BASE, "state", "spread_trace.jsonl")) as f:
            lines = f.readlines()
        for ln in lines:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            total += 1
            if r.get("spread_induced"):
                induced += 1
                by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0) + 1
        recent = [_json.loads(x) for x in lines[-8:] if x.strip()]
    except Exception:
        pass

    return jsonify({"open": opens,
                    "stops": {"total": total, "spread_induced": induced,
                              "by_symbol": by_sym, "recent": recent}})


@app.route("/spread")
def spread_page():
    from flask import Response
    html = """<!doctype html><html><head><meta charset='utf-8'>
<title>SLC - spread monitor</title><meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
 body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:24px;background:#0f1115;color:#e7e9ee}
 h1{font-size:18px;font-weight:600;margin:0 0 16px}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
 .card{background:#171a21;border:1px solid #232733;border-radius:10px;padding:14px 18px;min-width:130px}
 .card .label{font-size:12px;color:#9aa3b2}.card .val{font-size:24px;font-weight:600;margin-top:4px}
 table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #232733}
 th{color:#9aa3b2;font-weight:500}
 .warn{color:#f0b34a}.bad{color:#e26a6a}.ok{color:#5fc08a}
 .pill{display:inline-block;padding:2px 8px;border-radius:6px;background:#232733;font-size:12px}
 .muted{color:#9aa3b2}
</style></head><body>
<h1>Spread monitor <span class='muted' id='ts'></span></h1>
<div class='cards'>
 <div class='card'><div class='label'>Total SL exits</div><div class='val' id='c-total'>-</div></div>
 <div class='card'><div class='label'>Spread-induced</div><div class='val bad' id='c-induced'>-</div></div>
 <div class='card'><div class='label'>Open trades</div><div class='val' id='c-open'>-</div></div>
 <div class='card'><div class='label'>Widest open spread</div><div class='val warn' id='c-wide'>-</div></div>
</div>
<h1 style='font-size:15px'>Open trades</h1>
<table id='opent'><thead><tr><th>#</th><th>Symbol</th><th>Side</th><th>Mode</th>
 <th>Cur spread</th><th>Max spread</th></tr></thead><tbody></tbody></table>
<h1 style='font-size:15px'>Recent stops</h1>
<table id='stopt'><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Max spread</th><th>Cause</th></tr></thead><tbody></tbody></table>
<script>
async function load(){
 const r=await fetch('/api/spread'); const d=await r.json();
 document.getElementById('ts').textContent='- '+new Date().toLocaleTimeString();
 document.getElementById('c-total').textContent=d.stops.total;
 document.getElementById('c-induced').textContent=d.stops.spread_induced;
 document.getElementById('c-open').textContent=d.open.length;
 const wide=Math.max(0,...d.open.map(o=>o.max_spread_pts||0));
 document.getElementById('c-wide').textContent=wide?wide.toFixed(1)+'p':'-';
 const ob=document.querySelector('#opent tbody'); ob.innerHTML='';
 d.open.forEach(o=>{const tr=document.createElement('tr');
  const ms=o.max_spread_pts||0; const cls=ms>=10?'bad':ms>=4?'warn':'ok';
  tr.innerHTML=`<td>${o.id}</td><td>${o.symbol}</td><td>${o.side}</td><td><span class='pill'>${o.mode}</span></td>`+
   `<td>${o.cur_spread_pts!=null?o.cur_spread_pts.toFixed(1)+'p':'-'}</td>`+
   `<td class='${cls}'>${ms?ms.toFixed(1)+'p':'-'}</td>`; ob.appendChild(tr);});
 if(!d.open.length) ob.innerHTML="<tr><td colspan='6' class='muted'>no open paper/shadow trades</td></tr>";
 const sb=document.querySelector('#stopt tbody'); sb.innerHTML='';
 d.stops.recent.slice().reverse().forEach(s=>{const tr=document.createElement('tr');
  const t=new Date((s.t||0)*1000).toLocaleString();
  tr.innerHTML=`<td>${t}</td><td>${s.symbol}</td><td>${s.side}</td>`+
   `<td>${s.max_spread_pts!=null?Number(s.max_spread_pts).toFixed(1)+'p':'-'}</td>`+
   `<td class='${s.spread_induced?"bad":"muted"}'>${s.spread_induced?'spread-induced':'price'}</td>`; sb.appendChild(tr);});
 if(!d.stops.recent.length) sb.innerHTML="<tr><td colspan='5' class='muted'>no stops recorded yet</td></tr>";
}
load(); setInterval(load,5000);
</script></body></html>"""
    return Response(html, mimetype="text/html")


# ======================================================================
# Safety wiring — S1 alerts, S2 reconciliation, S3 dead-man
# (docs/ARCHITECTURE-V3.md Increment 1, items 6-8)
#
# alerts.py and reconcile.py were finished, tested, and imported by nothing
# outside their own suites. A pager nobody wired is not a pager. This process
# wires them, because it is the process that runs the engine loop and the only
# one that calls notifier.start():
#
#   RAISE   any process, any time. alerts.raise_alert records and never sends,
#           never blocks and never throws — a caller already handling a kill
#           switch must not also have to handle an alerting failure.
#   RELAY   exactly one process delivers, decided by the DB lease in
#           alerts.own_relay(). Normally this one; dashboard_api takes over
#           about two minutes after this process stops renewing, which is what
#           makes the dead-man in S3 able to page about this process dying.
#
# Nothing here places, cancels or modifies an order, and nothing here resumes.
# The only lever is halt_new_entries = True — monotonically de-risking, read by
# engine.try_execute as a fail-safe. It is never written False from here:
# healing is a human act (/api/resume), because a reconciler that un-halts
# itself is a reconciler that argues with the evidence.
# ======================================================================
MT5_MAGIC = 770001              # the magic engine.py stamps on its own orders
SAFETY_TICK_S = 15              # cadence of the cheap, in-memory checks
RECONCILE_EVERY_S = 60          # S2 cadence — venue reads are network-bound
DEADMAN_MIN_STALE_S = 180       # same window analysis.health() calls "alive"
DEADMAN_POLL_MULT = 3           # ...or three engine cycles, whichever is longer

# The truthful origin for an automated de-risking write, in order of honesty.
# params_store.WHITELISTS has no entry for "system" today, so set_param would
# REFUSE it (and a refused halt is a halt that did not happen), which leaves
# "human" — the origin every dashboard control already uses — as the only key
# that works. This is a lie in the audit trail and it is deliberate rather than
# hidden: the reason string and the agent_log row both name the reconciler.
# The one-line fix lives in a file this change does not own — add
# `"system": {"halt_new_entries"}` to params_store.WHITELISTS — and the moment
# somebody makes it, this tuple starts recording the truth with no edit here.
_HALT_ORIGINS = ("system", "reconciler", "human")

_WATCHED_SETTINGS = ("trading_mode", "halt_new_entries")
_LAST_SEEN_KEY = "safety_last_seen"          # watched setting -> last value
MONITOR_HEARTBEAT_KEY = "safety_monitor_t"   # this monitor's own liveness
RECON_REPORT_KEY = "recon_last_report"       # what /api/reconcile renders

_safety: Dict[str, Any] = {
    "started_t": time.time(), "last_tick_t": 0.0, "last_reconcile_t": 0.0,
    "ticks": 0, "errors": 0, "last_error": "",
}


def _stamp(key: str) -> Optional[float]:
    """A timestamp out of settings, or None when it is missing/unreadable.

    Deliberately not `or 0` and never `or time.time()`. This value decides
    whether a process is alive, and an absent heartbeat is the exact thing
    being detected: a numeric default here makes "we have never heard from the
    engine" indistinguishable from "the engine checked in at the epoch" or,
    worse, "the engine checked in just now".
    """
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


def _deadman_limit() -> float:
    poll = int((CFG.get("engine") or {}).get("poll_seconds", 20) or 20)
    return float(max(DEADMAN_MIN_STALE_S, DEADMAN_POLL_MULT * poll))


def _live_open() -> List[Dict[str, Any]]:
    try:
        return storage.open_trades("live") or []
    except Exception:
        return []


def _halt_origin() -> str:
    for cand in _HALT_ORIGINS:
        if "halt_new_entries" in params_store.WHITELISTS.get(cand, set()):
            return cand
    return "human"


def apply_safety_halt(why: str) -> str:
    """Set the fail-safe. True only, through the write layer, once.

    Returns "" when nothing needed doing, else the origin the write was
    recorded under. A failure to apply the halt is itself a page: the caller
    believing it halted while entries keep flowing is the worst outcome
    available here.
    """
    try:
        if storage.get_setting("halt_new_entries", False):
            return ""                      # already standing aside
    except Exception:
        pass                               # cannot read it -> try the write
    origin = _halt_origin()
    try:
        params_store.set_param("halt_new_entries", True, origin=origin,
                               reason="automated safety halt: %s" % why[:300],
                               trigger_data={"by": "safety monitor (server.py)",
                                             "why": why[:300]})
        storage.log_agent("change", "halt_new_entries",
                          "safety monitor halted new entries: %s" % why[:300])
        return origin
    except Exception as e:
        alerts.raise_alert(
            "halt_write_failed",
            "COULD NOT HALT NEW ENTRIES (%s) — the reason was: %s"
            % (str(e)[:200], why[:200]),
            severity=alerts.P1, dedupe_key="halt_write")
        return ""


# ------------------------------------------------------------------ S1
def check_trading_mode(now: Optional[float] = None) -> Dict[str, Any]:
    """Live mode enabled, and the other watched state transitions.

    Watches the STATE, not the endpoint. live_switch.confirm_live writes
    trading_mode with storage.set_setting, and the notice it queues into
    notifier from the dashboard process is never delivered because that
    process has no notifier worker — the one message announcing real money at
    risk. Instrumenting that call site would fix that path and only that path;
    watching the value catches every path to live, including a hand-edited DB.

    The last seen value is persisted, so this reports the TRANSITION and does
    not re-page every cycle for a mode that is simply still live.
    """
    del now
    out: Dict[str, Any] = {"changes": {}}
    try:
        seen = storage.get_setting(_LAST_SEEN_KEY, None)
    except Exception:
        return out
    seen = seen if isinstance(seen, dict) else None
    current = {k: storage.get_setting(k, None) for k in _WATCHED_SETTINGS}
    if seen is None:
        # First observation on this database. Record the baseline — and if we
        # are booting straight into live, that still deserves the page.
        storage.set_setting(_LAST_SEEN_KEY, current)
        if str(current.get("trading_mode")) == "live":
            alerts.raise_alert(
                "trading_mode",
                "engine started with trading_mode = LIVE — real money is at "
                "risk. The EA's AllowTradeExecution input is the other half of "
                "the gate.", severity=alerts.P1, dedupe_key="live")
            out["changes"]["trading_mode"] = (None, "live")
        return out
    for key in _WATCHED_SETTINGS:
        was, is_ = seen.get(key), current.get(key)
        if was == is_:
            continue
        out["changes"][key] = (was, is_)
        if key == "trading_mode":
            live = str(is_) == "live"
            msg = ("LIVE TRADING ENABLED (%s -> LIVE). Real money is at risk; "
                   "the EA's AllowTradeExecution input is the other half of "
                   "the gate." % (was,) if live
                   else "trading_mode %s -> %s" % (was, is_))
            alerts.raise_alert(
                "trading_mode", msg,
                # P1, not the registry's P2. A mode change is a state change,
                # but THIS state change is the one the whole promotion gate
                # exists to guard, and P2 can be held through quiet hours.
                severity=alerts.P1 if live else alerts.P2,
                dedupe_key=str(is_))
        else:
            alerts.raise_alert(
                "halt", "halt_new_entries %s -> %s" % (was, is_),
                dedupe_key=str(is_))
    if out["changes"]:            # only on an edge — no settings write per tick
        storage.set_setting(_LAST_SEEN_KEY, current)
    return out


def check_kill_switches(p: Dict[str, Any],
                        now: Optional[float] = None) -> str:
    """Daily/weekly stops. Hard stops in paper AND live (invariant 9)."""
    del now
    mode = p.get("trading_mode", "paper")
    if mode == "off":
        return "off"
    balance = engine._balance(mode, p)
    if balance <= 0:
        # engine.loss_limits_hit returns None when balance <= 0, so "no limit
        # hit" and "I could not read the account" are the same value there.
        # The engine is safe either way (it cannot size a trade off a zero
        # balance), but reporting it as "kill switches clear" is exactly the
        # reassuring default this codebase keeps getting bitten by.
        open_live = _live_open()
        alerts.raise_alert(
            "kill_switch_unknown",
            "%s balance reads %.2f — the daily/weekly stops cannot be "
            "evaluated at all%s" % (mode, balance,
                                    " while %d live position(s) are open"
                                    % len(open_live) if open_live else ""),
            severity=alerts.P1 if (mode == "live" and open_live) else alerts.P2,
            dedupe_key=mode)
        return "unknown"
    reason = engine.loss_limits_hit(mode, balance, p)
    if not reason:
        return "clear"
    # loss_limits_hit answers one question with two meanings: a stop was
    # breached, or the drawdown could not be computed because a position has
    # no usable quote. Ask _open_pnl which one it is rather than parsing the
    # sentence — and if the two ever disagree, the fired branch is the louder
    # one, so a misclassification pages harder instead of softer.
    try:
        _, unvalued = engine._open_pnl(mode)
    except Exception:
        unvalued = 0
    if unvalued:
        alerts.raise_alert(
            "kill_switch_unknown", "%s: %s" % (mode, reason),
            # A live position we cannot value is money whose drawdown is
            # unknown. In paper it is a data gap, loud enough at P2.
            severity=alerts.P1 if mode == "live" else alerts.P2,
            dedupe_key="%s|unvalued" % mode,
            detail={"mode": mode, "balance": balance, "unvalued": unvalued})
        return "unknown"
    alerts.raise_alert(
        "kill_switch_fired", "%s: %s — new entries are standing aside"
        % (mode, reason), severity=alerts.P1,
        dedupe_key="%s|%s" % (mode, reason[:40]),
        detail={"mode": mode, "balance": round(balance, 2)})
    return "fired"


def check_db_integrity(p: Dict[str, Any]) -> str:
    """storage latched suspect: every number the kill switches read comes out
    of that file, so this is P1 in any mode, not only live."""
    try:
        suspect = storage.integrity_suspect()
    except Exception as e:
        suspect = "integrity_suspect() itself failed: %s" % (str(e)[:120])
    if not suspect:
        return ""
    alerts.raise_alert(
        "db_integrity_live",
        "DB integrity SUSPECT (%s) — mode=%s. Balances, open trades and the "
        "kill-switch arithmetic all read this file."
        % (suspect, p.get("trading_mode")),
        severity=alerts.P1, dedupe_key=str(p.get("trading_mode")))
    return suspect


def check_mt5_reachable(now: Optional[float] = None) -> str:
    """The venue that holds the positions going dark.

    MT5 reaches this process through the EA drop copy rather than venues.py,
    so its reachability is the freshness of that feed. engine.source_state
    already fails closed on "never reported"; this consumes that verdict.
    """
    now = time.time() if now is None else now
    if not engine.mt5_configured():
        return "not configured"      # a crypto-only node has no EA, ever
    st = engine.source_state(engine.MT5_SOURCE, now)
    if st["fresh"] and st["reachable"]:
        return "ok"
    if st["age"] is None and (now - _safety["started_t"]) <= _deadman_limit():
        return "warming up"          # process just started; the EA pushes ~5s
    open_live = _live_open()
    why = st["reason"] or "mt5 feed unusable"
    if open_live:
        alerts.raise_alert(
            "feed_loss_live",
            "MT5 is unreachable (%s) while %d live position(s) are open — the "
            "venue holding real money is not answering."
            % (why, len(open_live)),
            severity=alerts.P1, dedupe_key=engine.MT5_SOURCE)
        return "unreachable_holding_positions"
    alerts.raise_alert("feed_stale", "MT5 unreachable (%s); no live positions "
                       "open" % why, dedupe_key=engine.MT5_SOURCE)
    return "unreachable"


# ------------------------------------------------------------------ S3
def check_engine_heartbeat(now: Optional[float] = None) -> str:
    """Dead-man switch: make the ABSENCE of a heartbeat produce an alert.

    engine.py writes engine_heartbeat_t every cycle and the only reader is
    analysis.health(), which renders to a dashboard. Pull-only telemetry tells
    you the engine is dead if and only if somebody was already looking.

    WHAT THIS CATCHES: the engine LOOP dying or wedging while this process
    lives — an unhandled exception out of engine_loop, a thread blocked on a
    venue socket, a deadlock on storage._lock. That is the common failure and
    nothing observed it before.

    WHAT THIS CANNOT CATCH, stated plainly because a dead-man you believe in
    wrongly is worse than none: this watchdog is a thread inside the same
    process as its subject. Kill the process, lose the box, blue-screen the
    host, and the watchdog dies with the engine and no alert is ever raised.
    Two things narrow that gap and neither closes it. First, dashboard_api.py
    runs the same check from a SEPARATE process and inherits the alerts relay
    lease about two minutes after this process stops renewing it, so a dead
    server.py does page — as long as the dashboard process is up. Second, only
    an OUTBOUND ping to something off this machine (an inverted watchdog such
    as healthchecks.io, pinged at the END of a successful engine cycle) can
    survive the whole box going away, and that ping has to be written inside
    engine_loop, which this change does not own. Until it exists, a total host
    failure is silent. That is a real hole, and it is on the record here.
    """
    now = time.time() if now is None else now
    limit = _deadman_limit()
    hb = _stamp("engine_heartbeat_t")
    if hb is None:
        if (now - _safety["started_t"]) <= limit:
            return "warming up"
        alerts.raise_alert(
            "engine_deadman",
            "no engine heartbeat has EVER been written (watched for %ds). The "
            "engine loop is not running: nothing is managing stops, and every "
            "dashboard number is a fossil." % int(now - _safety["started_t"]),
            severity=alerts.P1, dedupe_key="never")
        return "never"
    age = now - hb
    if age <= limit:
        return "alive"
    alerts.raise_alert(
        "engine_deadman",
        "engine heartbeat is %ds old (limit %ds) — the engine loop has "
        "stopped cycling. Open positions are unmanaged until it returns."
        % (int(age), int(limit)),
        severity=alerts.P1, dedupe_key="stalled",
        detail={"heartbeat_t": hb, "age_s": int(age), "limit_s": int(limit)})
    return "stalled"


# ------------------------------------------------------------------ S2
def _mt5_is_a_position_source(p: Dict[str, Any]) -> bool:
    """Is MT5 somewhere we might HOLD something, or only a price feed?

    This decides whether a silent EA is blindness (which halts) or merely a
    stale quote (which does not). It has to be answered honestly in both
    directions. Answer yes too eagerly and a paper-only node latches a halt
    every time the EA blinks, and a rail that fires forever is a rail the
    operator switches off. Answer no too eagerly and an orphan sits behind the
    silence with nobody looking.

    So: yes if we are live, yes if a live row is open, and yes if this database
    has EVER recorded a live trade — an account that has traded live can still
    be holding something we lost track of, whatever mode we are in today.
    """
    if str(p.get("trading_mode")) == "live":
        return True
    if _live_open():
        return True
    try:
        return storage.query_one(
            "SELECT id FROM trades WHERE mode='live' LIMIT 1") is not None
    except Exception:
        return True                     # cannot tell -> assume we might hold


def _mt5_positions(p: Dict[str, Any], now: float):
    """(rows, declared names) for the MT5 drop copy.

    reconcile._sources reads a {"venue", "error"} row as a source that went
    dark, which is exactly what a stale drop copy is. Handing it the stale
    POSITIONS instead would let a dead feed's last frame masquerade as the
    venue's current book.
    """
    if not _mt5_is_a_position_source(p):
        return [], []
    st = engine.source_state(engine.MT5_SOURCE, now)
    if st["fresh"] and st["reachable"]:
        feed = engine.get_feed()
        return (reconcile.from_mt5_feed(feed.get("open_positions"),
                                        magic=MT5_MAGIC,
                                        venue=engine.MT5_SOURCE),
                [engine.MT5_SOURCE])
    return ([{"venue": engine.MT5_SOURCE,
              "error": st["reason"] or "EA drop copy stale or absent"}],
            [engine.MT5_SOURCE])


def _scrub(text: Any) -> str:
    """Mask any stored venue credential that an adapter error echoed back.

    A venue error is the one string in this module that this process did not
    write itself: venues.positions_all() stores `str(e)` from the adapter, and
    an exchange that replies with the request it rejected can hand back the key
    that signed it. From here that string travels into an alert message, i.e.
    into Telegram — and constraint 3 puts a credential nowhere near a
    notification or a persisted reason string.

    Masking the VALUES rather than classifying the whole string (which is what
    engine._safe_detail does, against a strict allow-list) keeps "connection
    reset by peer" readable. Being unable to enumerate the secrets means being
    unable to prove the text is clean, so that path prints nothing.
    """
    s = "" if text is None else str(text)[:400]
    if not s:
        return s
    try:
        fields = getattr(venues, "_SECRET_FIELDS",
                         ("api_key", "api_secret", "password"))
        for v in venues.list_venues():
            cfg = venues.get(str(v.get("name"))) or {}
            for f in fields:
                secret = cfg.get(f)
                if isinstance(secret, str) and len(secret) >= 8 and secret in s:
                    s = s.replace(secret, "•••redacted•••")
    except Exception:
        return "venue error (withheld: could not verify it carries no secret)"
    return s


def _trim_report(rep: Dict[str, Any], keep: int = 20) -> Dict[str, Any]:
    """A storable, scrubbed summary. Material findings are kept FIRST so the
    trimmed report answers should_halt_entries exactly as the full one did."""
    disc = [d for d in rep.get("discrepancies", []) if isinstance(d, dict)]
    material = [d for d in disc if d.get("material")]
    rest = [d for d in disc if not d.get("material")]
    out = {k: rep.get(k) for k in
           ("t", "mode", "venue_backed", "blind", "local_open",
            "venue_positions", "counts", "material", "halt")}
    out["sources"] = [dict(s, error=_scrub(s.get("error")))
                      for s in (rep.get("sources") or []) if isinstance(s, dict)]
    out["discrepancies"] = [dict(d, detail=_scrub(d.get("detail")))
                            for d in material[:keep] + rest[:keep]]
    out["reason"] = _scrub(rep.get("reason"))
    out["truncated"] = len(disc) > len(out["discrepancies"])
    out["clean_n"] = len(rep.get("clean") or [])
    out["stops_unverified"] = (rep.get("stops_unverified") or [])[:keep]
    return out


def reconcile_tick(now: Optional[float] = None,
                   force: bool = False) -> Dict[str, Any]:
    """S2: compare the book against the venues, halt on material drift, page.

    Never heals. Closing a LOCAL_ONLY row or re-sending a stop would make this
    a second execution path beside engine.py. The report is stored for
    /api/reconcile, the classification is annotated onto the trade rows (one
    column no rail reads), and the only lever pulled is halt_new_entries.

    The comparison always runs against mode="live" whichever mode the engine is
    in: only live trades exist at a venue, and reconciling the CONFIGURED mode
    would stop checking live rows the moment somebody flipped back to paper —
    which is precisely when an orphaned live position is most dangerous.
    """
    now = time.time() if now is None else now
    if not force and (now - _safety["last_reconcile_t"]) < RECONCILE_EVERY_S:
        return {"ran": False, "reason": "not due"}
    _safety["last_reconcile_t"] = now
    try:
        p = engine.params()
    except Exception:
        p = {"trading_mode": storage.get_setting("trading_mode", "paper")}

    rows, names = _mt5_positions(p, now)
    halt, raw_reason, report = reconcile.gate("live", positions=rows,
                                              venue_names=names, now=int(now))
    # Everything that leaves this function — the stored report, the page, the
    # reason recorded against the halt — is derived from the scrubbed copy, so
    # venue text is cleaned in one place with no way to route around it. The
    # VERDICT still comes from the gate's own full report.
    safe = _trim_report(report)
    reason = reconcile.halt_reason(safe) or _scrub(raw_reason)

    try:
        storage.set_setting(RECON_REPORT_KEY, safe)
    except Exception as e:
        print("[safety] could not store recon report:", e)
    try:
        reconcile.apply_recon_state(report)      # annotation only, never status
    except Exception as e:                        # it swallows its own errors
        print("[safety] recon annotate:", e)

    # A venue that cannot be reached while we hold something is S1's
    # "venue holding an open position went dark". trades carry no venue column,
    # so WHICH venue holds a given row is not knowable here — with any live
    # position open, every blind source is treated as possibly the one.
    holding = len(_live_open())
    for name in safe.get("blind") or []:
        src = next((s for s in safe.get("sources") or []
                    if s.get("venue") == name), {})
        alerts.raise_alert(
            "venue_unreachable",
            "%s is unreachable (%s)%s" % (name, src.get("error") or "no detail",
                                          " while %d live position(s) are open"
                                          % holding if holding else ""),
            severity=alerts.P1 if holding else alerts.P2, dedupe_key=str(name))

    out = {"ran": True, "halt": halt, "reason": reason,
           "material": report.get("material", 0),
           "counts": report.get("counts", {}), "blind": report.get("blind", []),
           "halted_origin": ""}
    if not halt:
        return out
    alerts.raise_alert(
        "recon_drift",
        "reconciliation found %d material discrepancy(ies): %s — new entries "
        "halted, nothing was healed." % (report.get("material", 0), reason),
        severity=alerts.P1, dedupe_key=reason[:60] or "drift",
        detail={"counts": report.get("counts"), "blind": report.get("blind")})
    out["halted_origin"] = apply_safety_halt("reconciliation: %s" % reason)
    return out


# --------------------------------------------------------------- the tick
def _guard(name: str, fn, *args, **kw):
    """Run one check. A check that throws must not take the others with it, and
    must not be silent about having thrown."""
    try:
        return fn(*args, **kw)
    except Exception as e:
        _safety["errors"] += 1
        _safety["last_error"] = "%s: %s" % (name, str(e)[:200])
        print("[safety] check %s failed: %s" % (name, e))
        alerts.raise_alert("safety_monitor_error",
                           "safety check %s raised %s" % (name, str(e)[:200]),
                           dedupe_key=name)
        return None


def _relay(send=None) -> Dict[str, Any]:
    if send is None:
        # The relay owner must have a worker running, or "delivered" means
        # "queued into a process that drains nothing" — the live_switch bug
        # wearing a different hat. notifier.start() is idempotent.
        notifier.start()
    return alerts.relay_once(send=send)


def safety_tick(now: Optional[float] = None, send=None) -> Dict[str, Any]:
    """One pass of the monitor. Record everything first, deliver last: a relay
    that is down must never cost us the recording."""
    now = time.time() if now is None else now
    out: Dict[str, Any] = {"t": now}
    try:
        p = engine.params()
    except Exception as e:
        # Do NOT substitute a plausible-looking parameter set here. The kill
        # switch would then be evaluated against invented stops and report
        # "clear" on numbers nobody chose. Skip the checks that need it and
        # say loudly that they did not run.
        p = None
        _safety["last_error"] = "params(): %s" % (str(e)[:200])
        alerts.raise_alert(
            "safety_monitor_error",
            "engine.params() failed (%s) — the kill-switch check did not run "
            "this cycle." % (str(e)[:200]),
            severity=alerts.P1, dedupe_key="params")
    out["mode"] = _guard("trading_mode", check_trading_mode, now)
    if p is not None:
        out["db"] = _guard("db_integrity", check_db_integrity, p)
        out["kill_switch"] = _guard("kill_switches", check_kill_switches, p, now)
    out["mt5"] = _guard("mt5_reachable", check_mt5_reachable, now)
    out["engine"] = _guard("engine_heartbeat", check_engine_heartbeat, now)
    out["reconcile"] = _guard("reconcile", reconcile_tick, now)
    out["relay"] = _guard("relay", _relay, send)
    out["digest"] = _guard("digest", alerts.digest_once, send)
    _safety["ticks"] += 1
    _safety["last_tick_t"] = now
    try:
        # This monitor's own liveness, so the dashboard process can tell
        # "engine stalled" from "the whole watchdog went away".
        storage.set_setting(MONITOR_HEARTBEAT_KEY, int(now))
    except Exception:
        pass
    return out


def safety_loop(interval: int = SAFETY_TICK_S) -> None:
    _safety["started_t"] = time.time()
    print("safety monitor: started (tick %ds, reconcile %ds)"
          % (interval, RECONCILE_EVERY_S))
    while True:
        try:
            safety_tick()
        except Exception as e:            # the monitor may never die quietly
            _safety["errors"] += 1
            _safety["last_error"] = str(e)[:200]
            print("[safety] tick error:", e)
        time.sleep(interval)


def start_safety_monitor(interval: int = SAFETY_TICK_S) -> None:
    notifier.start()                      # this process is the relay owner
    threading.Thread(target=safety_loop, args=(interval,), daemon=True).start()


def safety_state() -> Dict[str, Any]:
    return dict(_safety, deadman_limit_s=_deadman_limit(),
                relay_owner=alerts.relay_owner())


# ======================================================================
if __name__ == "__main__":
    storage.init()
    seed_settings()
    instruments.seed_defaults()      # per-symbol metadata registry (Phase 2)
    news_calendar.configure(CFG.get("news_calendar"))
    news_calendar.start_refresher()  # scheduled-event blackouts (Phase 3)
    notifier.start()
    engine.set_notifier(notifier.send)

    threading.Thread(target=engine.engine_loop,
                     args=(CFG["engine"]["poll_seconds"],), daemon=True).start()
    threading.Thread(target=agent.agent_loop,
                     args=(CFG["agent"], notifier.send), daemon=True).start()
    start_safety_monitor()           # S1/S2/S3 — and this process relays

    # SLC_HOST / SLC_PORT env vars override config.yaml — lets a second
    # instance run in parallel with an existing bot without editing tracked
    # files (e.g. SLC_PORT=8768 python3 server.py)
    host = os.environ.get("SLC_HOST", CFG["server"]["host"])
    port = int(os.environ.get("SLC_PORT", CFG["server"]["port"]))
    print("=" * 60)
    print(" Keel")
    print(" Dashboard : http://localhost:%d" % port)
    print(" EA target : http://<this-machine-ip>:%d" % port)
    print("=" * 60)
    app.run(host=host, port=port, threaded=True)
