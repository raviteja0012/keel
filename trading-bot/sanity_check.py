"""SLC strategy sanity check.

Runs on demand or weekly (see watchdog-install.sh). Combines:
  1. LIVE results   — closed paper/live trades from the database
  2. SHADOW sample  — watch-pair trades collected in the background
  3. BACKTEST GRID  — parameter sensitivity sweep over the full bar
                      history (min_rr, atr_buffer, grade filter, 2x
                      spread stress), using the exact live strategy code

and produces rule-based recommendations. Nothing is auto-changed:
the report is written to state/sanity_report.md and a summary is sent
to Telegram/Discord (--notify).

    python3 sanity_check.py            # full run, report only
    python3 sanity_check.py --notify   # also push summary notification
    python3 sanity_check.py --quick    # swing-only, smaller grid
"""
from __future__ import annotations

import argparse
import time
from typing import Any, Dict, List

import storage
import backtest as bt
try:
    import tv_context as _tv
    _TV_AVAILABLE = True
except ImportError:
    _TV_AVAILABLE = False


def fmt_stats(st: Dict[str, Any]) -> str:
    if not st.get("n"):
        return "n=0"
    return ("n=%d win=%s%% exp=%sR totR=%s pf=%s dd=%sR"
            % (st["n"], st["win%"], st["expectancy_R"], st["total_R"],
               st["pf"], st["maxDD_R"]))


def db_stats(where: str, args: tuple = ()) -> Dict[str, Any]:
    rows = storage.query(
        "SELECT r_multiple FROM trades WHERE status='closed' AND %s" % where, args)
    rs = [r["r_multiple"] or 0 for r in rows]
    n = len(rs)
    if n == 0:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    return {"n": n, "win%": round(100 * len(wins) / n, 1),
            "expectancy_R": round(sum(rs) / n, 3),
            "total_R": round(sum(rs), 2), "pf": None, "maxDD_R": None}


def run_grid(symbols: List[str], modes: List[str], base: Dict,
             quick: bool) -> List[Dict]:
    # The intraday walk (15m bars) is ~3x the work of swing and its verdict
    # rarely changes with parameters — so the variant grid runs on SWING
    # only; intraday is evaluated once, inside the baseline.
    variants = [("baseline (current settings)", {}, 1.0, modes)]
    grid_modes = [m for m in modes if m == "swing"] or modes
    if not quick:
        variants += [
            ("min_rr 1.8", {"min_rr": 1.8}, 1.0, grid_modes),
            ("min_rr 2.5", {"min_rr": 2.5}, 1.0, grid_modes),
            ("atr_buffer 0.25", {"atr_buffer": 0.25}, 1.0, grid_modes),
            ("atr_buffer 0.50", {"atr_buffer": 0.50}, 1.0, grid_modes),
            ("grade A only", {"min_grade": "A"}, 1.0, grid_modes),
        ]
    variants += [("spread x2 stress", {}, 2.0, grid_modes)]

    results = []
    for name, over, smult, vmodes in variants:
        params = dict(base, **over)
        trades = []
        for sym in symbols:
            spread = bt.est_spread(sym) * smult
            for mode in vmodes:
                trades += bt.run_symbol_mode(sym, mode, params, spread)
        res = {"name": name, "over": over, "stress": smult > 1.0,
               "overall": bt.stats(trades),
               "swing": bt.stats([t for t in trades if t.mode == "swing"]),
               "intraday": bt.stats([t for t in trades if t.mode == "intraday"]),
               "trades": trades}
        results.append(res)
        print("grid: %-28s %s" % (name, fmt_stats(res["overall"])), flush=True)
    return results


# Bounded auto-tuning: a parameter variant is applied only if it beats the
# baseline by >=15% total swing R (with n>=30) on TWO consecutive daily runs.
# Bounds come from params_store — the same source of truth the agent and the
# hallucination check use — and the write itself goes through set_param
# (origin='sanity'), so the whitelist/bounds/human-pin rules are enforced at
# the write layer, not by this script's discipline.
import params_store
APPLY_BOUNDS = {k: params_store.BOUNDS[k] for k in ("min_rr", "atr_buffer")}


