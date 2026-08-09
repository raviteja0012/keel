"""SLC engine: consumes MT5 feed/bars, generates signals, executes them
on the paper broker or via live EA commands, and manages open trades
(TP1 -> break-even -> structure trail).

Runs as a daemon thread started by server.py. All mutable runtime
parameters come from storage.settings (dashboard- and agent-tunable).
"""
import json
import threading
import time
from typing import Any, Dict, List, Optional

import decisions
import exposure
import instruments
import sessions
import storage
import strategy
import strategies
from strategy import MODE_TFS
try:
    import tv_context as _tv
    _TV_AVAILABLE = True
except ImportError:
    _TV_AVAILABLE = False
try:
    import news_calendar as _news_cal          # Phase 3 blackout gate
    _NEWS_CAL_AVAILABLE = True
except ImportError:
    _NEWS_CAL_AVAILABLE = False

TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600,
              "2h": 7200, "4h": 14400, "1d": 86400}

# --------------------------------------------------------------------
# Shared live state pushed by the EA (guarded by _feed_lock)
# --------------------------------------------------------------------
_feed_lock = threading.Lock()
feed_state: Dict[str, Any] = {
    "account": {}, "open_positions": [], "closed_today": [],
    "prices": {}, "terminal": {}, "last_feed_t": 0, "last_bars_t": 0,
}

_notify = None          # set by server.py -> notifier.send
_recent_keys: Dict[str, float] = {}   # signal dedup: key -> expiry ts
_last_info: Dict[str, Dict] = {}      # symbol|mode -> last analysis info
_tv_ctx: Dict[str, Any] = {}          # TradingView context snapshot (cached)
_tv_ctx_t: float = 0                  # timestamp of last TV context load


def set_notifier(fn) -> None:
    global _notify
    _notify = fn


def notify(msg: str) -> None:
    if _notify:
        try:
            _notify(msg)
        except Exception as e:           # never let telegram kill the engine
            print("notify error:", e)


# ------------------------------------------------------------ feed in
_open_spread: Dict[int, Dict[str, Any]] = {}


def _accum_px_window(p: Dict[str, Any]) -> None:
    """Track the worst exit-side price and max spread seen on the 5s feed
    BETWEEN 20s management cycles, so a spread blowout during news / session
    rollover still counts against open stops even if it lands between polls."""
    sym, bid, ask = p.get("symbol"), p.get("bid"), p.get("ask")
    if sym is None or bid is None or ask is None:
        return
    # prefer the EA's tick-accurate extremes for THIS push; fall back to quote
    t_min_bid = p.get("min_bid", bid)
    t_max_ask = p.get("max_ask", ask)
    point     = p.get("point") or 0.0
    spr_pts   = p.get("max_spread", p.get("spread", 0))
    spr_px    = (spr_pts * point) if point else abs(ask - bid)
    win = feed_state.setdefault("px_window", {})
    w = win.get(sym)
    if w is None:
        win[sym] = {"min_bid": t_min_bid, "max_ask": t_max_ask,
                    "max_spread": spr_px, "max_spread_pts": spr_pts}
    else:
        w["min_bid"] = min(w["min_bid"], t_min_bid)
        w["max_ask"] = max(w["max_ask"], t_max_ask)
        if spr_px > w["max_spread"]:
            w["max_spread"], w["max_spread_pts"] = spr_px, spr_pts


def _log_spread_event(tr: Dict[str, Any], max_spr: float,
                      max_spr_pts: float, spread_induced: bool) -> None:
    """Append a record so the real spread story of each stop is visible."""
    try:
        import json as _json, os as _os
        _os.makedirs("state", exist_ok=True)
        with open("state/spread_trace.jsonl", "a") as f:
            f.write(_json.dumps({
                "t": int(time.time()), "id": tr["id"], "symbol": tr["symbol"],
                "side": tr["side"], "mode": tr["mode"], "sl": tr["sl"],
                "max_spread_px": round(max_spr, 6), "max_spread_pts": max_spr_pts,
                "spread_induced": bool(spread_induced)}) + "\n")
    except Exception as e:
        print("spread-trace log error:", e)


def ingest_feed(data: Dict[str, Any]) -> None:
    with _feed_lock:
        feed_state["account"] = data.get("account", {})
        feed_state["open_positions"] = data.get("open_positions", [])
        feed_state["closed_today"] = data.get("closed_today", [])
        feed_state["terminal"] = data.get("terminal", {})
        for p in data.get("prices", []):
            feed_state["prices"][p["symbol"]] = p
            _accum_px_window(p)          # capture intra-cycle spread + extremes
        feed_state["last_feed_t"] = time.time()


def ingest_bars(data: Dict[str, Any]) -> None:
    bars = data.get("bars", {})
    for symbol, tfs in bars.items():
        for tf, blist in tfs.items():
            if isinstance(blist, list) and blist:
                storage.store_bars(symbol, tf, blist)
    with _feed_lock:
        feed_state["last_bars_t"] = time.time()


def get_feed() -> Dict[str, Any]:
    with _feed_lock:
        return json.loads(json.dumps(feed_state))


def get_last_info() -> List[Dict]:
    return list(_last_info.values())


