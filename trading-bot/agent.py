"""Self-evaluation agent.

Every `interval_min` it computes performance stats from closed trades
and applies BOUNDED adjustments. Hard rules:

  MAY        raise/lower min_grade (B <-> A)
  MAY        nudge atr_buffer within bounds (one step of 0.05 per eval)
  MAY        nudge min_rr within bounds (one step of 0.1 per eval)
  MAY        disable a symbol (>=20 trades, expectancy < -0.2R)
  MAY        re-enable a disabled symbol after 14 days for re-trial
  MAY        disable a mode, intraday/swing (>=25 trades, negative expectancy)
  MAY NEVER  touch risk_pct, daily/weekly stops, max_concurrent,
             trading_mode, or anything else.

Every change is written to agent_log and announced on Telegram.
"""
import time
from typing import Any, Dict, List, Optional

import params_store
import storage

STEP_BUFFER = 0.05
STEP_RR = 0.1


def _stats(rows: List[Dict]) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    rs = [r["r_multiple"] or 0 for r in rows]
    wins = [r for r in rs if r > 0]
    gains = sum(r["pnl"] for r in rows if (r["pnl"] or 0) > 0)
    losses = -sum(r["pnl"] for r in rows if (r["pnl"] or 0) < 0)
    return {
        "n": n,
        "win_rate": round(100.0 * len(wins) / n, 1),
        "expectancy_r": round(sum(rs) / n, 3),
        "total_r": round(sum(rs), 2),
        "pnl": round(sum(r["pnl"] or 0 for r in rows), 2),
        "profit_factor": round(gains / losses, 2) if losses > 0 else None,
    }


def _closed(where: str = "", args: tuple = ()) -> List[Dict]:
    # shadow (watch-pair) trades are excluded from official stats/tuning
    return storage.query(
        "SELECT * FROM trades WHERE status='closed' AND mode != 'shadow' %s "
        "ORDER BY exit_time" % (("AND " + where) if where else ""), args)


def evaluate() -> Dict[str, Any]:
    """Full stats snapshot (also used by the dashboard /api/performance)."""
    out: Dict[str, Any] = {"overall": _stats(_closed())}
    for mode in ("paper", "live"):
        out[mode] = _stats(_closed("mode=?", (mode,)))
    for tm in ("intraday", "swing"):
        out[tm] = _stats(_closed("trade_mode=?", (tm,)))
    for g in ("A", "B"):
        out["grade_" + g] = _stats(_closed("grade=?", (g,)))
    out["by_symbol"] = {}
    for r in storage.query("SELECT DISTINCT symbol FROM trades "
                           "WHERE status='closed' AND mode != 'shadow'"):
        out["by_symbol"][r["symbol"]] = _stats(_closed("symbol=?", (r["symbol"],)))
    # shadow sample (watch pairs) reported separately — study data only
    out["shadow"] = _stats(storage.query(
        "SELECT * FROM trades WHERE status='closed' AND mode='shadow' ORDER BY exit_time"))
    return out


def _change(key: str, old, new, why: str, notify, trigger=None) -> None:
    """All agent tuning goes through params_store.set_param, which enforces
    the whitelist + bounds at the WRITE layer and records a structured
    param_changes row with the stats that triggered it (lesson 3: no silent
    tuning). A refused write is logged and announced, never applied.
    Change notifications are deliberately NOT gated on notify_agent — that
    flag only quiets routine eval summaries."""
    try:
        params_store.set_param(key, new, origin="agent", reason=why,
                               trigger_data=trigger)
    except params_store.ParamRejected as e:
        detail = "REFUSED %s: %s -> %s | %s" % (key, old, new, e)
        storage.log_agent("info", key, detail)
        print("agent:", detail)
        return
    detail = "%s: %s -> %s | %s" % (key, old, new, why)
    storage.log_agent("change", key, detail)
    notify("🤖 <b>Agent adjustment</b>\n<code>%s</code>: %s → %s\n<i>%s</i>"
           % (key, old, new, why))
    print("agent:", detail)