def auto_apply(results: List[Dict], recs: List[str]) -> None:
    import json
    import os
    baseline = results[0]["swing"]
    best = None
    for r in results[1:]:
        if r["stress"] or not r["over"]:
            continue
        key = list(r["over"].keys())[0]
        if key not in APPLY_BOUNDS:
            continue                              # grade etc: report-only
        rs = r["swing"]
        if (rs.get("n", 0) >= 30 and baseline.get("n", 0) >= 30
                and rs.get("total_R", 0) > max(1.0, baseline.get("total_R", 0)) * 1.15
                and (best is None or rs["total_R"] > best["swing"]["total_R"])):
            best = r

    path = "state/sanity_pending.json"
    prev = {}
    if os.path.exists(path):
        try:
            prev = json.load(open(path))
        except Exception:
            prev = {}

    if best is None:
        json.dump({}, open(path, "w"))
        return
    key, val = list(best["over"].items())[0]
    lo, hi = APPLY_BOUNDS[key]
    val = max(lo, min(hi, val))
    if prev.get("name") == best["name"]:
        # confirmed on a second consecutive run -> apply (through the guarded
        # write layer; a refusal is reported, never silently forced)
        old = storage.get_setting(key, None)
        try:
            params_store.set_param(
                key, val, origin="sanity",
                reason="sanity auto-tune: variant '%s' beat baseline 2 days running"
                       % best["name"],
                trigger_data={"variant": best["name"],
                              "variant_swing": best["swing"],
                              "baseline_swing": baseline})
        except params_store.ParamRejected as e:
            recs.insert(0, "REFUSED by write layer: %s" % e)
            json.dump({}, open(path, "w"))
            return
        storage.log_agent("change", key,
                          "sanity auto-tune: %s -> %s (variant '%s' beat baseline "
                          "2 days running)" % (old, val, best["name"]))
        recs.insert(0, "APPLIED: %s %s -> %s (won the grid two consecutive days)."
                    % (key, old, val))
        json.dump({}, open(path, "w"))
    else:
        json.dump({"name": best["name"], "ts": time.time()}, open(path, "w"))
        recs.insert(0, "Pending: '%s' won today's grid — will be auto-applied "
                    "if it wins again tomorrow." % best["name"])