# ----------------------------------------------------------- params
def params() -> Dict[str, Any]:
    s = storage.all_settings()
    out = {
        "trading_mode": s.get("trading_mode", "paper"),
        "modes": s.get("modes", ["intraday", "swing"]),
        "risk_pct": float(s.get("risk_pct", 1.0)),
        "b_setup_risk_factor": float(s.get("b_setup_risk_factor", 0.5)),
        "min_rr": float(s.get("min_rr", 2.0)),
        "atr_buffer": float(s.get("atr_buffer", 0.35)),
        "vol_mult": float(s.get("vol_mult", 0.0)),
        "max_concurrent": int(s.get("max_concurrent", 2)),
        "max_concurrent_per_class": int(s.get("max_concurrent_per_class", 2)),
        "max_correlated": int(s.get("max_correlated", 3)),
        "max_bucket_exposure": float(s.get("max_bucket_exposure", 2.0)),
        "halt_new_entries": bool(s.get("halt_new_entries", False)),
        "daily_stop_pct": float(s.get("daily_stop_pct", 2.0)),
        "weekly_stop_pct": float(s.get("weekly_stop_pct", 5.0)),
        "min_grade": s.get("min_grade", "B"),
        "regime_max": float(s.get("regime_max", 2.5)),
        "regime_b_ban": float(s.get("regime_b_ban", 1.5)),
        "max_spread_frac": float(s.get("max_spread_frac", 0.10)),
        "enabled_pairs": s.get("enabled_pairs", []),
        "watch_pairs": s.get("watch_pairs", []),
        "agent_disabled_pairs": s.get("agent_disabled_pairs", []),
        "agent_disabled_modes": s.get("agent_disabled_modes", []),
        "paper_balance": float(s.get("paper_balance", 10000.0)),
    }
    # strategy enable flags (strategy_<name>_enabled) flow through to the plugin
    # registry; default-on, so SLC stays on exactly as before and a new strategy
    # is picked up here automatically once its setting is seeded.
    for k, v in s.items():
        if k.startswith("strategy_") and k.endswith("_enabled"):
            out[k] = bool(v)
    return out


# ------------------------------------------------------------ sizing
def calc_lots(symbol: str, entry: float, sl: float, risk_amount: float):
    """Broker-exact sizing from tick_value/tick_size pushed by the EA.
    Returns (lots, actual_risk) so the caller can record the TRUE risk
    after lot rounding (min-lot floors can otherwise overstate it)."""
    p = feed_state["prices"].get(symbol)
    if not p:
        return 0.0, 0.0
    tick_value = p.get("tick_value") or 0
    tick_size = p.get("tick_size") or 0
    if tick_value <= 0 or tick_size <= 0:
        return 0.0, 0.0
    loss_per_lot = abs(entry - sl) / tick_size * tick_value
    if loss_per_lot <= 0:
        return 0.0, 0.0
    lots = max(0.01, int(risk_amount / loss_per_lot * 100) / 100.0)
    return lots, round(lots * loss_per_lot, 2)


# ------------------------------------------------- unrealized P&L
def trade_upnl(tr: Dict) -> Optional[float]:
    """Unrealized P&L of an open trade. Live trades use the broker's own
    number (includes swap/commission); paper trades are marked to the
    exit side of the book (bid for longs, ask for shorts)."""
    if tr["mode"] == "live":
        pos = next((x for x in feed_state["open_positions"]
                    if x.get("ticket") == tr["ticket"]
                    or "SLC#%d" % tr["id"] in (x.get("comment") or "")), None)
        if pos is not None:
            return round((pos.get("unrealized_pnl") or 0) + (pos.get("swap") or 0), 2)
    p = feed_state["prices"].get(tr["symbol"])
    if not p:
        return None
    tick_value = p.get("tick_value") or 0
    tick_size = p.get("tick_size") or 0
    if tick_value <= 0 or tick_size <= 0:
        return None
    cur = p["bid"] if tr["side"] == "buy" else p["ask"]
    direction = 1 if tr["side"] == "buy" else -1
    pnl = (cur - tr["entry"]) * direction / tick_size * tick_value * tr["lots"]
    if tr["tp1_done"]:
        # half was banked at +1R: value it from price distance (exact),
        # not from the recorded risk_amount
        banked = abs(tr["entry"] - tr["initial_sl"]) / tick_size * tick_value * tr["lots"]
        pnl = pnl * 0.5 + banked * 0.5
    return round(pnl, 2)


def open_pnl_total() -> float:
    return round(sum(trade_upnl(t) or 0 for t in storage.open_trades()), 2)


# ----------------------------------------------------- loss limits
def _pnl_since(mode: str, since_ts: int) -> float:
    row = storage.query_one(
        "SELECT COALESCE(SUM(pnl),0) s FROM trades "
        "WHERE mode=? AND status='closed' AND exit_time>=?", (mode, since_ts))
    return row["s"] if row else 0.0


def _open_pnl(mode: str) -> float:
    return round(sum(trade_upnl(t) or 0 for t in storage.open_trades(mode)), 2)


def _broker_offset() -> float:
    term = feed_state.get("terminal", {})
    return (term.get("time_local") or 0) - (term.get("time_utc") or 0)


def loss_limits_hit(mode: str, balance: float, p: Dict) -> Optional[str]:
    """Daily/weekly kill switches. The loss 'day' follows the broker clock
    (statements cut there, not at UTC midnight), and OPEN drawdown counts
    against the stop — being -2% underwater on open positions halts new
    entries just as surely as -2% realized. Open profit never offsets
    realized losses (conservative by construction)."""
    if balance <= 0:
        return None
    day0, week0 = sessions.day_week_start("broker", broker_offset_s=_broker_offset())
    open_dd = min(0.0, _open_pnl(mode))
    if _pnl_since(mode, day0) + open_dd <= -balance * p["daily_stop_pct"] / 100:
        return "daily stop (-%.1f%%) hit" % p["daily_stop_pct"]
    if _pnl_since(mode, week0) + open_dd <= -balance * p["weekly_stop_pct"] / 100:
        return "weekly stop (-%.1f%%) hit" % p["weekly_stop_pct"]
    return None


