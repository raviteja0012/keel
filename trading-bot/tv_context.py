"""TradingView Context Module — Keel

Fetches daily indicator snapshot from TradingView's public scanner API
for all enabled + watch pairs. Cached to state/tv_context.json and
refreshed hourly (or on-demand).

Usage in engine/strategy:
    import tv_context
    ctx = tv_context.load()                    # fast, uses cache
    align = tv_context.direction_align(ctx, "EURUSD", "short")  # True/False
    regime = tv_context.get_regime(ctx, "EURUSD")               # "strong_trend" etc
    score  = tv_context.confluence_score(ctx, "EURUSD", "short") # 0-4

Run standalone to refresh cache:
    python3 tv_context.py [--notify]
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
from typing import Any, Dict, Optional

# Python 3.x on macOS (python.org installer) ships without system CA certs.
# Use certifi if available, otherwise fall back to unverified context.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl._create_unverified_context()

CACHE_PATH = "state/tv_context.json"
REFRESH_SECS = 3600   # re-fetch if cache older than 1 hour

# Broker symbol → TradingView ticker
SYMBOL_MAP: Dict[str, str] = {
    # Forex majors + minors
    "EURUSD": "FX:EURUSD",   "GBPUSD": "FX:GBPUSD",   "USDJPY": "FX:USDJPY",
    "AUDUSD": "FX:AUDUSD",   "USDCAD": "FX:USDCAD",   "NZDUSD": "FX:NZDUSD",
    "USDCHF": "FX:USDCHF",   "EURGBP": "FX:EURGBP",   "EURJPY": "FX:EURJPY",
    "GBPJPY": "FX:GBPJPY",   "AUDJPY": "FX:AUDJPY",   "CADJPY": "FX:CADJPY",
    "CHFJPY": "FX:CHFJPY",   "NZDJPY": "FX:NZDJPY",   "EURAUD": "FX:EURAUD",
    "EURCAD": "FX:EURCAD",   "EURCHF": "FX:EURCHF",   "EURNZD": "FX:EURNZD",
    "GBPAUD": "FX:GBPAUD",   "GBPCAD": "FX:GBPCAD",   "GBPCHF": "FX:GBPCHF",
    "GBPNZD": "FX:GBPNZD",   "AUDCAD": "FX:AUDCAD",   "AUDCHF": "FX:AUDCHF",
    "AUDNZD": "FX:AUDNZD",   "NZDCAD": "FX:NZDCAD",   "NZDCHF": "FX:NZDCHF",
    "CADCHF": "FX:CADCHF",
    # Metals
    "XAUUSD": "TVC:GOLD",    "XAGUSD": "TVC:SILVER",
    "XPTUSD": "TVC:PLATINUM","XPDUSD": "TVC:PALLADIUM",
    # Energy
    "USOIL":  "TVC:USOIL",   "UKOIL":  "TVC:UKOIL",
    # Indices
    "US500":  "FOREXCOM:SPXUSD", "US30": "FOREXCOM:DJIA", "NAS100": "FOREXCOM:NSXUSD",
    # Crypto
    "BTCUSD": "BINANCE:BTCUSDT", "ETHUSD": "BINANCE:ETHUSDT",
    "LTCUSD": "BINANCE:LTCUSDT", "XRPUSD": "BINANCE:XRPUSDT",
    "SOLUSD": "BINANCE:SOLUSDT", "ADAUSD": "BINANCE:ADAUSDT",
    "DOGUSD": "BINANCE:DOGEUSDT","LINKUSD":"BINANCE:LINKUSDT",
    "BCHUSD": "BINANCE:BCHUSDT",
}

COLUMNS = [
    "close", "open", "high", "low", "volume",
    "change", "change_abs",
    "RSI", "RSI[1]",
    "ATR",
    "EMA20", "EMA50", "EMA200",
    "MACD.macd", "MACD.signal", "MACD.hist",
    "Stoch.K", "Stoch.D",
    "ADX", "ADX+DI", "ADX-DI",
    "Mom",
    "BB.upper", "BB.lower", "BB.basis",
    "Volatility.D",
]


# ─────────────────────────── fetch ───────────────────────────────────────────

def _fetch(symbols: list[str]) -> Dict[str, Any]:
    """Call TV scanner and return broker-keyed dict."""
    tv_map = {v: k for k, v in SYMBOL_MAP.items()}
    tickers = [SYMBOL_MAP[s] for s in symbols if s in SYMBOL_MAP]
    if not tickers:
        return {}
    payload = json.dumps({"symbols": {"tickers": tickers},
                          "columns": COLUMNS}).encode()
    req = urllib.request.Request(
        "https://scanner.tradingview.com/global/scan",
        data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=25, context=_SSL_CTX) as resp:
        raw = json.loads(resp.read())
    out = {}
    for item in raw.get("data", []):
        broker_sym = tv_map.get(item["s"], item["s"])
        vals = item["d"]
        rec = dict(zip(COLUMNS, vals))
        rec["tv_ticker"] = item["s"]
        out[broker_sym] = rec
    return out


def refresh(symbols: Optional[list[str]] = None) -> Dict[str, Any]:
    """Fetch fresh data for all symbols and write cache."""
    if symbols is None:
        try:
            import storage
            storage.init()
            s = storage.all_settings()
            enabled = s.get("enabled_pairs", [])
            watch = [w for w in s.get("watch_pairs", []) if w not in enabled]
            symbols = enabled + watch
        except Exception:
            symbols = list(SYMBOL_MAP.keys())

    data = _fetch(symbols)
    if not data:
        return {}

    # Derive per-symbol scored analysis
    for sym, d in data.items():
        d["_regime"]    = _regime(d)
        d["_direction"] = _direction(d)
        d["_ema_align"] = _ema_align(d)
        d["_bb_pos"]    = _bb_pos(d)

    os.makedirs("state", exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump({"ts": time.time(), "data": data}, f, indent=2)
    return data


# ─────────────────────────── load (cached) ───────────────────────────────────

def load(max_age: int = REFRESH_SECS) -> Dict[str, Any]:
    """Return cached data (auto-refreshes if stale or missing)."""
    if os.path.exists(CACHE_PATH):
        try:
            cache = json.load(open(CACHE_PATH))
            if time.time() - cache.get("ts", 0) < max_age:
                return cache.get("data", {})
        except Exception:
            pass
    return refresh()


# ─────────────────────────── derived metrics ─────────────────────────────────

def _regime(d: Dict) -> str:
    adx = d.get("ADX") or 0
    if adx >= 35:
        return "strong_trend"
    elif adx >= 20:
        return "moderate_trend"
    return "ranging"


def _direction(d: Dict) -> str:
    """Majority vote across 4 indicators: ADX DI, RSI, MACD, Momentum."""
    bull = 0
    if (d.get("ADX+DI") or 0) > (d.get("ADX-DI") or 0): bull += 1
    if (d.get("RSI") or 50) > 50:                         bull += 1
    if (d.get("MACD.macd") or 0) > (d.get("MACD.signal") or 0): bull += 1
    if (d.get("Mom") or 0) > 0:                           bull += 1
    return "long" if bull >= 3 else "short"


def _ema_align(d: Dict) -> str:
    c, e20, e50 = d.get("close") or 0, d.get("EMA20") or 0, d.get("EMA50") or 0
    if c > e20 > e50:  return "bullish"
    if c < e20 < e50:  return "bearish"
    return "mixed"


def _bb_pos(d: Dict) -> float:
    lo, hi = d.get("BB.lower") or 0, d.get("BB.upper") or 1
    c = d.get("close") or 0
    rng = hi - lo
    return round((c - lo) / rng, 3) if rng > 0 else 0.5


# ─────────────────────────── public helpers ──────────────────────────────────

def get_regime(ctx: Dict[str, Any], symbol: str) -> str:
    """'strong_trend' | 'moderate_trend' | 'ranging' | 'unknown'"""
    d = ctx.get(symbol)
    if d is None:
        return "unknown"
    return d.get("_regime") or _regime(d)


def direction_align(ctx: Dict[str, Any], symbol: str,
                    signal_dir: str) -> bool:
    """True if TV macro direction agrees with signal direction.

    signal_dir should be 'long' or 'short' (case-insensitive).
    """
    d = ctx.get(symbol)
    if d is None:
        return True   # no data → don't block
    tv_dir = d.get("_direction") or _direction(d)
    return tv_dir == signal_dir.lower()


def confluence_score(ctx: Dict[str, Any], symbol: str,
                     signal_dir: str) -> int:
    """0-4 score: how many TV factors confirm the signal.

    Used as an optional signal quality boost (not a hard block).
      0 = no confirmation
      4 = full confluence (trending + direction + EMA + RSI extreme)
    """
    d = ctx.get(symbol)
    if d is None:
        return 0
    dir_ = signal_dir.lower()
    score = 0
    tv_dir = d.get("_direction") or _direction(d)
    regime = d.get("_regime") or _regime(d)
    ema    = d.get("_ema_align") or _ema_align(d)
    rsi    = d.get("RSI") or 50

    if tv_dir == dir_:
        score += 1
    if regime in ("strong_trend", "moderate_trend"):
        score += 1
    if (dir_ == "long"  and ema == "bullish") or \
       (dir_ == "short" and ema == "bearish"):
        score += 1
    # RSI confirms continuation
    if (dir_ == "long"  and rsi < 45) or \
       (dir_ == "short" and rsi > 55):
        score += 1  # momentum still has room to run

    return score


def summary_line(ctx: Dict[str, Any], symbol: str) -> str:
    """One-line human-readable summary for a symbol."""
    d = ctx.get(symbol)
    if d is None:
        return f"{symbol}: no TV data"
    regime = d.get("_regime") or _regime(d)
    dir_   = d.get("_direction") or _direction(d)
    rsi    = d.get("RSI") or 0
    adx    = d.get("ADX") or 0
    chg    = d.get("change") or 0
    return (f"{symbol}: {dir_.upper()} | {regime} | "
            f"RSI={rsi:.1f} ADX={adx:.1f} chg={chg:+.2f}%")


# ─────────────────────────── standalone run ──────────────────────────────────

def _print_report(data: Dict[str, Any]) -> None:
    print(f"\n{'='*70}")
    print(f"TV Context Snapshot — {time.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*70}")
    print(f"{'SYMBOL':<10} {'PRICE':>10} {'RSI':>6} {'ADX':>6} {'DIR':>6} "
          f"{'REGIME':<16} {'EMA':>8} {'BB%':>5} {'CHG%':>6}")
    print(f"{'='*70}")
    for sym in sorted(data):
        d = data[sym]
        print(f"{sym:<10} {d.get('close',0):>10.4f} "
              f"{d.get('RSI',0):>6.1f} {d.get('ADX',0):>6.1f} "
              f"{(d.get('_direction','?')).upper():>6} "
              f"{d.get('_regime','?'):<16} "
              f"{d.get('_ema_align','?'):>8} "
              f"{d.get('_bb_pos',0):>5.2f} "
              f"{d.get('change',0):>+6.2f}")

    # Top opportunities
    scored = []
    for sym, d in data.items():
        dir_ = d.get("_direction", "short")
        s = confluence_score(data, sym, dir_)
        scored.append((s, sym, d.get("_direction","?"), d.get("RSI",0),
                       d.get("ADX",0), d.get("_regime","?")))
    print(f"\n{'TOP OPPORTUNITIES':}")
    print(f"{'-'*50}")
    for s, sym, dir_, rsi, adx, reg in sorted(scored, reverse=True)[:12]:
        print(f"  [{s}] {sym:<10} {dir_.upper():<6} "
              f"RSI={rsi:.1f} ADX={adx:.1f} {reg}")

    # USD bias summary
    usd_long  = ["USDCAD", "USDJPY", "USDCHF"]
    usd_short = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"]
    usd_bull  = (sum(1 for s in usd_long  if data.get(s, {}).get("_direction") == "long") +
                 sum(1 for s in usd_short if data.get(s, {}).get("_direction") == "short"))
    total = len(usd_long) + len(usd_short)
    print(f"\n  USD macro: {usd_bull}/{total} pairs favour USD strength "
          f"({'BULLISH' if usd_bull >= 5 else 'MIXED' if usd_bull >= 3 else 'BEARISH'})")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true", help="send summary to Telegram/Discord")
    ap.add_argument("--force", action="store_true", help="bypass cache and re-fetch")
    args = ap.parse_args()

    if args.force or not os.path.exists(CACHE_PATH):
        data = refresh()
    else:
        data = load(max_age=0 if args.force else REFRESH_SECS)

    _print_report(data)
    print(f"\nCache: {CACHE_PATH}")

    if args.notify and data:
        try:
            import notifier
            notifier.start()
            # Build short summary
            lines = ["📊 <b>[SLC BOT] TV Context Update</b>"]
            usd_long  = ["USDCAD", "USDJPY", "USDCHF"]
            usd_short = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"]
            usd_bull  = (sum(1 for s in usd_long  if data.get(s, {}).get("_direction") == "long") +
                         sum(1 for s in usd_short if data.get(s, {}).get("_direction") == "short"))
            usd_label = "BULLISH" if usd_bull >= 5 else "MIXED" if usd_bull >= 3 else "BEARISH"
            lines.append(f"USD macro: {usd_bull}/7 {usd_label}")
            # Top 5 setups
            scored = sorted(
                [(confluence_score(data, s, data[s].get("_direction","short")), s,
                  data[s].get("_direction","?"), data[s].get("RSI",0), data[s].get("ADX",0))
                 for s in data],
                reverse=True
            )[:5]
            lines.append("Top setups:")
            for sc, sym, d, rsi, adx in scored:
                lines.append(f"  • {sym} {d.upper()} [score {sc}] RSI={rsi:.0f} ADX={adx:.0f}")
            notifier.send("\n".join(lines))
            time.sleep(3)
        except Exception as e:
            print(f"notify failed: {e}")
