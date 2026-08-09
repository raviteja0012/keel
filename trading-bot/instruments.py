"""Instrument registry — the single source of truth for per-symbol metadata.

Every subsystem that used to guess symbol semantics from string slicing
(news entity matching, session gating, exposure bucketing, display precision)
reads this registry instead. Rows live in the `instruments` table so the
dashboard can edit them; `seed_defaults()` populates every symbol the bot
already watches, and `classify()` infers sensible defaults for unknown
symbols so a newly-enabled pair never crashes the engine — it just gets
conservative metadata until curated.

Factor exposures express the STRUCTURAL bet of a BUY of the symbol as signed
weights per bucket (sell = negated). Buckets: individual currencies (USD, EUR,
...), GOLD/SILVER/OIL, CRYPTO, and RISK (equity-risk appetite). This is what
lets the exposure gate see that long EURUSD + long DAX + long XAUUSD all stack
short-USD / long-risk — the 30-of-47 correlated-losers lesson.
"""
import json
import threading
from typing import Any, Dict, List, Optional

import storage

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}
_cache_t = {"loaded": False}

_FIAT = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}

# Curated defaults for every symbol in the current enabled + watch lists,
# plus the common index CFDs Vantage offers. display_digits is cosmetic;
# factor_exposures / news_entities / session_calendar are load-bearing.
_DEFAULTS: Dict[str, Dict[str, Any]] = {}


def _fx(symbol: str) -> Dict[str, Any]:
    base, quote = symbol[:3], symbol[3:6]
    return {
        "asset_class": "forex", "session_calendar": "forex",
        "day_boundary": "broker", "display_digits": 3 if "JPY" in symbol else 5,
        "factor_exposures": {base: 1.0, quote: -1.0},
        "news_entities": [base, quote],
    }


for _s in ("EURUSD GBPUSD USDJPY AUDUSD USDCAD USDCHF NZDUSD "
           "AUDJPY CADJPY CHFJPY NZDJPY EURJPY GBPJPY "
           "EURAUD EURCAD EURCHF EURNZD EURGBP "
           "GBPAUD GBPCAD GBPCHF GBPNZD "
           "AUDCAD AUDCHF AUDNZD NZDCAD NZDCHF CADCHF").split():
    _DEFAULTS[_s] = _fx(_s)

_DEFAULTS.update({
    "XAUUSD": {"asset_class": "metals", "session_calendar": "metals",
               "day_boundary": "broker", "display_digits": 2,
               "factor_exposures": {"GOLD": 1.0, "USD": -1.0},
               "news_entities": ["XAU", "gold", "USD"]},
    "XAGUSD": {"asset_class": "metals", "session_calendar": "metals",
               "day_boundary": "broker", "display_digits": 3,
               "factor_exposures": {"SILVER": 0.7, "GOLD": 0.3, "USD": -1.0},
               "news_entities": ["XAG", "silver", "USD"]},
    "XPDUSD": {"asset_class": "metals", "session_calendar": "metals",
               "day_boundary": "broker", "display_digits": 2,
               "factor_exposures": {"SILVER": 0.5, "USD": -1.0, "RISK": 0.3},
               "news_entities": ["palladium", "USD"]},
    "USOIL":  {"asset_class": "energies", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 2,
               "factor_exposures": {"OIL": 1.0, "USD": -0.5, "RISK": 0.3},
               "news_entities": ["oil", "OPEC", "crude", "USD"]},
    "UKOIL":  {"asset_class": "energies", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 2,
               "factor_exposures": {"OIL": 1.0, "USD": -0.3, "RISK": 0.3},
               "news_entities": ["oil", "OPEC", "brent"]},
    # Index CFDs (Vantage). RISK is the shared equity-appetite bucket that
    # links them to each other and to crypto.
    "US30":   {"asset_class": "indices", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 1,
               "factor_exposures": {"RISK": 1.0, "USD": 0.3},
               "news_entities": ["USD", "Fed", "stocks", "Dow"]},
    "NAS100": {"asset_class": "indices", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 1,
               "factor_exposures": {"RISK": 1.0, "USD": 0.3, "TECH": 0.5},
               "news_entities": ["USD", "Fed", "stocks", "Nasdaq", "tech"]},
    "SP500":  {"asset_class": "indices", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 1,
               "factor_exposures": {"RISK": 1.0, "USD": 0.3},
               "news_entities": ["USD", "Fed", "stocks", "S&P"]},
    "GER40":  {"asset_class": "indices", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 1,
               "factor_exposures": {"RISK": 1.0, "EUR": 0.3},
               "news_entities": ["EUR", "ECB", "stocks", "DAX"]},
    "UK100":  {"asset_class": "indices", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 1,
               "factor_exposures": {"RISK": 1.0, "GBP": 0.3},
               "news_entities": ["GBP", "BoE", "stocks", "FTSE"]},
    "JPN225": {"asset_class": "indices", "session_calendar": "indices",
               "day_boundary": "broker", "display_digits": 0,
               "factor_exposures": {"RISK": 1.0, "JPY": -0.3},
               "news_entities": ["JPY", "BoJ", "stocks", "Nikkei"]},
})


def _crypto(symbol: str, coin: str, beta: float = 1.0) -> Dict[str, Any]:
    return {
        "asset_class": "crypto", "session_calendar": "crypto",
        "day_boundary": "utc", "display_digits": 2,
        # CRYPTO is the shared beta bucket (BTC/ETH set the regime; alts carry
        # a higher beta weight so an alt basket can't hide as "diversified").
        "factor_exposures": {"CRYPTO": beta, "USD": -0.5, "RISK": 0.5},
        "news_entities": [coin, "crypto", "bitcoin"],
    }