def loss_governor(mode: str) -> float:
    """Playbook §10.3: after 3 consecutive losses, halve risk until 2
    consecutive wins. Derived from the trades table (restart-safe — no
    counters to lose)."""
    rows = storage.query(
        "SELECT pnl FROM trades WHERE mode=? AND status='closed' "
        "ORDER BY exit_time DESC LIMIT 50", (mode,))
    halved, consec_loss, consec_win = False, 0, 0
    for r in reversed(rows):                       # oldest -> newest
        if (r["pnl"] or 0) < 0:
            consec_loss += 1
            consec_win = 0
            if consec_loss >= 3:
                halved = True
        else:
            consec_win += 1
            consec_loss = 0
            if halved and consec_win >= 2:
                halved = False
    return 0.5 if halved else 1.0


def _balance(mode: str, p: Dict) -> float:
    if mode == "live":
        return float(feed_state["account"].get("balance", 0) or 0)
    closed = storage.query_one(
        "SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE mode='paper' AND status='closed'")
    return p["paper_balance"] + (closed["s"] if closed else 0)


# ------------------------------------------------------- signal exec
def _get_tv_ctx() -> Dict[str, Any]:
    """Return cached TV context; refresh at most once per hour."""
    global _tv_ctx, _tv_ctx_t
    if not _TV_AVAILABLE:
        return {}
    if time.time() - _tv_ctx_t > 3600:
        try:
            _tv_ctx = _tv.load()
            _tv_ctx_t = time.time()
        except Exception as e:
            print("tv_context load error:", e)
    return _tv_ctx


def _log_signal(sig: Dict, status: str, reason: str) -> int:
    return storage.execute(
        "INSERT INTO signals(t,symbol,trade_mode,side,grade,entry,sl,tp,rr,status,reason,setup,strategy) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), sig["symbol"], sig["trade_mode"], sig["side"], sig["grade"],
         sig["entry"], sig["sl"], sig["tp"], sig["rr"], status, reason,
         json.dumps(sig["setup"], default=str), sig.get("strategy", "slc")))


