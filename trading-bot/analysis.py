"""Importable analysis library — the study views as FUNCTIONS over the DB.

Phase 1 found that the studies the operator skills describe (regime/session
performance, exposure snapshot, drawdown forensics, promotion-gate status)
existed only as prose or ad-hoc chat output. This module makes each one a
pure function over trading.db so the dashboard (and any script, skill, or
test) imports the SAME computation instead of reimplementing it.

Every function returns plain JSON-serializable dicts. Nothing here writes.
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

import exposure
import instruments
import sessions
import storage

BASE = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------------ helpers
def _closed(mode: Optional[str] = None, days: Optional[int] = None,
            symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    where, args = ["status='closed'", "mode != 'shadow'"], []
    if mode:
        where.append("mode=?")
        args.append(mode)
    if days:
        where.append("exit_time >= ?")
        args.append(int(time.time()) - days * 86400)
    if symbol:
        where.append("symbol=?")
        args.append(symbol)
    return storage.query(
        "SELECT * FROM trades WHERE %s ORDER BY exit_time" % " AND ".join(where),
        tuple(args))


def _num(v: Any) -> Optional[float]:
    """Defensive numeric coercion for values read from historical DBs —
    SQLite columns are dynamically typed and real user databases have been
    seen holding string timestamps (a single string row makes MAX() return
    a string, since strings sort above numbers)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    rs = [_num(r["r_multiple"]) or 0 for r in rows]
    pnls = [_num(r["pnl"]) or 0 for r in rows]
    wins = [x for x in rs if x > 0]
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    return {"n": n,
            "win_rate": round(100.0 * len(wins) / n, 1),
            "expectancy_r": round(sum(rs) / n, 3),
            "total_r": round(sum(rs), 2),
            "pnl": round(sum(pnls), 2),
            "profit_factor": round(gains / losses, 2) if losses > 0 else None}