def run_once(cfg: Dict[str, Any], notify) -> None:
    min_trades = int(cfg.get("min_trades_per_eval", 15))
    bounds = cfg.get("bounds", {})
    b_buf = bounds.get("atr_buffer", [0.25, 0.60])
    b_rr = bounds.get("min_rr", [1.8, 3.0])

    last_eval = storage.get_setting("agent_last_eval_trade_id", 0)
    closed = _closed()
    new_since = [t for t in closed if t["id"] > last_eval]
    storage.log_agent("eval", "evaluation run",
                      "%d closed total, %d new since last eval" % (len(closed), len(new_since)))
    if len(closed) < min_trades or len(new_since) < 5:
        return  # not enough evidence to act — evaluating without acting

    s = evaluate()

    # 1. Grade B quality control ------------------------------------------
    gb = s["grade_B"]
    min_grade = storage.get_setting("min_grade", "B")
    if gb.get("n", 0) >= 15 and gb.get("expectancy_r", 0) < 0 and min_grade == "B":
        _change("min_grade", "B", "A",
                "B setups expectancy %.2fR over %d trades — A+ only from now"
                % (gb["expectancy_r"], gb["n"]), notify, trigger=gb)
    elif gb.get("n", 0) >= 15 and gb.get("expectancy_r", 0) > 0.15 and min_grade == "A":
        _change("min_grade", "A", "B",
                "B setups recovered to +%.2fR — re-enabled at half risk"
                % gb["expectancy_r"], notify, trigger=gb)

    # 2. Per-symbol kill switch -------------------------------------------
    disabled = storage.get_setting("agent_disabled_pairs", [])
    for sym, st in s["by_symbol"].items():
        if st["n"] >= 20 and st["expectancy_r"] < -0.2 and sym not in disabled:
            disabled = disabled + [sym]
            storage.set_setting("agent_disabled_pair_t_" + sym, int(time.time()))
            _change("agent_disabled_pairs", "active", disabled,
                    "%s expectancy %.2fR over %d trades — disabled" %
                    (sym, st["expectancy_r"], st["n"]), notify, trigger=st)
    # re-trial after 14 days
    for sym in list(disabled):
        t0 = storage.get_setting("agent_disabled_pair_t_" + sym, 0)
        if t0 and time.time() - t0 > 14 * 86400:
            disabled.remove(sym)
            _change("agent_disabled_pairs", "disabled", disabled,
                    "%s re-enabled for re-trial after 14 days" % sym, notify)

    # 3. Mode kill switch ---------------------------------------------------
    disabled_modes = storage.get_setting("agent_disabled_modes", [])
    for tm in ("intraday", "swing"):
        st = s[tm]
        if st.get("n", 0) >= 25 and st.get("expectancy_r", 0) < 0 and tm not in disabled_modes:
            disabled_modes = disabled_modes + [tm]
            _change("agent_disabled_modes", "active", disabled_modes,
                    "%s mode expectancy %.2fR over %d trades — disabled"
                    % (tm, st["expectancy_r"], st["n"]), notify, trigger=st)

    # 4. ATR buffer tuning (wick-out analysis) ------------------------------
    # Losses where MFE >= 0.5R = price went our way then stopped us out:
    # the stop is too tight -> widen buffer one step.
    losers = [t for t in closed[-40:] if (t["r_multiple"] or 0) < 0]
    if len(losers) >= 10:
        wicked = [t for t in losers if (t["mfe"] or 0) >= 0.5]
        buf = float(storage.get_setting("atr_buffer", 0.35))
        frac = len(wicked) / len(losers)
        if frac > 0.4 and buf + STEP_BUFFER <= b_buf[1]:
            _change("atr_buffer", buf, round(buf + STEP_BUFFER, 2),
                    "%.0f%% of recent losers reached +0.5R before stopping out — widening buffer"
                    % (frac * 100), notify, trigger={"wicked_frac": round(frac, 3), "losers": len(losers)})
        elif frac < 0.1 and buf - STEP_BUFFER >= b_buf[0]:
            _change("atr_buffer", buf, round(buf - STEP_BUFFER, 2),
                    "clean stop-outs — tightening buffer to improve RR", notify,
                    trigger={"wicked_frac": round(frac, 3), "losers": len(losers)})

    # 5. min_rr tuning -------------------------------------------------------
    # Winners closed at TP2: if most runners exceed target by far (mfe >>
    # rr), raise min_rr a step; if many trades miss TP2 after TP1, lower.
    recent = closed[-40:]
    tp2_hits = [t for t in recent if t["exit_reason"] == "take profit (TP2)"]
    tp1_then_back = [t for t in recent
                     if t["tp1_done"] and (t["r_multiple"] or 0) < 1.0
                     and t["exit_reason"] != "take profit (TP2)"]
    rr = float(storage.get_setting("min_rr", 2.0))
    if len(recent) >= 20:
        if len(tp2_hits) >= 8 and rr + STEP_RR <= b_rr[1]:
            avg_overshoot = sum((t["mfe"] or 0) for t in tp2_hits) / len(tp2_hits)
            if avg_overshoot > rr + 0.8:
                _change("min_rr", rr, round(rr + STEP_RR, 2),
                        "runners average %.1fR MFE — extending targets" % avg_overshoot, notify,
                        trigger={"tp2_hits": len(tp2_hits), "avg_mfe": round(avg_overshoot, 2)})
        if len(tp1_then_back) > len(tp2_hits) and rr - STEP_RR >= b_rr[0]:
            _change("min_rr", rr, round(rr - STEP_RR, 2),
                    "%d trades faded after TP1 vs %d full targets — closer targets"
                    % (len(tp1_then_back), len(tp2_hits)), notify,
                    trigger={"tp1_then_back": len(tp1_then_back), "tp2_hits": len(tp2_hits)})

    storage.set_setting("agent_last_eval_trade_id", closed[-1]["id"])
    o = s["overall"]
    if storage.get_setting("notify_agent", True):
        notify("🤖 <b>Agent evaluation</b>\nTrades: %d | Win: %.1f%% | Expectancy: %.2fR | P&L: %.2f"
               % (o["n"], o["win_rate"], o["expectancy_r"], o["pnl"]))


def agent_loop(cfg: Dict[str, Any], notify) -> None:
    interval = int(cfg.get("interval_min", 240)) * 60
    print("agent: started (every %d min)" % (interval // 60))
    while True:
        time.sleep(interval)
        try:
            if storage.get_setting("agent_enabled", True):
                run_once(cfg, notify)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("agent error:", e)