def try_execute(sig: Dict, p: Dict) -> None:
    mode = p["trading_mode"]
    symbol = sig["symbol"]
    strat = sig.get("strategy", "slc")
    a_class = instruments.asset_class(symbol)
    regime_ctx = {"atr_ratio": sig.get("regime")}

    # dedup: one shot per setup zone. Skipped signals only lock the zone
    # briefly (so e.g. "max concurrent" can retry once capacity frees);
    # executed ones lock it for 12 h.
    now = time.time()
    for k in list(_recent_keys):
        if _recent_keys[k] < now:
            del _recent_keys[k]
    if sig["key"] in _recent_keys:
        return

    def skip(reason: str, track: bool = False, stage: str = "rails",
             checks: Any = None) -> None:
        """track=True: the setup itself was valid but a risk/capacity filter
        blocked it -> also record it as a SHADOW trade so the outcome is
        studied (what did the filter cost or save us?)."""
        _recent_keys[sig["key"]] = time.time() + (12 * 3600 if track else 30 * 60)
        sig_id = _log_signal(sig, "skipped", reason)
        decisions.record(strat, symbol, sig["trade_mode"], stage, "skipped",
                         reason=reason, grade=sig["grade"], checks=checks,
                         regime=regime_ctx, signal_id=sig_id)
        if track and not any(t["symbol"] == symbol
                             for t in storage.open_trades("shadow")):
            storage.insert_trade({
                "mode": "shadow", "trade_mode": sig["trade_mode"], "symbol": symbol,
                "side": sig["side"], "status": "open", "grade": sig["grade"],
                "entry_time": int(time.time()), "entry": sig["entry"],
                "sl": sig["sl"], "initial_sl": sig["sl"],
                "tp1": sig["tp1"], "tp2": sig["tp"],
                "lots": 0.0, "risk_pct": 0.0, "risk_amount": 0.0,
                "setup": json.dumps({"skip_reason": reason, **sig["setup"]},
                                    default=str), "signal_id": 0,
                "strategy": strat, "asset_class": a_class,
            })
        if _notify and storage.get_setting("notify_signals", True):
            notify("⚠️ <b>Signal skipped</b> %s %s (%s)\n%s"
                   % (sig["side"].upper(), symbol, sig["trade_mode"], reason))

    # ---- fail-safe gates first (constraint 4: uncertainty halts entries) ----
    suspect = storage.integrity_suspect()
    if suspect:
        return skip("DB integrity suspect — failing safe: %s" % suspect,
                    stage="fail_safe")
    if p.get("halt_new_entries"):
        return skip("manual halt active (halt_new_entries)", stage="fail_safe")
    if mode == "off":
        return skip("trading mode is OFF", stage="mode")

    # ---- session calendar (never blocks crypto; see sessions.py) -----------
    open_ok, closed_reason = sessions.is_market_open(symbol)
    if not open_ok:
        return skip(closed_reason, stage="session")

    # ---- scheduled-news blackout (Phase 3; logs the exact event) -----------
    if _NEWS_CAL_AVAILABLE:
        blk = _news_cal.is_blackout(symbol)
        if blk:
            decisions.record(strat, symbol, sig["trade_mode"], "news", "skipped",
                             reason=blk["reason"], grade=sig["grade"],
                             regime=regime_ctx, news_ctx=blk)
            _news_cal.log_action(blk.get("event_id"), "block_entry", symbol,
                                 blk["reason"])
            _recent_keys[sig["key"]] = time.time() + 30 * 60
            _log_signal(sig, "skipped", blk["reason"])
            return

    all_open = storage.open_trades(mode)
    open_same = [t for t in all_open if t["trade_mode"] == sig["trade_mode"]]
    if len(open_same) >= p["max_concurrent"]:
        return skip("max concurrent (%d) reached" % p["max_concurrent"],
                    track=True, stage="concurrency")
    if any(t["symbol"] == symbol for t in all_open):
        return skip("already in a %s trade" % symbol, track=True, stage="concurrency")
    by_class = exposure.class_concurrency(all_open)
    if by_class.get(a_class, 0) >= p["max_concurrent_per_class"]:
        return skip("max concurrent per asset class (%s: %d) reached"
                    % (a_class, p["max_concurrent_per_class"]),
                    track=True, stage="concurrency")
    same_dir = [t for t in all_open if t["side"] == sig["side"]]
    if len(same_dir) >= p["max_correlated"]:
        return skip("max correlated same-direction exposure (%d) reached"
                    % p["max_correlated"], track=True, stage="exposure")

    balance = _balance(mode, p)
    if balance <= 0:
        return skip("no balance data yet (is the EA pushing?)", stage="fail_safe")
    lim = loss_limits_hit(mode, balance, p)
    if lim:
        return skip(lim, track=True, stage="loss_limit")

    price = feed_state["prices"].get(symbol)
    if not price:
        return skip("no live price")
    entry = price["ask"] if sig["side"] == "buy" else price["bid"]
    stop_dist = abs(entry - sig["sl"])
    spread = abs(price["ask"] - price["bid"])
    if stop_dist <= 0:
        return skip("price moved through SL before execution")
    if spread > p["max_spread_frac"] * stop_dist:
        return skip("spread %.5f > %.0f%% of stop distance" % (spread, p["max_spread_frac"] * 100))

    # price drifted from the analyzed signal -> setup geometry no longer valid
    if abs(entry - sig["entry"]) > 0.25 * stop_dist:
        return skip("price drifted %.5f from signal entry %.5f (>25%% of stop)"
                    % (entry, sig["entry"]))
    # re-check RR at the ACTUAL fill (drift + current spread)
    rr_actual = abs(sig["tp"] - entry) / stop_dist
    if rr_actual < p["min_rr"] * 0.9:
        return skip("RR at fill %.2f < required %.2f" % (rr_actual, p["min_rr"] * 0.9))

    risk_pct = p["risk_pct"] * (p["b_setup_risk_factor"] if sig["grade"] == "B" else 1.0)
    # consecutive-loss governor (playbook §10.3) and session risk factor
    # (e.g. crypto weekends) only ever REDUCE risk, never raise it
    gov = loss_governor(mode)
    sess_factor, sess_why = sessions.entry_risk_factor(symbol)
    if gov < 1.0:
        risk_pct *= gov
    if sess_factor < 1.0:
        risk_pct *= sess_factor

    # cross-asset factor-bucket exposure gate (lesson 1: EUR+DAX+Gold longs
    # are one stacked bet) — snapshot recorded on the decision either way
    exp_ok, exp_reason, exp_snapshot = exposure.check_candidate(
        symbol, sig["side"], risk_pct, all_open,
        p["max_bucket_exposure"], base_risk_pct=p["risk_pct"])
    if not exp_ok:
        return skip(exp_reason, track=True, stage="exposure", checks=exp_snapshot)

    risk_amount = balance * risk_pct / 100.0
    lots, actual_risk = calc_lots(symbol, entry, sig["sl"], risk_amount)
    if lots <= 0:
        return skip("could not size position (tick data missing)", stage="sizing")
    if actual_risk > risk_amount * 1.5:
        return skip("min lot would risk %.2f > 1.5x intended %.2f"
                    % (actual_risk, risk_amount), track=True, stage="sizing")
    risk_amount = actual_risk                       # record the TRUE risk
    risk_pct = round(risk_amount / balance * 100.0, 3)

    # TV context confluence (informational — does not block valid PA signals)
    tv_ctx = _get_tv_ctx()
    tv_score = _tv.confluence_score(tv_ctx, symbol, sig["side"].replace("buy","long").replace("sell","short")) \
        if _TV_AVAILABLE and tv_ctx else 0
    tv_regime = _tv.get_regime(tv_ctx, symbol) if _TV_AVAILABLE and tv_ctx else "unknown"
    tv_align  = _tv.direction_align(tv_ctx, symbol, sig["side"].replace("buy","long").replace("sell","short")) \
        if _TV_AVAILABLE and tv_ctx else None
    tv_rsi    = (tv_ctx.get(symbol) or {}).get("RSI")
    tv_adx    = (tv_ctx.get(symbol) or {}).get("ADX")

    _recent_keys[sig["key"]] = time.time() + 12 * 3600
    risk_notes = []
    if gov < 1.0:
        risk_notes.append("risk halved: 3+ consecutive losses (until 2 wins)")
    if sess_factor < 1.0:
        risk_notes.append(sess_why)
    enhanced_setup = dict(sig["setup"], tv_score=tv_score, tv_regime=tv_regime,
                          tv_align=tv_align, tv_rsi=round(tv_rsi, 1) if tv_rsi else None,
                          tv_adx=round(tv_adx, 1) if tv_adx else None)
    if risk_notes:
        enhanced_setup["risk_notes"] = risk_notes
    sig_id = _log_signal(sig, "executed", "")
    trade = {
        "mode": mode, "trade_mode": sig["trade_mode"], "symbol": symbol,
        "side": sig["side"], "status": "open", "grade": sig["grade"],
        "entry_time": int(time.time()), "entry": entry,
        "sl": sig["sl"], "initial_sl": sig["sl"], "tp1": sig["tp1"], "tp2": sig["tp"],
        "lots": lots, "risk_pct": risk_pct, "risk_amount": risk_amount,
        "setup": json.dumps(enhanced_setup, default=str), "signal_id": sig_id,
        "strategy": strat, "asset_class": a_class,
    }
    trade_id = storage.insert_trade(trade)
    decisions.record(strat, symbol, sig["trade_mode"], "execution", "executed",
                     reason="; ".join(risk_notes), grade=sig["grade"],
                     checks=exp_snapshot, features={"rr": sig["rr"], "lots": lots,
                                                    "risk_pct": risk_pct},
                     regime=regime_ctx, signal_id=sig_id, trade_id=trade_id)

    if mode == "live":
        storage.enqueue_command("open_trade", {
            "symbol": symbol, "side": sig["side"], "lots": lots,
            "sl": sig["sl"], "tp": sig["tp"], "magic": 770001,
            "comment": "SLC#%d" % trade_id,
            "reason": "SLC %s grade %s RR %.1f" % (sig["trade_mode"], sig["grade"], sig["rr"]),
        })

    # TV confluence line for notification
    tv_line = ""
    if tv_score is not None and tv_ctx:
        align_sym = "✅" if tv_align else "⚠️"
        tv_line = ("\nTV: %s score %d/4 | %s | RSI=%.0f ADX=%.0f"
                   % (align_sym, tv_score, tv_regime,
                      tv_rsi or 0, tv_adx or 0))

    notify(
        "🟢 <b>TRADE OPENED</b> (%s)\n"
        "<b>%s %s</b> — %s, grade %s\n"
        "Entry: <code>%.5f</code>\nSL: <code>%.5f</code>\n"
        "TP1: <code>%.5f</code>  TP2: <code>%.5f</code>\n"
        "Lots: %.2f | Risk: %.2f%% (%.2f) | RR: %.1f%s"
        % (mode.upper(), sig["side"].upper(), symbol, sig["trade_mode"], sig["grade"],
           entry, sig["sl"], sig["tp1"], sig["tp"], lots, risk_pct, risk_amount, sig["rr"],
           tv_line))


