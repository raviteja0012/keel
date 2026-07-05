#!/usr/bin/env python3
"""Agent grounding / anti-hallucination check  (SLC bot)

A read-only daily audit that verifies the bot's auto-tuning agent is acting on
REAL, healthy data and within its allowed authority — i.e. not "hallucinating"
decisions from corrupt/stale data or stepping outside its rules.

Checks (FAIL = hard problem, WARN = look):
  1. DB integrity ok               — corrupt data makes every stat meaningless
  2. Data freshness                — stale bars/feed => evals on old data
  3. Agent changed only ALLOWED keys (min_grade/atr_buffer/min_rr/
     agent_disabled_pairs/agent_disabled_modes) — never risk_pct, stops,
     max_concurrent, trading_mode
  4. Tuned params within bounds    — atr_buffer in [0.25,0.60], min_rr in [1.8,3.0]
  5. Disables are sample-justified — symbol disable needs >=20 closed trades,
     mode disable >=25 (else the agent acted on too little data)
  6. Forbidden risk settings still match config (risk_pct, stops, max_concurrent)
  7. Ground-truth recompute        — prints the real win/expectancy the agent
     should be seeing, computed straight from the trades table

Writes a line to state/hallucination_check.jsonl and prints a verdict.
Never modifies anything.
"""
import os, sys, json, time, sqlite3, re

HERE = os.path.dirname(os.path.abspath(__file__))
BOT  = os.path.join(HERE, "trading-bot")
DB   = os.path.join(BOT, "data", "trading.db")
CFG  = os.path.join(BOT, "config.yaml")
LOG  = os.path.join(HERE, "hallucination_check.jsonl")

# Single source of truth: whitelist + bounds come from params_store (the same
# constants the write layer enforces), so this audit can never drift from the
# tuner. Falls back to the historical constants if the module isn't importable
# (e.g. running against an old checkout).
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading-bot"))
    import params_store as _ps
    ALLOWED_AGENT_KEYS = set(_ps.WHITELISTS["agent"])
    BUF_BOUNDS = _ps.BOUNDS["atr_buffer"]
    RR_BOUNDS = _ps.BOUNDS["min_rr"]
except Exception:
    _ps = None
    ALLOWED_AGENT_KEYS = {"min_grade", "atr_buffer", "min_rr",
                          "agent_disabled_pairs", "agent_disabled_modes"}
    BUF_BOUNDS = (0.25, 0.60)
    RR_BOUNDS = (1.8, 3.0)
FORBIDDEN_KEYS = {"risk_pct", "daily_stop_pct", "weekly_stop_pct",
                  "max_concurrent", "trading_mode"}
FRESH_HRS = 12          # newest bar should be within this many hours (forex;
                        # crypto is audited separately at 4h below)
CRYPTO_FRESH_HRS = 4
CRYPTO_PREFIXES = ("BTC", "ETH", "ADA", "DOG", "LINK", "BCH", "SOL", "XRP")

fails, warns, info = [], [], {}


def cfg_risk():
    out = {}
    try:
        import yaml
        out = (yaml.safe_load(open(CFG)) or {}).get("risk", {})
    except Exception:
        for ln in open(CFG):
            m = re.match(r"\s*([a-z_]+):\s*([-\d.]+)", ln)
            if m:
                try: out[m.group(1)] = float(m.group(2))
                except: pass
    return out


