#!/usr/bin/env python3
"""Thin, dependency-free reader for the SLC bot's local API.

Usage:
    python3 slc.py status     # account + open trades + performance (compact)
    python3 slc.py signals    # recent signals with status + reason
    python3 slc.py perf       # closed + shadow performance
    python3 slc.py health     # is the server up / EA feeding / data fresh

Base URL: env SLC_BASE_URL, else http://127.0.0.1:8766.
Prints clean text (no raw JSON dumps); on connection failure it says how to start the server.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SLC_BASE_URL", "http://127.0.0.1:8766")


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"_error": "server not reachable at %s (%s).\n"
                          "Start it: cd \"$BOT_DIR\" && python3 server.py  "
                          "(or ./watchdog-install.sh status)"
                          % (BASE, getattr(e, "reason", e))}
    except Exception as e:                       # noqa: BLE001
        return {"_error": "%s: %s" % (type(e).__name__, e)}


def _err(d):
    if isinstance(d, dict) and d.get("_error"):
        print("⚠️  " + d["_error"])
        return True
    return False


def _money(x):
    try:
        return "${:,.2f}".format(float(x))
    except (TypeError, ValueError):
        return "—"


def cmd_status():
    st = get("/api/state")
    if _err(st):
        return 1
    perf = get("/api/performance")
    bal = st.get("paper_balance")
    feed = st.get("feed", {}) or {}
    acct = feed.get("account", {}) or {}
    eq = acct.get("equity", bal)
    opn = st.get("open_trades", []) or []
    print("📊 Keel — %s | EA: %s"
          % ("LIVE" if bal is None else "paper",
             "connected" if st.get("ea_connected") else "NOT connected"))
    print("   Balance %s | Equity %s | Open PnL %s | Shadow tracking %s"
          % (_money(bal if bal is not None else acct.get("balance")),
             _money(eq), _money(st.get("open_pnl")), st.get("shadow_open", 0)))
    if not opn:
        print("   No open trades.")
    else:
        print("   Open trades (%d):" % len(opn))
        for t in opn:
            up = t.get("upnl")
            tv = ""
            try:
                s = json.loads(t.get("setup") or "{}")
                if s.get("tv_score") is not None:
                    tv = " | TV %s/4 %s" % (s.get("tv_score"), s.get("tv_regime", ""))
            except (ValueError, TypeError):
                pass
            print("     %-4s %-7s %-8s g%s  entry %.5f sl %.5f  lots %s  uPnL %s%s%s"
                  % (t.get("side", "?").upper(), t.get("symbol", "?"),
                     t.get("trade_mode", "?"), t.get("grade", "?"),
                     t.get("entry", 0), t.get("sl", 0), t.get("lots", 0),
                     ("—" if up is None else _money(up)),
                     ("  [TP1 banked]" if t.get("tp1_done") else ""), tv))
    if not _err(perf):
        print("   Closed: n=%s win=%s%% exp=%sR totR=%s pf=%s"
              % (perf.get("n", 0), perf.get("win_pct", 0), perf.get("expectancy_R", 0),
                 perf.get("total_R", 0), perf.get("pf", 0)))
        sh = perf.get("shadow") or {}
        if sh:
            print("   Shadow: n=%s win=%s%% exp=%sR"
                  % (sh.get("n", 0), sh.get("win_pct", 0), sh.get("expectancy_R", 0)))
    return 0


def cmd_signals():
    d = get("/api/signals")
    if _err(d):
        return 1
    rows = d.get("signals", []) or []
    if not rows:
        print("No signals recorded yet.")
        return 0
    print("🔔 Recent signals (newest first):")
    for r in rows[:25]:
        tag = {"executed": "✅", "skipped": "⛔", "shadow": "👻"}.get(r.get("status"), "•")
        line = "  %s %-7s %-4s %-8s g%-3s rr%.1f  %s" % (
            tag, r.get("symbol", "?"), (r.get("side") or "?").upper(),
            r.get("trade_mode", "?"), r.get("grade", "?"), r.get("rr") or 0,
            r.get("status", ""))
        if r.get("reason"):
            line += " — " + r["reason"]
        print(line)
    return 0


def cmd_perf():
    d = get("/api/performance")
    if _err(d):
        return 1
    print("📈 Performance (closed): n=%s win=%s%% exp=%sR totR=%s pf=%s"
          % (d.get("n", 0), d.get("win_pct", 0), d.get("expectancy_R", 0),
             d.get("total_R", 0), d.get("pf", 0)))
    sh = d.get("shadow") or {}
    if sh:
        print("   Shadow: n=%s win=%s%% exp=%sR totR=%s"
              % (sh.get("n", 0), sh.get("win_pct", 0), sh.get("expectancy_R", 0),
                 sh.get("total_R", 0)))
    return 0


def cmd_health():
    st = get("/api/state")
    if _err(st):
        return 1
    feed = st.get("feed", {}) or {}
    age = st.get("server_time", 0) - (feed.get("last_feed_t") or 0)
    print("🩺 Health")
    print("   Server: up")
    print("   EA feed: %s (last push %ss ago)"
          % ("connected" if st.get("ea_connected") else "NOT connected", int(age) if age >= 0 else "?"))
    print("   Open trades: %d | Shadow: %s"
          % (len(st.get("open_trades", []) or []), st.get("shadow_open", 0)))
    print("   Tip: DB integrity → run hallucination_check.py (read-only); tests → tests/test_*.py")
    return 0


CMDS = {"status": cmd_status, "signals": cmd_signals, "perf": cmd_perf, "health": cmd_health}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = CMDS.get(cmd)
    if not fn:
        print("usage: slc.py [%s]" % "|".join(CMDS))
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main())