# ----------------------------------------- external signals (TradingView etc.)
def ingest_external_signal(symbol: str, fields: Dict[str, Any],
                           p: Dict[str, Any]) -> Dict[str, Any]:
    """Route a parsed external alert (e.g. a TradingView webhook) as a
    CANDIDATE through the SAME rails as an SLC signal — never a raw market
    order, never flips trading_mode. Builds an SLC-shaped sig (valid stop on the
    correct side, RR computed if absent) and hands it to `try_execute`, which
    applies the mode / stop / spread / drift / RR / concurrency / correlation /
    balance / loss-limit / sizing rails. Returns a status dict for the HTTP
    response. (Invariants checked: #6 candidates-not-orders, #3 stop side, #5
    risk cap via B sizing + the shared rails.)"""
    side = fields.get("side")
    if side not in ("buy", "sell"):
        return {"accepted": False, "reason": "missing/invalid side"}
    trade_mode = fields.get("trade_mode")
    if trade_mode not in MODE_TFS:
        trade_mode = "swing"

    price_info = feed_state["prices"].get(symbol)
    live = None
    if price_info:
        live = price_info["ask"] if side == "buy" else price_info["bid"]

    entry = fields.get("entry")
    if entry is None:
        entry = live
    if entry is None:
        return {"accepted": False,
                "reason": "no entry in alert and no live price for %s" % symbol}
    entry = float(entry)

    sl = fields.get("sl")
    if sl is None:
        return {"accepted": False, "reason": "external signal must carry a stop"}
    sl = float(sl)
    # stop MUST be on the correct side of entry (never invent or flip it)
    if side == "buy" and not sl < entry:
        return {"accepted": False,
                "reason": "long stop %.6f not below entry %.6f" % (sl, entry)}
    if side == "sell" and not sl > entry:
        return {"accepted": False,
                "reason": "short stop %.6f not above entry %.6f" % (sl, entry)}

    risk = abs(entry - sl)
    if risk <= 0:
        return {"accepted": False, "reason": "degenerate risk distance"}

    tp = fields.get("tp")
    if tp is None:
        tp = entry + p["min_rr"] * risk if side == "buy" else entry - p["min_rr"] * risk
    tp = float(tp)
    if side == "buy" and not tp > entry:
        return {"accepted": False, "reason": "long target not above entry"}
    if side == "sell" and not tp < entry:
        return {"accepted": False, "reason": "short target not below entry"}

    tp1 = fields.get("tp1")
    tp1 = float(tp1) if tp1 is not None else (entry + risk if side == "buy" else entry - risk)
    rr = abs(tp - entry) / risk

    direction = "long" if side == "buy" else "short"
    src = str(fields.get("strategy") or "tradingview")
    sig = {
        "symbol": symbol, "trade_mode": trade_mode, "side": side,
        "strategy": "ext:%s" % src,
        # external alerts size as a B setup (b_setup_risk_factor, conservative);
        # they are vetted by the provider plus the bot's hard rails, not the SLC
        # quality checklist.
        "grade": "B",
        "entry": entry, "sl": sl, "tp1": tp1, "tp": tp,
        "rr": round(rr, 2), "atr": 0.0, "regime": 1.0, "spread": 0.0,
        "setup": {"source": "external", "provider": src, "trend": None,
                  "poi": None, "sweep": None, "confirmation": None},
        "key": "%s|%s|%s|%s|%.6f" % (src, symbol, trade_mode, direction, sl),
    }
    try_execute(sig, p)
    return {"accepted": True, "symbol": symbol, "side": side,
            "trade_mode": trade_mode, "rr": round(rr, 2),
            "mode": p["trading_mode"]}