def main():
    if not os.path.exists(DB):
        print("FAIL: db missing"); sys.exit(1)
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    c.row_factory = sqlite3.Row

    # 1. integrity
    integ = c.execute("PRAGMA integrity_check").fetchone()[0]
    info["integrity"] = integ
    if integ != "ok":
        fails.append("DB integrity NOT ok (%s…) — agent stats are unreliable" % integ[:40])

    # 2. freshness — clamp clock skew (broker bars can be timestamped ahead of
    # this machine's clock; a negative age means skew, not freshness)
    mt = c.execute("SELECT MAX(t) FROM bars").fetchone()[0] or 0
    age_h = max(0.0, (time.time() - mt) / 3600.0)
    info["newest_bar_age_h"] = round(age_h, 1)
    if age_h > FRESH_HRS:
        warns.append("newest bar is %.1fh old (>%dh) — feed may be stale" % (age_h, FRESH_HRS))
    # crypto trades 24/7 — a gap there is a dead feed, not a weekend
    like = " OR ".join("symbol LIKE '%s%%'" % p for p in CRYPTO_PREFIXES)
    mt_c = c.execute("SELECT MAX(t) FROM bars WHERE %s" % like).fetchone()[0]
    if mt_c:
        age_c = max(0.0, (time.time() - mt_c) / 3600.0)
        info["newest_crypto_bar_age_h"] = round(age_c, 1)
        if age_c > CRYPTO_FRESH_HRS:
            warns.append("newest CRYPTO bar is %.1fh old (>%dh) — 24/7 feed gap"
                         % (age_c, CRYPTO_FRESH_HRS))

    # settings snapshot
    st = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    def js(v):
        try: return json.loads(v)
        except: return v

    # 3 + 4. audit agent_log changes
    changes = list(c.execute(
        "SELECT t,action,detail FROM agent_log WHERE kind='change' ORDER BY t"))
    info["agent_changes"] = len(changes)
    for ch in changes:
        key = ch["action"]
        if key not in ALLOWED_AGENT_KEYS:
            fails.append("agent changed FORBIDDEN/unknown key '%s' (%s)" % (key, (ch["detail"] or "")[:60]))
        if key == "atr_buffer":
            m = re.findall(r"[-\d.]+", ch["detail"] or "")
            vals = [float(x) for x in m if x not in (".", "-")]
            if vals and not (BUF_BOUNDS[0] <= vals[-1] <= BUF_BOUNDS[1]):
                fails.append("atr_buffer change out of bounds: %s" % vals[-1])
        if key == "min_rr":
            m = re.findall(r"[-\d.]+", ch["detail"] or "")
            vals = [float(x) for x in m if x not in (".", "-")]
            if vals and not (RR_BOUNDS[0] <= vals[-1] <= RR_BOUNDS[1]):
                fails.append("min_rr change out of bounds: %s" % vals[-1])

    # 3b. structured param_changes audit (Phase 2+): every accepted automated
    # change must be whitelisted for its origin and inside bounds — this reads
    # exact old/new values instead of regex-parsing prose, and also verifies
    # refused writes were indeed not applied.
    have_pc = c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='param_changes'").fetchone()
    if have_pc and _ps is not None:
        pcs = list(c.execute("SELECT * FROM param_changes ORDER BY t"))
        info["param_changes"] = len(pcs)
        for pc in pcs:
            origin, key = pc["origin"], pc["key"]
            wl = _ps.WHITELISTS.get(origin)
            if pc["accepted"] and wl is not None and key not in wl:
                fails.append("param_changes: origin %r changed non-whitelisted "
                             "key '%s'" % (origin, key))
            b = _ps.BOUNDS.get(key)
            if pc["accepted"] and b is not None:
                try:
                    v = float(json.loads(pc["new"]))
                    if not (b[0] <= v <= b[1]):
                        fails.append("param_changes: %s=%s outside bounds %s "
                                     "(origin %s)" % (key, v, list(b), origin))
                except (ValueError, TypeError):
                    pass
        n_refused = sum(1 for pc in pcs if not pc["accepted"])
        if n_refused:
            info["param_changes_refused"] = n_refused
    # current tuned params within bounds
    try:
        buf = float(st.get("atr_buffer", 0.35))
        if not (BUF_BOUNDS[0] <= buf <= BUF_BOUNDS[1]):
            fails.append("live atr_buffer %.2f out of bounds %s" % (buf, BUF_BOUNDS))
    except Exception: pass
    try:
        rr = float(st.get("min_rr", 2.5))
        if not (RR_BOUNDS[0] <= rr <= RR_BOUNDS[1]):
            fails.append("live min_rr %.2f out of bounds %s" % (rr, RR_BOUNDS))
    except Exception: pass

    # 5. disables must be sample-justified
    disabled = js(st.get("agent_disabled_pairs", "[]")) or []
    for sym in (disabled if isinstance(disabled, list) else []):
        n = c.execute("SELECT COUNT(*) FROM trades WHERE symbol=? AND status='closed'", (sym,)).fetchone()[0]
        if n < 20:
            fails.append("symbol %s disabled but only %d closed trades (<20) — ungrounded" % (sym, n))
    dmodes = js(st.get("agent_disabled_modes", "[]")) or []
    for tm in (dmodes if isinstance(dmodes, list) else []):
        n = c.execute("SELECT COUNT(*) FROM trades WHERE trade_mode=? AND status='closed'", (tm,)).fetchone()[0]
        if n < 25:
            fails.append("mode %s disabled but only %d closed trades (<25) — ungrounded" % (tm, n))

    # 6. forbidden risk settings vs config
    rc = cfg_risk()
    for k in ("risk_pct", "daily_stop_pct", "weekly_stop_pct", "max_concurrent"):
        if k in st and k in rc:
            try:
                if abs(float(st[k]) - float(rc[k])) > 1e-9:
                    warns.append("%s drifted from config (%s vs %s)" % (k, st[k], rc[k]))
            except Exception: pass

    # 7. ground-truth recompute
    def grp(where, args=()):
        rs = [r["r_multiple"] or 0 for r in c.execute(
            "SELECT r_multiple FROM trades WHERE status='closed' AND r_multiple IS NOT NULL AND %s" % where, args)]
        n = len(rs)
        if not n: return {"n": 0}
        w = [x for x in rs if x > 0]
        return {"n": n, "win": round(100*len(w)/n, 1), "exp": round(sum(rs)/n, 3)}
    info["truth"] = {"overall": grp("1=1"),
                     "swing": grp("trade_mode='swing'"),
                     "intraday": grp("trade_mode='intraday'"),
                     "paper": grp("mode='paper'")}
    info["agent_eval_min_trades"] = 15
    info["closed_total"] = c.execute("SELECT COUNT(*) FROM trades WHERE status='closed'").fetchone()[0]
    c.close()

    verdict = "FAIL" if fails else ("WARN" if warns else "GROUNDED")
    rec = {"ts": int(time.time()), "date": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
           "verdict": verdict, "fails": fails, "warns": warns, "info": info}
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

    print(json.dumps(rec, indent=2))
    print("\nVERDICT:", verdict,
          "— agent decisions are grounded in healthy data" if verdict == "GROUNDED"
          else ("— review warnings" if verdict == "WARN" else "— ungrounded/at-risk, investigate"))
    # distinct exit codes so automation can tell the three states apart:
    # 0 = GROUNDED, 1 = WARN, 2 = FAIL
    sys.exit(2 if verdict == "FAIL" else (1 if verdict == "WARN" else 0))


if __name__ == "__main__":
    main()