def recommendations(results: List[Dict], base: Dict) -> List[str]:
    recs: List[str] = []
    baseline = results[0]
    bswing, bintra = baseline["swing"], baseline["intraday"]

    if bintra.get("n", 0) >= 15 and bintra.get("expectancy_R", 0) < -0.1:
        recs.append("Intraday expectancy %.2fR over %d backtest trades — keep it "
                    "DISABLED (or disable it if currently on)."
                    % (bintra["expectancy_R"], bintra["n"]))
    if bswing.get("n", 0) >= 20 and bswing.get("expectancy_R", 0) > 0:
        recs.append("Swing remains positive: %.2fR over %d trades (PF %s)."
                    % (bswing["expectancy_R"], bswing["n"], bswing["pf"]))
    elif bswing.get("n", 0) >= 20:
        recs.append("⚠ Swing expectancy turned %.2fR over %d trades — "
                    "investigate before adding risk."
                    % (bswing.get("expectancy_R", 0), bswing["n"]))

    # parameter sensitivity: does any variant beat baseline swing by >25%?
    for r in results[1:]:
        if r["name"].startswith("spread"):
            continue
        rs, bs = r["swing"], bswing
        if (rs.get("n", 0) >= 20 and bs.get("n", 0) >= 20
                and rs.get("total_R", 0) > max(1.0, bs.get("total_R", 0)) * 1.25):
            recs.append("Variant '%s' outperforms baseline on swing "
                        "(%.1fR vs %.1fR total) — worth testing."
                        % (r["name"], rs["total_R"], bs.get("total_R", 0)))

    stress = next((r for r in results if r["name"].startswith("spread")), None)
    if stress and stress["swing"].get("n"):
        se = stress["swing"].get("expectancy_R", 0)
        if se <= 0:
            recs.append("⚠ Swing edge DISAPPEARS at 2x spread (%.2fR) — avoid "
                        "trading during spread-widening hours (rollover, news)."
                        % se)
        else:
            recs.append("Swing edge survives 2x spread stress (%.2fR) — cost-robust." % se)

    # weakest symbols in baseline
    by_sym: Dict[str, List] = {}
    for t in baseline["trades"]:
        by_sym.setdefault(t.symbol, []).append(t)
    weak = []
    for sym, ts in by_sym.items():
        st = bt.stats(ts)
        if st["n"] >= 4 and st["total_R"] <= -2.0:
            weak.append("%s (%.1fR/%d)" % (sym, st["total_R"], st["n"]))
    if weak:
        recs.append("Weakest backtest symbols: %s — keep watch-only / consider "
                    "removing from enabled pairs." % ", ".join(sorted(weak)))

    best = []
    for sym, ts in by_sym.items():
        st = bt.stats(ts)
        if st["n"] >= 4 and st["total_R"] >= 2.0:
            best.append("%s (+%.1fR/%d)" % (sym, st["total_R"], st["n"]))
    if best:
        recs.append("Strongest backtest symbols: %s." % ", ".join(sorted(best)))
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="bounded auto-tune: apply a variant that wins the "
                         "grid two consecutive runs (min_rr/atr_buffer only)")
    ap.add_argument("--modes", default="intraday,swing",
                    help="restrict modes, e.g. --modes swing")
    args = ap.parse_args()

    storage.init()
    s = storage.all_settings()
    base = {
        "min_rr": float(s.get("min_rr", 2.0)),
        "atr_buffer": float(s.get("atr_buffer", 0.35)),
        "min_grade": s.get("min_grade", "B"),
        "regime_max": float(s.get("regime_max", 2.5)),
        "regime_b_ban": float(s.get("regime_b_ban", 1.5)),
        "max_spread_frac": float(s.get("max_spread_frac", 0.10)),
    }
    enabled = s.get("enabled_pairs", [])
    watch = [w for w in s.get("watch_pairs", []) if w not in enabled]
    symbols = enabled + watch
    modes = ["swing"] if args.quick else \
        [m.strip() for m in args.modes.split(",") if m.strip()]

    t0 = time.time()
    live = db_stats("mode IN ('paper','live')")
    shadow = db_stats("mode = 'shadow'")
    results = run_grid(symbols, modes, base, args.quick)
    recs = recommendations(results, base)
    if args.apply:
        auto_apply(results, recs)

    # TV context snapshot
    tv_data = {}
    tv_section = []
    if _TV_AVAILABLE:
        try:
            tv_data = _tv.refresh(symbols)  # always fresh for sanity run
            # USD macro vote
            usd_long  = ["USDCAD", "USDJPY", "USDCHF"]
            usd_short = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"]
            usd_bull  = (sum(1 for s in usd_long  if tv_data.get(s, {}).get("_direction") == "long") +
                         sum(1 for s in usd_short if tv_data.get(s, {}).get("_direction") == "short"))
            total_usd = len(usd_long) + len(usd_short)
            usd_label = "BULLISH" if usd_bull >= 5 else "MIXED" if usd_bull >= 3 else "BEARISH"
            tv_section.append("USD macro: %d/%d pairs → %s" % (usd_bull, total_usd, usd_label))
            # Top trending pairs (ADX > 30)
            trending = [(s, d.get("ADX", 0), d.get("_direction","?"))
                        for s, d in tv_data.items() if (d.get("ADX") or 0) >= 30]
            trending.sort(key=lambda x: -x[1])
            if trending:
                tv_section.append("Trending (ADX≥30): " +
                                   ", ".join("%s %s(%.0f)" % (s, d.upper(), a)
                                             for s, a, d in trending[:8]))
            # Ranging pairs (ADX < 15) — lower quality for trend-following
            ranging = [s for s, d in tv_data.items() if (d.get("ADX") or 0) < 15]
            if ranging:
                tv_section.append("Low-ADX/ranging (<15): " + ", ".join(sorted(ranging)[:10]))
        except Exception as e:
            tv_section.append("TV context fetch error: %s" % e)

    # ---------------- report ------------------------------------------
    lines = ["# SLC Sanity Report — %s" % time.strftime("%Y-%m-%d %H:%M"),
             "", "Settings: min_rr=%.1f atr_buffer=%.2f grade>=%s | %d symbols, modes: %s"
             % (base["min_rr"], base["atr_buffer"], base["min_grade"],
                len(symbols), "+".join(modes)), ""]
    lines += ["## Live/paper closed trades", fmt_stats(live), ""]
    lines += ["## Shadow (watch-pair) sample", fmt_stats(shadow), ""]
    if tv_section:
        lines += ["## TradingView market context", ""] + tv_section + [""]
    lines += ["## Backtest grid", "",
              "| variant | overall | swing | intraday |", "|---|---|---|---|"]
    for r in results:
        lines.append("| %s | %s | %s | %s |" % (
            r["name"], fmt_stats(r["overall"]),
            fmt_stats(r["swing"]), fmt_stats(r["intraday"])))
    lines += ["", "## Recommendations", ""]
    lines += ["- " + r for r in recs] if recs else ["- Nothing actionable yet."]
    lines += ["", "_grid runtime: %.0fs_" % (time.time() - t0)]

    import os
    os.makedirs("state", exist_ok=True)
    with open("state/sanity_report.md", "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines[-(len(recs) + 4):]))
    print("\nreport -> state/sanity_report.md")

    if args.notify and recs:
        try:
            import notifier
            notifier.start()
            notifier.send("🧪 <b>Weekly sanity check</b>\n"
                          + "\n".join("• " + r for r in recs[:5])
                          + "\n<i>full report: trading-bot/state/sanity_report.md</i>")
            time.sleep(4)                       # let the queue flush
        except Exception as e:
            print("notify failed:", e)


if __name__ == "__main__":
    main()