# --------------------------------------------------- shadow tracking
def try_execute_shadow(sig: Dict, p: Dict) -> None:
    """Record a signal on a watch-only pair as a SHADOW trade: same entry
    rules and management as paper, but no money, no concurrency slot, no
    notifications. Pure sample collection for strategy evaluation."""
    symbol = sig["symbol"]
    now = time.time()
    if _recent_keys.get(sig["key"], 0) > now:
        return
    price = feed_state["prices"].get(symbol)
    if not price:
        return
    entry = price["ask"] if sig["side"] == "buy" else price["bid"]
    stop_dist = abs(entry - sig["sl"])
    spread = abs(price["ask"] - price["bid"])
    if stop_dist <= 0 or spread > p["max_spread_frac"] * stop_dist:
        return
    if abs(entry - sig["entry"]) > 0.25 * stop_dist:
        return
    if abs(sig["tp"] - entry) / stop_dist < p["min_rr"] * 0.9:
        return
    _recent_keys[sig["key"]] = now + 12 * 3600
    sig_id = _log_signal(sig, "shadow", "watch-pair sample")
    trade_id = storage.insert_trade({
        "mode": "shadow", "trade_mode": sig["trade_mode"], "symbol": symbol,
        "side": sig["side"], "status": "open", "grade": sig["grade"],
        "entry_time": int(time.time()), "entry": entry,
        "sl": sig["sl"], "initial_sl": sig["sl"], "tp1": sig["tp1"], "tp2": sig["tp"],
        "lots": 0.0, "risk_pct": 0.0, "risk_amount": 0.0,
        "setup": json.dumps(sig["setup"], default=str), "signal_id": sig_id,
        "strategy": sig.get("strategy", "slc"),
        "asset_class": instruments.asset_class(symbol),
    })
    decisions.record(sig.get("strategy", "slc"), symbol, sig["trade_mode"],
                     "execution", "shadow", reason="watch-pair sample",
                     grade=sig["grade"], regime={"atr_ratio": sig.get("regime")},
                     signal_id=sig_id, trade_id=trade_id)


# --------------------------------------------------- open trade mgmt
def _close_paper(tr: Dict, price: float, reason: str) -> None:
    p = feed_state["prices"].get(tr["symbol"], {})
    tick_value = p.get("tick_value") or 0
    tick_size = p.get("tick_size") or 1
    direction = 1 if tr["side"] == "buy" else -1
    move = (price - tr["entry"]) * direction
    pnl = move / tick_size * tick_value * tr["lots"] if tick_size > 0 else 0
    # account for the half already banked at TP1 (valued from price distance)
    if tr["tp1_done"]:
        banked = abs(tr["entry"] - tr["initial_sl"]) / tick_size * tick_value * tr["lots"]
        pnl = pnl * 0.5 + banked * 0.5
    risk_dist = abs(tr["entry"] - tr["initial_sl"])
    r_mult = (move / risk_dist) if risk_dist > 0 else 0
    if tr["tp1_done"]:
        r_mult = 0.5 * 1.0 + 0.5 * r_mult
    storage.update_trade(tr["id"], {
        "status": "closed", "exit_time": int(time.time()), "exit_price": price,
        "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2), "exit_reason": reason,
    })
    if tr["mode"] == "shadow":
        return                                  # silent sample collection
    emoji = "✅" if pnl >= 0 else "🔴"
    notify("%s <b>TRADE CLOSED</b> (PAPER)\n<b>%s %s</b> — %s\n"
           "Exit: <code>%.5f</code> | P&L: <b>%.2f</b> (%.2fR)\nReason: %s"
           % (emoji, tr["side"].upper(), tr["symbol"], tr["trade_mode"],
              price, pnl, r_mult, reason))


def _mtf_trail_level(tr: Dict) -> Optional[float]:
    tfs = MODE_TFS[tr["trade_mode"]]
    mtf = storage.get_bars(tr["symbol"], tfs["mtf"], 80)
    if len(mtf) < 10:
        return None
    pv = strategy.pivots(mtf, k=2)
    if tr["side"] == "buy":
        lows = [x["price"] for x in pv if x["kind"] == "L"]
        return lows[-1] if lows else None
    highs = [x["price"] for x in pv if x["kind"] == "H"]
    return highs[-1] if highs else None


