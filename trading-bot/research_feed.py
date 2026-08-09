"""Research-newsletter ingestion.

Paid research the owner subscribes to (Finimize, The Better Traders) is delivered
by email. That is the whole integration: read your own mailbox. There is nothing
to scrape, no login to automate, no page layout to reverse-engineer, and nothing
that breaks when a vendor redesigns their site or tightens their terms. A mail
parser is also honest about what it is: the content was sent to you.

Two things come out of a brief, and only one of them is safe to trade on:

  MARKET SNAPSHOT   index levels, gold, crude, yields, bitcoin. Numeric, dated,
                    checkable. Useful as cross-asset regime context.
  NARRATIVE         prose about what the Fed might do. Interesting to a human,
                    and a trap for a bot: it is stale by publication, it is not
                    falsifiable, and back-testing it is impossible.

So this module extracts both and marks them differently. Narrative is stored for
the operator to read in the dashboard and is NEVER exposed as a trading signal.
Nothing here places an order or touches a rail; the engine may read the snapshot
as context, the same way it reads the news calendar.

Transport is deliberately pluggable. parse_email() takes raw HTML and needs no
credentials, so it is fully testable offline; fetching is a separate concern.
"""
import html
import json
import re
import time
from typing import Any, Dict, List, Optional

import storage

# Senders we know how to read. An unknown sender is stored raw rather than
# guessed at: a mis-parsed number is worse than no number.
KNOWN_SOURCES = {
    "finimize.com": "finimize",
    "thebettertraders.com": "thebettertraders",
}

# Instrument labels as the briefs write them, mapped to Keel's symbols where a
# mapping is unambiguous. Deliberately conservative: an index level is context,
# not a price feed, and must never be mistaken for one.
_INSTRUMENTS = {
    "S&P 500": ("SPX", "indices"),
    "NASDAQ": ("NDX", "indices"),
    "FTSE 100": ("UKX", "indices"),
    "GOLD": ("XAUUSD", "metals"),
    "BRENT OIL": ("BRENT", "energies"),
    "CRUDE OIL": ("WTI", "energies"),
    "BITCOIN": ("BTCUSD", "crypto"),
    "US BONDS": ("US10Y", "rates"),
    "VIX": ("VIX", "volatility"),
}

# At least one DIGIT. "[\d,]+" alone matches a bare comma, which then reaches
# float("") and raises. Thousands separators are allowed, but not on their own.
_NUM = r"[-+]?\d[\d,]*(?:\.\d+)?"


def _text_from_html(raw: str) -> List[str]:
    raw = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>|</td>", "\n", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    drop = re.compile(r"(?i)unsubscribe|view in browser|privacy policy|utm_|"
                      r"©|all rights reserved|update your preferences|app store")
    out, seen = [], set()
    for ln in (l.strip() for l in text.split("\n")):
        if not ln or len(ln) < 3 or drop.search(ln) or ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return out


def _parse_snapshot(lines: List[str]) -> List[Dict[str, Any]]:
    """Pull the market table. The briefs lay it out as label / change / level on
    consecutive lines, so read a small window after each known label rather than
    trying to match one big regex against a layout that will change."""
    snap = []
    for i, ln in enumerate(lines):
        label = next((k for k in _INSTRUMENTS if ln.strip().upper().startswith(k)), None)
        if not label:
            continue
        # Start AFTER the label line. "S&P 500", "FTSE 100" and the footnote
        # "*10-year government yield" all carry digits that are not the level,
        # and a max-of-window heuristic happily picks them: US BONDS parsed as
        # 10.0 instead of the 4.65% yield.
        window = " ".join(lines[i + 1:i + 4])
        pct = re.search(r"(%s)\s*%%\s*([▲▼])" % _NUM, window)
        levels = re.findall(r"\$?\s*(%s)" % _NUM, window)
        if not pct and not levels:
            continue
        symbol, cls = _INSTRUMENTS[label]
        change = None
        if pct:
            change = float(pct.group(1).replace(",", ""))
            if pct.group(2) == "▼":
                change = -change
        # The briefs print label, then change, then level. So the level is the
        # FIRST number after the change, not the biggest one anywhere near it.
        cands = [float(x.replace(",", "")) for x in levels]
        if change is not None:
            cands = [c for c in cands if abs(c - abs(change)) > 1e-9]
        snap.append({"label": label, "symbol": symbol, "asset_class": cls,
                     "change_pct": change,
                     "level": cands[0] if cands else None})
    return snap


