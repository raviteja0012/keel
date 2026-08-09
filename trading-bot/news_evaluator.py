"""
News Sentiment Evaluator
========================
Scores financial news headlines for currency sentiment and determines
what SL management action (if any) each open position should receive.

No external NLP libraries required — uses keyword phrase scoring.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Currency → keyword mapping ───────────────────────────────────────────────
# Headlines mentioning these keywords are considered relevant to that currency.
CURRENCY_KEYWORDS: Dict[str, List[str]] = {
    "USD": [
        "dollar", "usd", "fed", "federal reserve", "fomc", "us economy",
        "american economy", "us gdp", "us cpi", "nonfarm payroll", "powell",
        "us inflation", "us employment", "us manufacturing", "us consumer",
        "us retail", "us pmi", "us housing", "us trade", "us debt",
        "president", "white house", "trump", "us tariff", "tariffs",
        "trade war", "treasury", "us sanctions", "congress", "government shutdown",
    ],
    "EUR": [
        "euro", "eur", "ecb", "european central bank", "eurozone",
        "eu economy", "lagarde", "germany", "france", "eu inflation",
        "european inflation", "eu gdp", "euro area", "european growth",
    ],
    "GBP": [
        "pound", "gbp", "sterling", "boe", "bank of england", "uk economy",
        "british", "bailey", "uk gdp", "uk cpi", "uk inflation", "uk employment",
        "uk retail", "uk manufacturing", "uk pmi",
    ],
    "JPY": [
        "yen", "jpy", "boj", "bank of japan", "japanese", "japan economy",
        "ueda", "japan gdp", "japan cpi", "japan inflation", "japanese growth",
    ],
    "AUD": [
        "aussie", "aud", "rba", "reserve bank of australia", "australia economy",
        "australian", "australia gdp", "australia cpi", "australia employment",
    ],
    "NZD": [
        "kiwi", "nzd", "rbnz", "new zealand", "nz economy", "nz gdp",
        "nz cpi", "nz employment",
    ],
    "CAD": [
        "loonie", "cad", "boc", "bank of canada", "canadian", "canada economy",
        "canada gdp", "canada cpi", "canada employment", "canada oil",
    ],
    "CHF": [
        "franc", "chf", "snb", "swiss national bank", "swiss", "switzerland",
        "swiss economy",
    ],
    "XAU": [
        "gold", "xau", "precious metal", "bullion", "gold price", "gold demand",
        "safe haven", "haven demand",
    ],
    "XAG": [
        "silver", "xag", "silver price", "silver demand", "precious metal",
        "safe haven",
    ],
    "OIL": [
        "oil", "crude", "wti", "brent", "opec", "petroleum", "oil price",
        "energy prices", "oil demand", "oil supply",
    ],
    "NAS": [
        "nasdaq", "tech stocks", "technology stocks", "ndx", "tech sector",
    ],
    "SPX": [
        "s&p", "sp500", "s&p 500", "us stocks", "wall street", "us equities",
    ],
    "DOW": [
        "dow", "dow jones", "us stocks", "wall street", "us equities",
        "industrial average",
    ],
    "DAX": [
        "dax", "german stocks", "european stocks", "frankfurt", "eu equities",
    ],
    "FTSE": [
        "ftse", "uk stocks", "london stocks", "british equities",
    ],
    "NIKKEI": [
        "nikkei", "japanese stocks", "tokyo stocks", "japan equities",
    ],
    # Crypto — the asset class the old lexicon didn't know existed. BTC terms
    # deliberately include broad crypto/regulation vocabulary since BTC sets
    # the regime for the class (playbook §9).
    "BTC": [
        "bitcoin", "btc", "crypto", "cryptocurrency", "crypto market",
        "digital asset", "bitcoin etf", "crypto etf", "halving",
        "sec crypto", "crypto regulation", "crypto ban", "stablecoin",
        "coinbase", "binance", "crypto exchange", "blockchain",
    ],
    "ETH": [
        "ethereum", "eth", "ether", "crypto", "cryptocurrency",
        "ethereum etf", "defi", "smart contract", "staking",
        "crypto regulation", "digital asset",
    ],
    "ADA": ["cardano", "ada", "crypto", "cryptocurrency", "altcoin"],
    "DOGE": ["dogecoin", "doge", "crypto", "cryptocurrency", "altcoin", "meme coin"],
    "LINK": ["chainlink", "link token", "crypto", "cryptocurrency", "defi", "altcoin"],
    "BCH": ["bitcoin cash", "bch", "crypto", "cryptocurrency", "altcoin"],
}

# ── Sentiment phrase scoring ─────────────────────────────────────────────────
# Positive = bullish, negative = bearish. Scale: ±1.0 (moderate) to ±2.0 (strong).
BULLISH_PHRASES: Dict[str, float] = {
    # Strong (±2.0)
    "rate hike": 2.0,
    "hawkish": 2.0,
    "beat expectations": 2.0,
    "beats expectations": 2.0,
    "stronger than expected": 2.0,
    "record high": 2.0,
    "aggressive tightening": 2.0,
    "above forecast": 2.0,
    "above expectations": 2.0,
    "better than expected": 2.0,
    "upside surprise": 2.0,
    "blowout": 2.0,
    "surge": 1.5,
    "surges": 1.5,
    "surged": 1.5,
    "jump": 1.5,
    "jumps": 1.5,
    "jumped": 1.5,
    "soar": 1.5,
    "soars": 1.5,
    "soared": 1.5,
    "rallies": 1.0,
    "climbs": 1.0,
    "advances": 1.0,
    # Moderate (±1.0)
    "rise": 1.0,
    "rises": 1.0,
    "rising": 1.0,
    "gain": 1.0,
    "gains": 1.0,
    "gaining": 1.0,
    "higher": 1.0,
    "positive": 1.0,
    "growth": 1.0,
    "expand": 1.0,
    "expanded": 1.0,
    "increase": 1.0,
    "increased": 1.0,
    "improved": 1.0,
    "improvement": 1.0,
    "strong": 1.0,
    "beat": 1.0,
    "optimistic": 1.0,
    "recovery": 1.0,
    "rally": 1.0,
    "rallied": 1.0,
    "climbed": 1.0,
    "advanced": 1.0,
    "strengthens": 1.0,
    "strengthened": 1.0,
    "tightening": 1.0,
    "upbeat": 1.0,
    "robust": 1.0,
    "accelerates": 1.0,
    "accelerated": 1.0,
}

BEARISH_PHRASES: Dict[str, float] = {
    # Strong (±2.0)
    "rate cut": 2.0,
    "dovish": 2.0,
    "miss expectations": 2.0,
    "misses expectations": 2.0,
    "weaker than expected": 2.0,
    "recession": 2.0,
    "aggressive easing": 2.0,
    "below forecast": 2.0,
    "below expectations": 2.0,
    "worse than expected": 2.0,
    "downside surprise": 2.0,
    "misses forecasts": 2.0,
    "missed forecasts": 2.0,
    "collapsed": 2.0,
    "plunge": 1.5,
    "plunges": 1.5,
    "plunged": 1.5,
    "slump": 1.5,
    "slumps": 1.5,
    "slumped": 1.5,
    "tumble": 1.5,
    "tumbles": 1.5,
    "tumbled": 1.5,
    "sinks": 1.5,
    "sank": 1.5,
    "slides": 1.0,
    "slid": 1.0,
    "collapse": 2.0,
    "collapses": 2.0,
    "selloff": 1.5,
    # Moderate (±1.0)
    "fall": 1.0,
    "falls": 1.0,
    "falling": 1.0,
    "drop": 1.0,
    "drops": 1.0,
    "dropping": 1.0,
    "decline": 1.0,
    "declines": 1.0,
    "declining": 1.0,
    "lower": 1.0,
    "negative": 1.0,
    "slow": 1.0,
    "slows": 1.0,
    "slowing": 1.0,
    "weak": 1.0,
    "weaker": 1.0,
    "miss": 1.0,
    "missed": 1.0,
    "pessimistic": 1.0,
    "retreated": 1.0,
    "weakens": 1.0,
    "weakened": 1.0,
    "contraction": 1.0,
    "contracting": 1.0,
    "downturn": 1.0,
    "easing": 1.0,
    "downbeat": 1.0,
    "disappointing": 1.0,
    "disappointed": 1.0,
    "decelerates": 1.0,
    "decelerated": 1.0,
    "concern": 0.5,
    "concerns": 0.5,
    "uncertainty": 0.5,
    "risk": 0.5,
}


@dataclass
class SentimentResult:
    currency: str
    score: float            # normalised −1.0 … +1.0; positive = bullish
    raw_score: float
    matched_phrases: List[str] = field(default_factory=list)
    headline_count: int = 0  # relevant headlines found


@dataclass
class TradeDecision:
    ticket: int
    symbol: str
    side: str               # "buy" | "sell"
    action: str             # "trail_sl" | "move_sl_be" | "hold"
    new_sl: Optional[float]
    reason: str
    score_base: float
    score_quote: float
    net_score: float        # positive = news favours the trade direction


# ── Helpers ──────────────────────────────────────────────────────────────────

# entity pair per instrument for sentiment scoring; consulted before the
# string heuristics so indices/crypto stop being sliced like FX pairs
# (US500 -> base "US5"/quote "00" was the legacy failure mode)
_NAMED_ENTITIES: Dict[str, Tuple[str, str]] = {
    "US30": ("DOW", "USD"), "NAS100": ("NAS", "USD"), "US100": ("NAS", "USD"),
    "SP500": ("SPX", "USD"), "US500": ("SPX", "USD"), "SPX500": ("SPX", "USD"),
    "GER40": ("DAX", "EUR"), "DE40": ("DAX", "EUR"),
    "UK100": ("FTSE", "GBP"), "JPN225": ("NIKKEI", "JPY"),
    "BTCUSD": ("BTC", "USD"), "ETHUSD": ("ETH", "USD"),
    "ADAUSD": ("ADA", "USD"), "DOGUSD": ("DOGE", "USD"),
    "LINKUSD": ("LINK", "USD"), "BCHUSD": ("BCH", "USD"),
}


def _parse_symbol(symbol: str) -> Tuple[str, str]:
    """Return (base_entity, quote_entity) for a trading symbol."""
    # Strip common broker suffixes: .r, _SB, m, etc.
    sym = re.sub(r'[._-].*$', '', symbol.upper()).strip()
    if sym in _NAMED_ENTITIES:
        return _NAMED_ENTITIES[sym]
    if len(sym) < 3:
        return sym, "USD"
    # Named commodities / indices
    if sym in ("XAUUSD", "GOLD"):
        return "XAU", "USD"
    if sym in ("XAGUSD", "SILVER"):
        return "XAG", "USD"
    if sym in ("USOIL", "UKOIL", "XTIUSD", "WTI", "BRENT"):
        return "OIL", "USD"
    if sym in ("NAS100", "NASDAQ", "NDX", "US100"):
        return "NAS", "USD"
    if sym in ("SPX500", "SP500", "US500", "SPX"):
        return "SPX", "USD"
    # Standard 6-char FX pair
    if len(sym) >= 6:
        return sym[:3], sym[3:6]
    return sym[:3], "USD"


def score_currency_sentiment(currency: str, headlines: List[str]) -> SentimentResult:
    """
    Compute a normalised sentiment score (−1 to +1) for a currency
    across a list of news headlines.
    """
    keywords = CURRENCY_KEYWORDS.get(currency, [currency.lower()])
    raw_score = 0.0
    matched: List[str] = []
    relevant_count = 0

    for headline in headlines:
        hl = headline.lower()
        # Only score if the headline mentions this currency
        positions = [hl.find(kw) for kw in keywords if kw in hl]
        if not positions:
            continue
        relevant_count += 1
        # Subject attribution: a currency named early in the headline is its
        # subject and gets full credit for the sentiment verbs; a late/context
        # mention ("...as president announces tariffs") gets reduced weight.
        # Prevents "Silver soars on tariff news" from scoring bullish-USD too.
        first = min(positions)
        w_subj = 1.0 if first <= max(12, 0.35 * len(hl)) else 0.25
        # whole-word matching so "rise" can't double-count inside "rises"
        for phrase, weight in BULLISH_PHRASES.items():
            if re.search(r"\b%s\b" % re.escape(phrase), hl):
                raw_score += weight * w_subj
                matched.append(f"+{phrase}({weight * w_subj:.1f})")
        for phrase, weight in BEARISH_PHRASES.items():
            if re.search(r"\b%s\b" % re.escape(phrase), hl):
                raw_score -= weight * w_subj
                matched.append(f"-{phrase}({weight * w_subj:.1f})")

    # Clamp to [−4, +4] then normalise to [−1, +1]
    clamped = max(-4.0, min(4.0, raw_score))
    normalised = clamped / 4.0

    return SentimentResult(
        currency=currency,
        score=normalised,
        raw_score=raw_score,
        matched_phrases=matched[:12],
        headline_count=relevant_count,
    )


def calculate_trail_sl(position: dict, trail_factor: float = 1.5) -> Optional[float]:
    """
    Calculate new trailing stop-loss price.

    Uses the original risk distance (|entry − sl|) multiplied by trail_factor
    as the buffer behind current price.  Only trails in the profitable direction
    — never widens the stop.

    Returns None if conditions are not met (price not yet profitable,
    no SL set, or trail would move SL in the wrong direction).
    """
    try:
        entry = float(position["entry"])
        current = float(position["current"])
        sl = float(position.get("sl", 0))
        side = position.get("side", "buy").lower()
    except (KeyError, ValueError, TypeError):
        return None

    if entry == 0 or current == 0:
        return None

    if side == "buy":
        if sl == 0:
            return None
        original_risk = entry - sl
        if original_risk <= 0:
            return None          # SL already above entry — unusual, skip
        if current <= entry:
            return None          # Not yet in profit
        trail_dist = original_risk * trail_factor
        new_sl = round(current - trail_dist, 5)
        return new_sl if new_sl > sl else None   # only move up

    elif side == "sell":
        if sl == 0:
            return None
        original_risk = sl - entry
        if original_risk <= 0:
            return None
        if current >= entry:
            return None          # Not yet in profit
        trail_dist = original_risk * trail_factor
        new_sl = round(current + trail_dist, 5)
        return new_sl if new_sl < sl else None   # only move down

    return None


def calculate_be_sl(position: dict, be_buffer_pips: float = 2.0) -> Optional[float]:
    """
    Calculate break-even stop-loss (entry + small buffer to cover spread).

    Returns None if the trade is not yet in profit, or if the SL is
    already at or past break-even.
    """
    try:
        entry = float(position["entry"])
        current = float(position["current"])
        sl = float(position.get("sl", 0))
        side = position.get("side", "buy").lower()
        symbol = position.get("symbol", "EURUSD")
    except (KeyError, ValueError, TypeError):
        return None

    if entry == 0:
        return None

    # Break-even buffer unit by symbol/asset class. The old table was
    # forex-only: a 2-"pip" buffer on BTCUSD was 0.0002 USD (meaningless).
    # Absolute per-class units, scaled by be_buffer_pips as a multiplier:
    sym = symbol.upper()
    if sym.startswith(("BTC", "ETH")):
        pip = 10.0                       # majors quote in thousands of USD
    elif sym.startswith(("ADA", "DOG", "LINK", "BCH", "SOL", "XRP", "LTC")):
        pip = 0.05
    elif sym in ("US30", "NAS100", "US100", "SP500", "US500", "SPX500",
                 "GER40", "DE40", "UK100", "JPN225"):
        pip = 1.0                        # one index point
    elif "JPY" in sym:
        pip = 0.01
    elif any(x in sym for x in ("XAU", "GOLD")):
        pip = 0.10
    elif any(x in sym for x in ("OIL", "WTI", "XTI")):
        pip = 0.01
    else:
        pip = 0.0001

    buffer = be_buffer_pips * pip

    if side == "buy":
        if current <= entry:
            return None          # Not in profit
        be_sl = round(entry + buffer, 5)
        if sl > 0 and sl >= be_sl:
            return None          # SL already at or above BE
        return be_sl

    elif side == "sell":
        if current >= entry:
            return None
        be_sl = round(entry - buffer, 5)
        if sl > 0 and sl <= be_sl:
            return None
        return be_sl

    return None


def evaluate_trade(
    position: dict,
    headlines: List[str],
    sentiment_threshold: float = 0.25,
    trail_factor: float = 1.5,
    be_buffer_pips: float = 2.0,
    close_loss_threshold: float = 0.5,
) -> TradeDecision:
    """
    Evaluate one open position against current news and return an action.

    Decision logic:
      net_score ≥ +threshold        →  trail_sl    (news favours the trade)
      net_score ≤ −threshold        →  move_sl_be  (news against, trade in profit)
      net_score ≤ −close_threshold
        and trade is in LOSS        →  close_trade (BE impossible — cut the loss)
      otherwise                     →  hold
    """
    ticket = int(position.get("ticket", 0))
    symbol = position.get("symbol", "")
    side = position.get("side", "buy").lower()

    base, quote = _parse_symbol(symbol)

    s_base = score_currency_sentiment(base, headlines)
    s_quote = score_currency_sentiment(quote, headlines)

    # Net score from trade's perspective:
    #   BUY  = long base, short quote → bullish base or bearish quote = good
    #   SELL = short base, long quote  → bearish base or bullish quote = good
    if side == "buy":
        net = s_base.score - s_quote.score
    else:
        net = s_quote.score - s_base.score

    log.debug(
        "[%s %s %s] base=%s(%.2f) quote=%s(%.2f) net=%.2f",
        ticket, symbol, side, base, s_base.score, quote, s_quote.score, net,
    )

    if net >= sentiment_threshold:
        new_sl = calculate_trail_sl(position, trail_factor)
        if new_sl is not None:
            reason = (
                f"Favorable news for {side.upper()} {symbol}: "
                f"{base}={s_base.score:+.2f} {quote}={s_quote.score:+.2f} net={net:+.2f}. "
                f"Phrases: {'; '.join((s_base.matched_phrases + s_quote.matched_phrases)[:6])}"
            )
            return TradeDecision(ticket, symbol, side, "trail_sl", new_sl, reason,
                                 s_base.score, s_quote.score, net)
        else:
            reason = (
                f"Favorable news (net={net:+.2f}) but trade not yet in profit "
                f"or SL already trailing. Holding."
            )
            return TradeDecision(ticket, symbol, side, "hold", None, reason,
                                 s_base.score, s_quote.score, net)

    elif net <= -sentiment_threshold:
        new_sl = calculate_be_sl(position, be_buffer_pips)
        if new_sl is not None:
            reason = (
                f"Adverse news for {side.upper()} {symbol}: "
                f"{base}={s_base.score:+.2f} {quote}={s_quote.score:+.2f} net={net:+.2f}. "
                f"Moving SL to break-even. "
                f"Phrases: {'; '.join((s_base.matched_phrases + s_quote.matched_phrases)[:6])}"
            )
            return TradeDecision(ticket, symbol, side, "move_sl_be", new_sl, reason,
                                 s_base.score, s_quote.score, net)
        # BE impossible -> is the trade underwater? Strongly adverse news on a
        # losing trade = cut it at market rather than ride the news move.
        try:
            entry = float(position["entry"])
            current = float(position["current"])
        except (KeyError, ValueError, TypeError):
            entry = current = 0.0
        in_loss = (current < entry) if side == "buy" else (current > entry)
        if in_loss and entry and net <= -close_loss_threshold:
            reason = (
                f"Strongly adverse news for {side.upper()} {symbol} while in loss: "
                f"{base}={s_base.score:+.2f} {quote}={s_quote.score:+.2f} net={net:+.2f} "
                f"(close threshold ±{close_loss_threshold:.2f}). Cutting the trade at market. "
                f"Phrases: {'; '.join((s_base.matched_phrases + s_quote.matched_phrases)[:6])}"
            )
            return TradeDecision(ticket, symbol, side, "close_trade", None, reason,
                                 s_base.score, s_quote.score, net)
        reason = (
            f"Adverse news (net={net:+.2f}) but trade "
            f"{'in loss yet below close threshold' if in_loss else 'not in profit or SL already at/past BE'}. "
            f"Holding."
        )
        return TradeDecision(ticket, symbol, side, "hold", None, reason,
                             s_base.score, s_quote.score, net)

    else:
        reason = (
            f"Neutral news for {symbol} "
            f"({base}={s_base.score:+.2f} {quote}={s_quote.score:+.2f} net={net:+.2f}, "
            f"threshold=±{sentiment_threshold:.2f}). No action."
        )
        return TradeDecision(ticket, symbol, side, "hold", None, reason,
                             s_base.score, s_quote.score, net)