def manage_open_trades(p: Dict) -> None:
    open_trs = storage.open_trades()
    for tr in open_trs:
        price_info = feed_state["prices"].get(tr["symbol"])
        if not price_info:
            continue
        bid, ask = price_info["bid"], price_info["ask"]
        cur = bid if tr["side"] == "buy" else ask          # exit side
        direction = 1 if tr["side"] == "buy" else -1
        risk_dist = abs(tr["entry"] - tr["initial_sl"])
        if risk_dist <= 0:
            continue
        r_now = (cur - tr["entry"]) * direction / risk_dist

        # track MFE / MAE in R
        upd: Dict[str, Any] = {}
        if r_now > (tr["mfe"] or 0):
            upd["mfe"] = round(r_now, 2)
        if r_now < (tr["mae"] or 0):
            upd["mae"] = round(r_now, 2)

        if tr["mode"] in ("paper", "shadow"):
            # Dynamic-spread stop: evaluate against the WORST exit-side price
            # seen on the 5s feed since the last cycle, not just this instant.
            w = feed_state.get("px_window", {}).get(tr["symbol"], {})
            worst_bid = min(bid, w.get("min_bid", bid))
            worst_ask = max(ask, w.get("max_ask", ask))
            max_spr = max(abs(ask - bid), w.get("max_spread", 0.0))
            max_spr_pts = w.get("max_spread_pts", price_info.get("spread", 0))
            rec = _open_spread.setdefault(tr["id"],
                                          {"symbol": tr["symbol"], "max_spread_pts": 0})
            if (max_spr_pts or 0) > rec["max_spread_pts"]:
                rec["max_spread_pts"] = max_spr_pts
            rec["cur_spread_pts"] = price_info.get("spread")
            sl_px = worst_bid if tr["side"] == "buy" else worst_ask
            hit_sl = (sl_px <= tr["sl"]) if tr["side"] == "buy" else (sl_px >= tr["sl"])
            hit_tp2 = (cur >= tr["tp2"]) if tr["side"] == "buy" else (cur <= tr["tp2"])
            if hit_sl:
                # did the SPREAD (not the mid) tag the stop?
                mid_ext = (worst_bid + max_spr / 2) if tr["side"] == "buy" \
                    else (worst_ask - max_spr / 2)
                spread_induced = (mid_ext > tr["sl"]) if tr["side"] == "buy" \
                    else (mid_ext < tr["sl"])
                _log_spread_event(tr, max_spr, max_spr_pts, spread_induced)
                if upd:
                    storage.update_trade(tr["id"], upd)
                base = "stop loss" if not tr["tp1_done"] else "trailing stop"
                reason = base + (" [spread-induced, max %.1fp]" % max_spr_pts
                                 if spread_induced else "")
                _close_paper(tr, tr["sl"], reason)
                continue
            if hit_tp2:
                if upd:
                    storage.update_trade(tr["id"], upd)
                _close_paper(tr, tr["tp2"], "take profit (TP2)")
                continue
            if not tr["tp1_done"] and r_now >= 1.0:
                upd["tp1_done"] = 1
                upd["sl"] = tr["entry"]                     # break-even
                if tr["mode"] == "paper":
                    notify("🔵 <b>TP1 hit</b> %s %s — 50%% banked, SL → breakeven"
                           % (tr["side"].upper(), tr["symbol"]))
            if tr["tp1_done"]:
                lvl = _mtf_trail_level(tr)
                if lvl is not None:
                    better = (lvl > tr["sl"]) if tr["side"] == "buy" else (lvl < tr["sl"])
                    in_profit = (lvl > tr["entry"]) if tr["side"] == "buy" else (lvl < tr["entry"])
                    if better and in_profit:
                        upd["sl"] = lvl
            if upd:
                storage.update_trade(tr["id"], upd)

        else:  # live: EA holds the position; we mirror + issue SL commands
            pos = next((x for x in feed_state["open_positions"]
                        if "SLC#%d" % tr["id"] in (x.get("comment") or "")
                        or x.get("ticket") == tr["ticket"]), None)
            if pos and not tr["ticket"]:
                upd["ticket"] = pos["ticket"]
            if pos:
                if not tr["tp1_done"] and r_now >= 1.0:
                    upd["tp1_done"] = 1
                    upd["sl"] = tr["entry"]
                    if tr["lots"] >= 0.02:        # 0.01 can't be halved
                        storage.enqueue_command("close_trade", {
                            "ticket": pos["ticket"], "lots": round(tr["lots"] / 2, 2),
                            "reason": "TP1 partial (1R)"})
                    storage.enqueue_command("trail_sl", {
                        "ticket": pos["ticket"], "symbol": tr["symbol"],
                        "new_sl": tr["entry"], "reason": "move to breakeven after TP1"})
                    notify("🔵 <b>TP1 hit</b> %s %s — partial close + BE sent to MT5"
                           % (tr["side"].upper(), tr["symbol"]))
                elif tr["tp1_done"]:
                    lvl = _mtf_trail_level(tr)
                    if lvl is not None:
                        better = (lvl > tr["sl"]) if tr["side"] == "buy" else (lvl < tr["sl"])
                        in_profit = (lvl > tr["entry"]) if tr["side"] == "buy" else (lvl < tr["entry"])
                        if better and in_profit:
                            upd["sl"] = lvl
                            storage.enqueue_command("trail_sl", {
                                "ticket": pos["ticket"], "symbol": tr["symbol"],
                                "new_sl": lvl, "reason": "structure trail"})
                if upd:
                    storage.update_trade(tr["id"], upd)
            else:
                # position no longer open at broker -> find the closing deal
                deal = next((d for d in feed_state["closed_today"]
                             if d.get("position_id") == tr["ticket"] and tr["ticket"]), None)
                exit_price = deal["price"] if deal else cur
                pnl = (deal["profit"] + deal.get("commission", 0) + deal.get("swap", 0)) \
                    if deal else 0.0
                r_mult = (exit_price - tr["entry"]) * direction / risk_dist
                storage.update_trade(tr["id"], {
                    "status": "closed", "exit_time": int(time.time()),
                    "exit_price": exit_price, "pnl": round(pnl, 2),
                    "r_multiple": round(r_mult, 2),
                    "exit_reason": "closed at broker"})
                emoji = "✅" if pnl >= 0 else "🔴"
                notify("%s <b>TRADE CLOSED</b> (LIVE)\n<b>%s %s</b>\n"
                       "Exit: <code>%.5f</code> | P&L: <b>%.2f</b> (%.2fR)"
                       % (emoji, tr["side"].upper(), tr["symbol"], exit_price, pnl, r_mult))

    # reset the spread window so the next cycle measures a fresh interval
    # (it re-accumulates from the very next 5s feed push)
    feed_state["px_window"] = {}
    open_ids = {t["id"] for t in open_trs}
    for _tid in list(_open_spread):
        if _tid not in open_ids:
            _open_spread.pop(_tid, None)
    try:
        import json as _json, os as _os
        _os.makedirs("state", exist_ok=True)
        with open("state/open_spread.json", "w") as _f:
            _json.dump({str(k): v for k, v in _open_spread.items()}, _f)
    except Exception as _e:
        print("open_spread dump error:", _e)