_DEFAULTS.update({
    "BTCUSD":  _crypto("BTCUSD", "BTC", 1.0),
    "ETHUSD":  _crypto("ETHUSD", "ETH", 1.0),
    "ADAUSD":  _crypto("ADAUSD", "ADA", 1.2),
    "DOGUSD":  _crypto("DOGUSD", "DOGE", 1.2),
    "LINKUSD": _crypto("LINKUSD", "LINK", 1.2),
    "BCHUSD":  _crypto("BCHUSD", "BCH", 1.2),
})

_CRYPTO_COINS = {"BTC", "ETH", "ADA", "DOG", "DOGE", "SOL", "XRP", "LTC",
                 "BCH", "LINK", "DOT", "AVA", "BNB", "TRX", "XLM", "UNI"}


def classify(symbol: str) -> Dict[str, Any]:
    """Best-effort metadata for a symbol with no curated/DB row. Conservative:
    unknown symbols get a generic RISK exposure so they still consume exposure
    budget rather than bypassing it."""
    s = symbol.upper()
    if s in _DEFAULTS:
        return dict(_DEFAULTS[s])
    if len(s) == 6 and s[:3] in _FIAT and s[3:] in _FIAT:
        return _fx(s)
    if s.startswith("XAU") or s.startswith("XAG") or s.startswith("XPD") or s.startswith("XPT"):
        return dict(_DEFAULTS["XAUUSD"], news_entities=["metals", "USD"])
    if any(s.startswith(c) for c in _CRYPTO_COINS):
        return _crypto(s, s[:3], 1.2)
    if "OIL" in s or s in ("NGAS", "XNGUSD"):
        return dict(_DEFAULTS["USOIL"])
    return {"asset_class": "indices", "session_calendar": "indices",
            "day_boundary": "broker", "display_digits": 2,
            "factor_exposures": {"RISK": 1.0},
            "news_entities": []}


def seed_defaults() -> int:
    """Insert curated rows for any symbol not yet in the instruments table.
    Existing rows are never overwritten (the DB is user-editable truth)."""
    n = 0
    existing = {r["symbol"] for r in storage.query("SELECT symbol FROM instruments")}
    for sym, meta in _DEFAULTS.items():
        if sym in existing:
            continue
        storage.execute(
            "INSERT OR IGNORE INTO instruments(symbol,asset_class,venue,venue_symbol,"
            "session_calendar,day_boundary,display_digits,factor_exposures,"
            "news_entities,enabled) VALUES(?,?,?,?,?,?,?,?,?,1)",
            (sym, meta["asset_class"], "mt5", sym, meta["session_calendar"],
             meta["day_boundary"], meta["display_digits"],
             json.dumps(meta["factor_exposures"]),
             json.dumps(meta["news_entities"])))
        n += 1
    invalidate_cache()
    return n


def invalidate_cache() -> None:
    with _lock:
        _cache.clear()
        _cache_t["loaded"] = False


def _load() -> None:
    with _lock:
        if _cache_t["loaded"]:
            return
        for r in storage.query("SELECT * FROM instruments"):
            for k in ("factor_exposures", "news_entities"):
                try:
                    r[k] = json.loads(r[k]) if r.get(k) else {}
                except (json.JSONDecodeError, TypeError):
                    r[k] = {}
            _cache[r["symbol"]] = r
        _cache_t["loaded"] = True


def get(symbol: str) -> Dict[str, Any]:
    """Registry row for the symbol; falls back to classify() (and tags the
    result inferred=True) so callers always get usable metadata."""
    _load()
    row = _cache.get(symbol.upper())
    if row:
        return row
    meta = classify(symbol)
    meta.update({"symbol": symbol.upper(), "inferred": True, "venue": "mt5"})
    return meta


def asset_class(symbol: str) -> str:
    return get(symbol)["asset_class"]


def factor_exposures(symbol: str, side: str) -> Dict[str, float]:
    """Signed factor weights of an open position (buy as declared, sell negated)."""
    fx = get(symbol).get("factor_exposures") or {}
    sign = 1.0 if side == "buy" else -1.0
    return {k: sign * float(v) for k, v in fx.items()}


def news_entities(symbol: str) -> List[str]:
    ents = get(symbol).get("news_entities") or []
    return [str(e) for e in ents]


def all_rows() -> List[Dict[str, Any]]:
    _load()
    return list(_cache.values())


def upsert(symbol: str, fields: Dict[str, Any]) -> None:
    """Dashboard-facing edit; JSON-encodes dict/list fields."""
    allowed = {"asset_class", "venue", "venue_symbol", "session_calendar",
               "day_boundary", "display_digits", "factor_exposures",
               "news_entities", "volume_min", "volume_step", "volume_max",
               "enabled"}
    row = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
           for k, v in fields.items() if k in allowed}
    if not row:
        return
    existing = storage.query_one("SELECT symbol FROM instruments WHERE symbol=?",
                                 (symbol.upper(),))
    if existing:
        sets = ",".join("%s=?" % k for k in row)
        storage.execute("UPDATE instruments SET %s WHERE symbol=?" % sets,
                        tuple(row.values()) + (symbol.upper(),))
    else:
        meta = classify(symbol)
        base = {"symbol": symbol.upper(), "asset_class": meta["asset_class"],
                "session_calendar": meta["session_calendar"],
                "day_boundary": meta["day_boundary"],
                "display_digits": meta["display_digits"],
                "factor_exposures": json.dumps(meta["factor_exposures"]),
                "news_entities": json.dumps(meta["news_entities"])}
        base.update(row)
        cols = ",".join(base)
        ph = ",".join("?" * len(base))
        storage.execute("INSERT INTO instruments(%s) VALUES(%s)" % (cols, ph),
                        tuple(base.values()))
    invalidate_cache()