def parse_email(sender: str, subject: str, html_body: str,
                received_t: Optional[int] = None) -> Dict[str, Any]:
    """Pure function: raw email in, structured note out. No I/O, no credentials,
    so the parser is testable without a mailbox."""
    source = next((v for k, v in KNOWN_SOURCES.items() if k in (sender or "").lower()),
                  "unknown")
    lines = _text_from_html(html_body or "")
    snapshot = _parse_snapshot(lines)
    # Everything that is not the numbers table is narrative.
    narrative = [l for l in lines if len(l) > 60][:40]
    return {
        "source": source,
        "sender": sender,
        "subject": subject,
        "received_t": int(received_t or time.time()),
        "snapshot": snapshot,
        "narrative": narrative,
        "line_count": len(lines),
    }


# --------------------------------------------------------------- persistence
_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER,                     -- when the brief was received
    source TEXT,                   -- finimize | thebettertraders | unknown
    subject TEXT,
    snapshot TEXT,                 -- JSON: the numeric market table
    narrative TEXT,                -- JSON: prose, for a HUMAN to read
    dedupe_key TEXT UNIQUE         -- source + subject + day
);
CREATE INDEX IF NOT EXISTS idx_research_notes_t ON research_notes(t);
"""


def init() -> None:
    """Additive, idempotent, same pattern as storage.init()."""
    for stmt in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
        storage.execute(stmt)


def record(note: Dict[str, Any]) -> int:
    """Store a parsed brief. Same brief twice is a no-op: newsletters get resent,
    and a duplicate must not double-count in any downstream view."""
    init()
    day = time.strftime("%Y-%m-%d", time.gmtime(note["received_t"]))
    key = "%s|%s|%s" % (note["source"], (note["subject"] or "")[:120], day)
    try:
        return storage.execute(
            "INSERT OR IGNORE INTO research_notes"
            "(t,source,subject,snapshot,narrative,dedupe_key) VALUES(?,?,?,?,?,?)",
            (note["received_t"], note["source"], note["subject"],
             json.dumps(note["snapshot"]), json.dumps(note["narrative"]), key))
    except Exception as e:                     # never break a caller over research
        print("[research_feed] record failed:", e)
        return 0


def latest_snapshot(max_age_h: int = 36) -> Dict[str, Any]:
    """Most recent market snapshot, or empty if the newest brief is stale.

    Stale means absent, not 'use the old one'. A three-day-old index level
    presented as current context is exactly the kind of quiet wrongness the
    integrity checks exist to catch."""
    init()
    row = storage.query_one(
        "SELECT t, source, snapshot FROM research_notes "
        "WHERE snapshot NOT IN ('[]','') ORDER BY t DESC LIMIT 1")
    if not row:
        return {"stale": True, "reason": "no research notes recorded", "rows": []}
    age_h = (time.time() - (row["t"] or 0)) / 3600.0
    if age_h > max_age_h:
        return {"stale": True, "age_h": round(age_h, 1),
                "reason": "newest brief is %.0fh old" % age_h, "rows": []}
    try:
        rows = json.loads(row["snapshot"])
    except (ValueError, TypeError):
        return {"stale": True, "reason": "unreadable snapshot", "rows": []}
    return {"stale": False, "age_h": round(age_h, 1), "source": row["source"],
            "t": row["t"], "rows": rows}


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    init()
    return storage.query(
        "SELECT id,t,source,subject FROM research_notes ORDER BY t DESC LIMIT ?",
        (limit,))