# ------------------------------------------------- regime/session performance
def regime_session_performance(mode: str = "paper",
                               days: Optional[int] = None) -> Dict[str, Any]:
    """Closed-trade performance bucketed by session label, asset class, and
    regime band. Session labels derive from entry time via the per-class
    calendar (so crypto buckets by UTC hour-block/weekend, FX by session).
    Regime uses the decision record when present (Phase 2+ trades), else the
    trade's stored setup JSON."""
    rows = _closed(mode, days)
    by_session: Dict[str, list] = {}
    by_class: Dict[str, list] = {}
    by_regime: Dict[str, list] = {}
    # decisions carry regime for post-Phase-2 trades
    dec_regime = {r["trade_id"]: r["regime"] for r in storage.query(
        "SELECT trade_id, regime FROM decisions "
        "WHERE action='executed' AND trade_id IS NOT NULL")}
    for tr in rows:
        label = sessions.session_label(tr["symbol"], tr["entry_time"])
        # collapse crypto hour-blocks into 4 coarse buckets for sample size
        if label.startswith("h") or label.startswith("weekend+h"):
            wk = label.startswith("weekend")
            h = int(label.split("h")[-1])
            label = ("weekend+" if wk else "") + ["00-06", "06-12", "12-18", "18-24"][h // 6]
        by_session.setdefault(label, []).append(tr)
        cls = tr.get("asset_class") or instruments.asset_class(tr["symbol"])
        by_class.setdefault(cls, []).append(tr)
        ratio = None
        raw = dec_regime.get(tr["id"])
        if raw:
            try:
                ratio = (json.loads(raw) or {}).get("atr_ratio")
            except (json.JSONDecodeError, TypeError):
                ratio = None
        if ratio is None:
            try:
                ratio = (json.loads(tr["setup"] or "{}")).get("regime")
            except (json.JSONDecodeError, TypeError):
                ratio = None
        if ratio is None:
            band = "unknown"
        elif ratio < 0.7:
            band = "compressed (<0.7)"
        elif ratio <= 1.5:
            band = "normal (0.7-1.5)"
        elif ratio <= 2.5:
            band = "expanded (1.5-2.5)"
        else:
            band = "shock (>2.5)"
        by_regime.setdefault(band, []).append(tr)
    return {"mode": mode, "days": days, "total": _stats(rows),
            "by_session": {k: _stats(v) for k, v in sorted(by_session.items())},
            "by_asset_class": {k: _stats(v) for k, v in sorted(by_class.items())},
            "by_regime": {k: _stats(v) for k, v in sorted(by_regime.items())}}


# ------------------------------------------------------- exposure snapshot
def exposure_snapshot(mode: str = "paper",
                      base_risk_pct: float = 1.0) -> Dict[str, Any]:
    """Current factor-bucket exposure + per-class concurrency (live view of
    the lesson-1 rail), plus each open position's contribution."""
    open_trades = storage.open_trades(mode)
    return {
        "mode": mode,
        "buckets": exposure.bucket_exposures(open_trades, base_risk_pct),
        "by_class": exposure.class_concurrency(open_trades),
        "positions": [
            {"id": t["id"], "symbol": t["symbol"], "side": t["side"],
             "risk_pct": t["risk_pct"], "grade": t["grade"],
             "asset_class": t.get("asset_class") or instruments.asset_class(t["symbol"]),
             "factors": instruments.factor_exposures(t["symbol"], t["side"])}
            for t in open_trades],
    }


# ------------------------------------------------------ drawdown forensics
def drawdown_forensics(mode: str = "paper",
                       days: Optional[int] = None) -> Dict[str, Any]:
    """Closed-trade drawdown anatomy: R-equity curve, max drawdown (depth +
    duration + the trades inside it), losing streaks, wick-out losers
    (reached >=+0.5R then lost — stop too tight), spread-induced stops, and
    loss concentration by symbol/grade/asset class."""
    rows = _closed(mode, days)
    if not rows:
        return {"mode": mode, "n": 0}
    curve, cum = [], 0.0
    for tr in rows:
        cum += _num(tr["r_multiple"]) or 0
        curve.append({"t": _num(tr["exit_time"]) or 0, "cum_r": round(cum, 2),
                      "id": tr["id"], "symbol": tr["symbol"]})
    peak, max_dd, dd_start, dd_trough, cur_peak_i = -1e18, 0.0, None, None, 0
    for i, pt in enumerate(curve):
        if pt["cum_r"] > peak:
            peak, cur_peak_i = pt["cum_r"], i
        dd = peak - pt["cum_r"]
        if dd > max_dd:
            max_dd, dd_start, dd_trough = dd, cur_peak_i, i
    worst_streak, streak = 0, 0
    for tr in rows:
        if (_num(tr["r_multiple"]) or 0) < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0
    losers = [t for t in rows if (_num(t["r_multiple"]) or 0) < 0]
    wickouts = [t for t in losers if (_num(t["mfe"]) or 0) >= 0.5]
    spread_stops = [t for t in rows
                    if "spread-induced" in (t["exit_reason"] or "")]
    def _conc(key_fn):
        out: Dict[str, list] = {}
        for t in losers:
            out.setdefault(key_fn(t), []).append(t)
        return {k: {"losses": len(v),
                    "lost_r": round(sum(x["r_multiple"] or 0 for x in v), 2)}
                for k, v in sorted(out.items(),
                                   key=lambda kv: sum(x["r_multiple"] or 0 for x in kv[1]))}
    return {
        "mode": mode, "n": len(rows), "equity_curve_r": curve[-500:],
        "max_drawdown_r": round(max_dd, 2),
        "max_dd_window": {
            "from_t": curve[dd_start]["t"] if dd_start is not None else None,
            "to_t": curve[dd_trough]["t"] if dd_trough is not None else None,
            "trades": [c["id"] for c in
                       (curve[dd_start:dd_trough + 1] if dd_start is not None else [])],
        },
        "worst_losing_streak": worst_streak,
        "wickout_losers": {"n": len(wickouts), "of_losers": len(losers),
                           "note": "losers that reached >=+0.5R first — stop-tightness signal"},
        "spread_induced_stops": len(spread_stops),
        "loss_concentration": {
            "by_symbol": _conc(lambda t: t["symbol"]),
            "by_grade": _conc(lambda t: t["grade"] or "?"),
            "by_class": _conc(lambda t: t.get("asset_class")
                              or instruments.asset_class(t["symbol"])),
        },
    }


# ------------------------------------------------------- promotion gate
GATE_MIN_TRADES = 50

# A sign-off is a human attestation, and an attestation has to be able to go
# stale. Without these two bounds the row is permanent: one call to /signoff
# satisfies the gate forever, and the same actor holding the dashboard token
# can sign off and request live in two consecutive calls.
SIGNOFF_MIN_AGE_S = 3600            # must sit for an hour — no sign-and-go
SIGNOFF_MAX_AGE_S = 30 * 86400      # and expires after 30 days


def _data_trust() -> Dict[str, Any]:
    """Latest hallucination_check verdict (the data-trust gate). Read-only;
    'unknown' if the check has never run."""
    path = os.path.join(os.path.dirname(BASE), "hallucination_check.jsonl")
    try:
        with open(path) as f:
            lines = f.readlines()
        rec = json.loads(lines[-1])
        return {"verdict": rec.get("verdict", "unknown"),
                "ts": rec.get("ts"), "age_h": round((time.time() - rec.get("ts", 0)) / 3600, 1)}
    except Exception:
        return {"verdict": "unknown", "ts": None, "age_h": None}


def _signoff_check(signed: Optional[Dict[str, Any]],
                   last_param_change: int) -> Dict[str, Any]:
    """The manual_signoff gate check. Fails CLOSED on every uncertainty:
    missing, too fresh (sign-and-go in one motion), expired, or predating the
    newest accepted parameter change (it attests to behaviour that changed)."""
    if not signed:
        return {"pass": False, "value": None, "reason": "no sign-off recorded"}
    age = int(time.time()) - int(signed["t"] or 0)
    who = signed.get("signed_by")
    if age < SIGNOFF_MIN_AGE_S:
        return {"pass": False, "value": who,
                "reason": "sign-off is %dm old; must settle for %dm before it "
                          "counts (no sign-and-go)"
                          % (age // 60, SIGNOFF_MIN_AGE_S // 60)}
    if age > SIGNOFF_MAX_AGE_S:
        return {"pass": False, "value": who,
                "reason": "sign-off expired (%dd old, max %dd) — re-attest"
                          % (age // 86400, SIGNOFF_MAX_AGE_S // 86400)}
    if last_param_change and int(signed["t"] or 0) < last_param_change:
        return {"pass": False, "value": who,
                "reason": "parameters changed after this sign-off — re-validate "
                          "then re-attest"}
    return {"pass": True, "value": who, "age_s": age}


def promotion_status() -> Dict[str, Any]:
    """Per (strategy × asset class) promotion-gate status: sample size,
    expectancy on closed PAPER trades, data-trust verdict, whether the risk
    rails have demonstrably fired, and the manual sign-off. NOTHING here
    flips live — this only reports; live_switch consumes it (Phase 5)."""
    rows = _closed("paper")
    cells: Dict[str, list] = {}
    for tr in rows:
        strat = tr.get("strategy") or "slc"
        cls = tr.get("asset_class") or instruments.asset_class(tr["symbol"])
        cells.setdefault("%s|%s" % (strat, cls), []).append(tr)
    trust = _data_trust()
    rails_seen = {r["stage"] for r in storage.query(
        "SELECT DISTINCT stage FROM decisions WHERE action='skipped'")}
    signoffs = storage.query(
        "SELECT * FROM promotion_signoffs ORDER BY t DESC")
    # a sign-off made BEFORE the newest accepted parameter change attests to
    # behaviour that no longer exists (CLAUDE.md: re-validate after changes)
    _lpc = storage.query_one(
        "SELECT MAX(t) m FROM param_changes WHERE accepted=1")
    last_param_change = (_lpc or {}).get("m") or 0
    out = {}
    for key, trs in sorted(cells.items()):
        strat, cls = key.split("|")
        st = _stats(trs)
        signed = next((s for s in signoffs
                       if s["strategy"] == strat and s["asset_class"] == cls), None)
        checks = {
            "sample_size": {"pass": st["n"] >= GATE_MIN_TRADES,
                            "value": st["n"], "required": GATE_MIN_TRADES},
            "positive_expectancy": {"pass": st.get("expectancy_r", 0) > 0,
                                    "value": st.get("expectancy_r")},
            "data_trust": {"pass": trust["verdict"] == "GROUNDED",
                           "value": trust["verdict"]},
            "rails_exercised": {
                "pass": bool({"loss_limit", "exposure", "concurrency"} & rails_seen),
                "value": sorted(rails_seen),
                "note": "risk rails must have demonstrably fired in paper"},
            "manual_signoff": _signoff_check(signed, last_param_change),
        }
        out[key] = {"strategy": strat, "asset_class": cls, "stats": st,
                    "checks": checks,
                    "gate_open": all(c["pass"] for c in checks.values())}
    return {"cells": out, "data_trust": trust,
            "note": "gate_open only permits REQUESTING live via the two-step "
                    "switch; it never flips anything by itself"}


# ------------------------------------------------------------ health
def health() -> Dict[str, Any]:
    """System health derived from the DB only, so it is correct from ANY
    process (the dashboard runs separately from the engine — in-memory feed
    state is not visible here):
      * engine liveness  <- newest equity row (written every engine cycle)
      * feed liveness    <- newest bar per asset class (EA pushes ~60s)
    """
    now = time.time()
    per_class_fresh: Dict[str, float] = {}
    for row in instruments.all_rows():
        if not row.get("enabled"):
            continue
        cls = row["asset_class"]
        newest = storage.query_one(
            "SELECT MAX(t) m FROM bars WHERE symbol=? "
            "AND typeof(t) IN ('integer','real')", (row["symbol"],))
        m = _num(newest["m"]) if newest else None
        age = (now - m) if m else None
        cur = per_class_fresh.get(cls)
        if age is not None and (cur is None or age < cur):
            per_class_fresh[cls] = age
    eq = storage.query_one("SELECT MAX(t) m FROM equity "
                           "WHERE typeof(t) IN ('integer','real')")
    m = _num(eq["m"]) if eq else None
    engine_age = (now - m) if m else None
    # engines running this build write an explicit heartbeat every cycle even
    # while standing aside without data; prefer it over the equity proxy
    hb = _num(storage.get_setting("engine_heartbeat_t", 0)) or 0
    if hb:
        engine_age = now - hb
    feed_t = _num(storage.get_setting("ea_last_feed_t", 0)) or 0
    feed_age = (now - feed_t) if feed_t else None
    try:
        import news_calendar
        cal = news_calendar.status()
    except Exception:
        cal = {}
    return {
        "engine_alive": engine_age is not None and engine_age < 180,
        "engine_last_seen_s": round(engine_age) if engine_age is not None else None,
        "ea_feed_age_s": round(feed_age) if feed_age is not None else None,
        "ea_connected": feed_age is not None and feed_age < 60,
        "db_integrity_suspect": storage.integrity_suspect() or None,
        "halt_new_entries": bool(storage.get_setting("halt_new_entries", False)),
        "trading_mode": storage.get_setting("trading_mode", "paper"),
        "open_trades": len(storage.open_trades()),
        "open_shadow": len(storage.open_trades("shadow")),
        "newest_bar_age_s_by_class": {k: round(v) for k, v in per_class_fresh.items()},
        "news_calendar": cal,
        "unacked_param_changes": len(storage.query(
            "SELECT id FROM param_changes WHERE ack=0 AND origin != 'human'")),
    }