def manual_close(trade_id: int) -> bool:
    tr = storage.query_one("SELECT * FROM trades WHERE id=? AND status='open'", (trade_id,))
    if not tr:
        return False
    price_info = feed_state["prices"].get(tr["symbol"])
    if tr["mode"] == "paper":
        if not price_info:
            return False
        cur = price_info["bid"] if tr["side"] == "buy" else price_info["ask"]
        _close_paper(tr, cur, "manual close (dashboard)")
    else:
        if tr["ticket"]:
            storage.enqueue_command("close_trade", {
                "ticket": tr["ticket"], "lots": 0, "reason": "manual close (dashboard)"})
        else:
            storage.update_trade(trade_id, {
                "status": "closed", "exit_time": int(time.time()),
                "exit_reason": "manual close (no ticket linked)"})
    return True


# -------------------------------------------------------------- loop
def engine_loop(poll_seconds: int = 20) -> None:
    print("engine: started (poll %ds)" % poll_seconds)
    while True:
        try:
            # heartbeat EVERY cycle — even while standing aside waiting for
            # the EA feed — so the dashboard can distinguish "engine process
            # down" from "engine up, no data" (both matter, differently)
            try:
                storage.set_setting("engine_heartbeat_t", int(time.time()))
                if feed_state.get("last_feed_t"):
                    storage.set_setting("ea_last_feed_t",
                                        int(feed_state["last_feed_t"]))
            except Exception:
                pass

            p = params()

            # No fresh EA feed -> no live prices -> analyzing would only
            # produce "no live price" skips and 30-min zone locks (classic
            # right after a server restart). Manage nothing, signal nothing,
            # just wait for the feed.
            feed_age = time.time() - (feed_state.get("last_feed_t") or 0)
            if feed_age > 60:
                _last_info.clear()
                _last_info["feed"] = {"symbol": "—", "trade_mode": "—",
                                      "note": "waiting for EA feed (last push %s)"
                                      % ("never" if feed_age > 1e9 else "%.0fs ago" % feed_age)}
                time.sleep(poll_seconds)
                continue

            manage_open_trades(p)

            mode = p["trading_mode"]
            balance = _balance(mode if mode != "off" else "paper", p)
            if balance > 0:
                if mode == "live":
                    eq = float(feed_state["account"].get("equity", balance) or balance)
                else:
                    eq = balance + open_pnl_total()   # mark open paper trades
                storage.record_equity(mode if mode != "off" else "paper", balance, eq)

            # broker clock offset (bar timestamps are broker time, not UTC)
            term = feed_state.get("terminal", {})
            tz_off = (term.get("time_local") or 0) - (term.get("time_utc") or 0)
            broker_now = time.time() + tz_off

            # Pre-warm TV context once per hour (non-blocking: uses cache if fresh)
            _get_tv_ctx()

            tradable = [s for s in p["enabled_pairs"] if s not in p["agent_disabled_pairs"]]
            shadow_only = [s for s in p["watch_pairs"] if s not in p["enabled_pairs"]]
            symbols = tradable + shadow_only
            modes = [m for m in p["modes"] if m not in p["agent_disabled_modes"]]
            for symbol in symbols:
                bars_by_tf = {tf: storage.get_bars(symbol, tf, 320)
                              for tf in ("15m", "30m", "1h", "2h", "4h", "1d")}
                price_info = feed_state["prices"].get(symbol)
                spread = abs((price_info["ask"] - price_info["bid"])) if price_info else 0.0
                live_mid = ((price_info["ask"] + price_info["bid"]) / 2) if price_info else None
                for m in modes:
                    # don't analyze stale data: the newest setup-TF bar must
                    # have closed within the staleness limit for the symbol's
                    # asset class (broker clock; crypto is stricter — a gap in
                    # a 24/7 market means the feed died, not a weekend)
                    mtf_tf = MODE_TFS[m]["mtf"]
                    tf_sec = TF_SECONDS[mtf_tf]
                    mtf_bars = bars_by_tf.get(mtf_tf, [])
                    stale_lim = sessions.staleness_limit_s(symbol, tf_sec)
                    if mtf_bars and broker_now - (mtf_bars[-1]["t"] + tf_sec) > stale_lim:
                        note = ("bar data stale (last %s bar %dm old) — standing aside"
                                % (mtf_tf, (broker_now - mtf_bars[-1]["t"]) // 60))
                        _last_info["%s|%s" % (symbol, m)] = {
                            "symbol": symbol, "trade_mode": m, "note": note}
                        decisions.record_state(None, symbol, m, "fail_safe", note)
                        continue
                    for sname, res in strategies.generate_all(
                            symbol, m, bars_by_tf, p,
                            spread=spread, live_price=live_mid):
                        res["info"]["watch"] = symbol in shadow_only
                        # one info row per symbol|mode|strategy (SLC-only keeps a
                        # single row per symbol|mode, now tagged with strategy)
                        _last_info["%s|%s|%s" % (symbol, m, sname)] = res["info"]
                        if not res["signal"]:
                            # audit the decision NOT to trade (deduped: only
                            # writes when the reason changes for this cell)
                            decisions.record_state(
                                sname, symbol, m, "analysis",
                                str(res["info"].get("note", "no setup")),
                                action="no_setup")
                        if res["signal"]:
                            res["signal"]["strategy"] = sname
                            if symbol in shadow_only:
                                # shadow trades that mirror an already-open shadow
                                # position on the same symbol are skipped
                                if not any(t["symbol"] == symbol
                                           for t in storage.open_trades("shadow")):
                                    try_execute_shadow(res["signal"], p)
                            else:
                                try_execute(res["signal"], p)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("engine error:", e)
        time.sleep(poll_seconds)
