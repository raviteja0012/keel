"""Position reconciliation — the engine only believes what the venue confirms.

The local `trades` table is a belief. The venue is the fact. When the two drift
apart the engine keeps sizing, counting concurrency and running kill switches
against a fiction, and nothing notices. This module compares them and says so.

It classifies and reports. It never heals. Closing a LOCAL_ONLY row, re-opening
a VENUE_ONLY one or re-sending a stop would make this a second execution path
running beside engine.py, which is the one thing the architecture forbids
(ARCHITECTURE-V2 §5.5: "never auto-heal by placing orders"). The only lever
offered here is a boolean the engine gates NEW ENTRIES on — halting is a
reduction in risk, healing is an increase — and the only write is an annotation
(`trades.recon_state`) that no rail reads.

Fail closed throughout. A venue that cannot be reached is UNKNOWN, and UNKNOWN
halts entries: "the venue did not answer" and "the venue confirms we are flat"
are the same silence over the wire, and only one of them is safe to trade on.

Two deliberate non-halts, because a rail that fires forever is a rail the
operator switches off:

  simulated modes   paper and shadow positions are simulated inside engine.py
                    and were never sent anywhere. A venue that has never heard
                    of them is not drift.

  read-only venues  a venue the engine cannot trade is a window, not an
                    execution path. Positions the operator holds there by hand
                    are foreign, reported, and not orphans.
"""
import time
from typing import Any, Dict, List, Optional, Tuple

import instruments
import storage
import venues

# Discrepancy classes. UNKNOWN is not "nothing found" — it is "we could not
# look", which is the distinction this whole module exists to preserve.
CLEAN = "CLEAN"
LOCAL_ONLY = "LOCAL_ONLY"          # we think we hold it, the venue does not
VENUE_ONLY = "VENUE_ONLY"          # the venue holds something we never opened
QTY_MISMATCH = "QTY_MISMATCH"
STOP_MISMATCH = "STOP_MISMATCH"
UNKNOWN = "UNKNOWN"                # unreachable venue / no position source

# Modes whose trades exist at a venue at all.
VENUE_BACKED_MODES = ("live",)

# recon_state values written onto trade rows (ARCHITECTURE-V2 §2.1 replaces the
# rejected status='reconciling' idea with this orthogonal column). 'orphan' has
# no row to live on — that is what makes it an orphan — so it stays report-only.
_STATE_BY_CLASS = {
    CLEAN: "clean",
    LOCAL_ONLY: "unconfirmed",
    QTY_MISMATCH: "qty_mismatch",
    STOP_MISMATCH: "stop_mismatch",
    UNKNOWN: "unknown",
}

# Which classification wins when one trade carries several. Ordered by how
# little we know: a row whose stop drifted is still a row we can see, a row we
# could not look up at all is not.
_SEVERITY = {CLEAN: 0, STOP_MISMATCH: 1, QTY_MISMATCH: 2,
             LOCAL_ONLY: 3, UNKNOWN: 4}

_SIDE_ALIASES = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}

# The same instrument is spelled four ways by quote currency. Unmapped,
# BTC/USDT against a local BTCUSD reads as LOCAL_ONLY plus VENUE_ONLY on every
# single cycle, and a permanent halt is a halt somebody turns off.
_QUOTE_ALIASES = ("USDT", "USDC", "BUSD", "USDD", "TUSD")

_VENUE_STOP_KEYS = ("sl", "stop_loss", "stop")

_schema_ready = {"done": False}


# ------------------------------------------------------------- normalisation
def _norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").upper().split(":")[0]        # ccxt "BTC/USDT:USDT"
    s = "".join(ch for ch in s if ch.isalnum())
    for q in _QUOTE_ALIASES:
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)] + "USD"
    return s


def _aliases(symbol: Any) -> set:
    """Every spelling one local trade may legitimately wear at a venue."""
    out = {_norm_symbol(symbol)}
    try:
        out.add(_norm_symbol(instruments.get(str(symbol)).get("venue_symbol")))
    except Exception:
        pass
    return {a for a in out if a}


def _side(value: Any) -> str:
    """Unrecognised side maps to "", which matches nothing — so a position we
    cannot read the direction of stays unmatched and is reported, rather than
    quietly pairing with a trade pointing the other way."""
    return _SIDE_ALIASES.get(str(value or "").strip().lower(), "")


def _signed(qty: Any, side: Any) -> float:
    """Quantity carrying its direction. Comparing signed sizes means a pair
    matched on venue id whose direction has flipped surfaces as a size
    discrepancy instead of passing as a match."""
    sd = _side(side)
    sign = 1.0 if sd == "buy" else (-1.0 if sd == "sell" else 0.0)
    return sign * abs(_f(qty))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tick_for(symbol: str, pos: Optional[Dict[str, Any]] = None) -> float:
    """One tick for the stop comparison. The venue's own increment wins; the
    instrument registry's display precision is the fallback."""
    for key in ("tick_size", "point"):
        v = _f((pos or {}).get(key))
        if v > 0:
            return v
    try:
        digits = instruments.get(symbol).get("display_digits")
        if digits is not None:
            return 10.0 ** -int(digits)
    except Exception:
        pass
    return 0.0


def _expected_qty(tr: Dict[str, Any]) -> float:
    """What the venue should be holding for this row.

    After TP1 the engine banks half at the venue but leaves `trades.lots` at the
    full entry size (engine.py mirrors the remainder implicitly). Comparing the
    row as written would flag QTY_MISMATCH on every winner that reaches TP1 —
    a permanent halt earned by the system working correctly.
    """
    lots = _f(tr.get("lots"))
    if tr.get("tp1_done") and lots >= 0.02:            # 0.01 cannot be halved
        return round(lots - round(lots / 2.0, 2), 8)
    return lots


def _pos_qty(pos: Dict[str, Any]) -> float:
    """Size under any of the spellings a position source uses. Reading these
    liberally costs nothing; reading them strictly turns a caller's key name
    into a silent qty of 0 and a phantom QTY_MISMATCH on every position."""
    for key in ("qty", "lots", "volume"):
        if key in pos:
            return _f(pos.get(key))
    return 0.0


def _pos_id(pos: Dict[str, Any]) -> str:
    for key in ("venue_id", "ticket", "id"):
        v = pos.get(key)
        if v not in (None, "", 0):
            return str(v).strip()
    return ""


def _venue_stop(pos: Dict[str, Any]) -> Tuple[bool, float]:
    """(reported, value). A venue that does not carry the field at all is a
    different thing from a venue reporting no stop attached."""
    for key in _VENUE_STOP_KEYS:
        if key in pos:
            return True, _f(pos.get(key))
    return False, 0.0


# ------------------------------------------------------------- venue reading
def _read_venues() -> Tuple[List[Dict[str, Any]], List[str], str]:
    """(position rows, configured venue names, fatal read error).

    An exception out of the venue layer is not an empty book. It is blindness,
    and it is returned as such so it cannot be mistaken for "we are flat".
    """
    try:
        names = [str(v.get("name")) for v in venues.list_venues()]
    except Exception as e:
        return [], [], "venue registry unreadable: %s" % (str(e)[:200])
    try:
        rows = venues.positions_all()
    except Exception as e:
        return [], names, "venue read failed: %s" % (str(e)[:200])
    if not isinstance(rows, list):
        return [], names, "venue read returned %s" % type(rows).__name__
    return rows, names, ""


def _sources(groups: List[Tuple[List[Dict[str, Any]], List[str], str]]
             ) -> List[Dict[str, Any]]:
    """One status row per source we expected to hear from.

    A group is (rows, names, read_err). positions_all() reports a failed venue
    as a {"venue", "error"} row; a healthy venue holding nothing reports nothing
    at all, which is why the names are folded in rather than inferred from the
    rows. A read error blinds only its own group: a broken exchange must not
    make the MT5 drop copy look dead, or the reverse.
    """
    seen: Dict[str, Dict[str, Any]] = {}

    def slot(name: str) -> Dict[str, Any]:
        return seen.setdefault(name, {"venue": name, "ok": True,
                                      "positions": 0, "error": ""})

    for rows, names, read_err in groups:
        touched = set(str(n) for n in names)
        for r in rows:
            name = str(r.get("venue") or "?")
            touched.add(name)
            s = slot(name)
            if r.get("error"):
                s["ok"] = False
                s["error"] = str(r["error"])[:200]
            else:
                s["positions"] += 1
        if read_err and not touched:
            touched.add("*")                # we cannot even name what went dark
        for name in touched:
            s = slot(name)
            if read_err:
                s["ok"], s["error"] = False, read_err
    return sorted(seen.values(), key=lambda s: s["venue"])


def _armed_by_venue() -> Dict[str, bool]:
    """name -> may the engine trade it. Read-only venues cannot have produced a
    position of ours; anything else is presumed armed, including a venue that
    has vanished from the registry."""
    try:
        return {str(v.get("name")): not bool(v.get("read_only", True))
                for v in venues.list_venues()}
    except Exception:
        return {}


def _known_tickets() -> set:
    """Venue ids this engine has ever recorded. A match proves the position is
    ours no matter what the registry says about the venue."""
    try:
        rows = storage.query("SELECT DISTINCT ticket FROM trades "
                             "WHERE ticket IS NOT NULL AND ticket<>0")
    except Exception:
        return set()
    return {str(r["ticket"]) for r in rows}


def from_mt5_feed(open_positions: Any, magic: Optional[int] = None,
                  venue: str = "mt5") -> List[Dict[str, Any]]:
    """Adapt the EA drop copy (`engine.feed_state["open_positions"]`) to position
    rows this module understands.

    MT5 reaches the engine over the EA bridge rather than through venues.py, and
    it is the one source that reports the stop resting at the broker — which is
    what makes STOP_MISMATCH checkable at all today. `magic` marks everything
    the bot did not open as foreign, so the operator's own manual positions on
    the same account are reported and never halt entries (ARCHITECTURE-V2 §5.5).
    """
    out: List[Dict[str, Any]] = []
    for p in open_positions or []:
        if not isinstance(p, dict):
            continue
        ticket = p.get("ticket")
        row: Dict[str, Any] = {
            "venue": venue, "symbol": p.get("symbol"), "side": p.get("side"),
            "qty": _f(p.get("lots")), "entry_price": _f(p.get("entry")),
            "sl": _f(p.get("sl")), "unrealized_pnl": _f(p.get("unrealized_pnl")),
            "venue_id": "" if ticket is None else str(ticket),
        }
        if magic is not None and _f(p.get("magic"), -1.0) != float(magic):
            row["foreign"] = True
        if ticket is not None and _f(ticket) <= 0:
            row["corrupt_id"] = True
        out.append(row)
    return out


# ------------------------------------------------------------- the comparison
def reconcile(mode: str = "live", positions: Optional[List[Dict[str, Any]]] = None,
              venue_names: Optional[List[str]] = None,
              now: Optional[int] = None) -> Dict[str, Any]:
    """Compare storage.open_trades(mode) against the venues' own positions.

    `positions` ADDS a source the venue registry does not own — the MT5 EA drop
    copy in `engine.feed_state["open_positions"]`, via from_mt5_feed(), is the
    live example. It is merged with the registry read and never replaces it: a
    caller must not be able to blind the reconciler to a real venue by handing
    it a second one. `venue_names` declares sources that are reachable but
    holding nothing, which is otherwise indistinguishable from having no source.

    Returns a report. Places nothing, cancels nothing, writes nothing.
    """
    t = int(now if now is not None else time.time())
    venue_backed = mode in VENUE_BACKED_MODES
    local = storage.open_trades(mode) or []

    reg_rows, reg_names, read_err = _read_venues()
    extra = [r for r in (positions or []) if isinstance(r, dict)]
    sources = _sources([(reg_rows, reg_names, read_err),
                        (extra, [str(n) for n in (venue_names or [])], "")])
    rows = list(reg_rows) + extra
    held = [r for r in rows if not r.get("error")]
    blind = [s["venue"] for s in sources if not s["ok"]]

    clean: List[Dict[str, Any]] = []
    disc: List[Dict[str, Any]] = []
    unverified: List[Dict[str, Any]] = []

    loose = list(held)
    pending = list(local) if venue_backed else []

    # Identity is the venue's id, not a symbol string the counterparty may
    # respell or truncate (ARCHITECTURE-V2 §0), so that pass runs first.
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for tr in list(pending):
        tid = str(tr.get("ticket") or "").strip()
        if not tid or tid == "0":
            continue
        pos = next((p for p in loose if _pos_id(p) == tid), None)
        if pos is not None:
            loose.remove(pos)
            pending.remove(tr)
            pairs.append((tr, pos))
    for tr in list(pending):
        al, sd = _aliases(tr.get("symbol")), _side(tr.get("side"))
        pos = next((p for p in loose
                    if _norm_symbol(p.get("symbol")) in al
                    and _side(p.get("side")) == sd), None)
        if pos is not None:
            loose.remove(pos)
            pending.remove(tr)
            pairs.append((tr, pos))

    for tr, pos in pairs:
        found = _compare(tr, pos)
        if found:
            disc.extend(found)
        else:
            clean.append(_entry(CLEAN, tr, pos, material=False))
        if not _venue_stop(pos)[0]:
            # No stop field from this venue: our stop is unverifiable here, not
            # wrong. Surfaced so it is visible, not halted on — otherwise every
            # spot venue halts the engine permanently.
            unverified.append({"trade_id": tr.get("id"), "symbol": tr.get("symbol"),
                               "venue": pos.get("venue"), "local_sl": _f(tr.get("sl"))})

    # Local rows with nobody to confirm them. While ANY source is blind we
    # cannot say a position is gone — only that we did not see it — so the
    # whole remainder degrades to UNKNOWN rather than to LOCAL_ONLY.
    why_blind = ""
    if blind:
        why_blind = "venue unreachable: %s" % "; ".join(
            "%s (%s)" % (s["venue"], s["error"] or "no detail")
            for s in sources if not s["ok"])
    elif not sources:
        why_blind = "no position source configured"
    for tr in pending:
        if why_blind:
            disc.append(_entry(UNKNOWN, tr, None, detail=why_blind))
        else:
            disc.append(_entry(LOCAL_ONLY, tr, None,
                               detail="open locally, not held at any venue"))

    # A blind venue with nothing local to confirm still halts: an orphan may be
    # sitting behind the silence.
    if blind:
        for name in sorted(blind):
            s = next(s for s in sources if s["venue"] == name)
            disc.append({"class": UNKNOWN, "material": True, "venue": name,
                         "trade_id": None, "symbol": None, "side": None,
                         "venue_id": "", "detail": "venue unreachable: %s"
                         % (s["error"] or "no detail")})

    # A position id of zero or below is the 32-bit ticket wrap (defect D1): the
    # row exists but its identity is nonsense, so nothing keyed on it can be
    # trusted. Kept as its own finding rather than folded into the match, which
    # would hide it behind a symbol that still lines up.
    for pos in held:
        if pos.get("corrupt_id"):
            disc.append({"class": UNKNOWN, "material": True,
                         "venue": str(pos.get("venue") or "?"), "trade_id": None,
                         "symbol": pos.get("symbol"), "side": pos.get("side"),
                         "venue_id": _pos_id(pos),
                         "detail": "venue reported a non-positive position id"})

    if loose:
        armed, tickets = _armed_by_venue(), _known_tickets()
        for pos in loose:
            name = str(pos.get("venue") or "?")
            ours = _pos_id(pos) in tickets and bool(_pos_id(pos))
            # Foreign means positively known not to be ours: another magic on
            # the same account, or a venue the engine was never armed to trade.
            # Those the operator holds by hand, so they alert and never halt.
            foreign = bool(pos.get("foreign")) or not armed.get(name, True)
            detail = ("orphan carrying a venue id this engine recorded" if ours
                      else "foreign position — not an execution path of ours"
                      if foreign else "orphan at a trading-enabled venue")
            disc.append({"class": VENUE_ONLY, "material": bool(ours or not foreign),
                         "venue": name, "trade_id": None,
                         "symbol": pos.get("symbol"), "side": pos.get("side"),
                         "venue_id": _pos_id(pos),
                         "venue_qty": _pos_qty(pos), "detail": detail})

    counts: Dict[str, int] = {CLEAN: len(clean)}
    for d in disc:
        counts[d["class"]] = counts.get(d["class"], 0) + 1
    material = [d for d in disc if d.get("material")]

    return {"t": t, "mode": mode, "venue_backed": venue_backed,
            "sources": sources, "blind": blind,
            "local_open": len(local), "venue_positions": len(held),
            "clean": clean, "discrepancies": disc,
            "stops_unverified": unverified,
            "counts": counts, "material": len(material),
            "halt": bool(material),
            "reason": material[0]["detail"] if material else ""}


def _entry(cls: str, tr: Dict[str, Any], pos: Optional[Dict[str, Any]],
           detail: str = "", material: bool = True, **extra) -> Dict[str, Any]:
    row = {"class": cls, "material": material, "trade_id": tr.get("id"),
           "symbol": tr.get("symbol"), "side": tr.get("side"),
           "venue": (pos or {}).get("venue", ""),
           "venue_id": str((pos or {}).get("venue_id") or ""),
           "detail": detail}
    row.update(extra)
    return row


def _compare(tr: Dict[str, Any], pos: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Size and stop checks on a matched pair. Both may fire on one trade."""
    out = []
    want = _signed(_expected_qty(tr), tr.get("side"))
    got = _signed(_pos_qty(pos), pos.get("side"))
    if abs(want - got) > max(abs(want), abs(got), 1.0) * 1e-9:
        out.append(_entry(QTY_MISMATCH, tr, pos, local_qty=want, venue_qty=got,
                          detail="local %s %s vs venue %s %s"
                          % (tr.get("side"), abs(want), pos.get("side"), abs(got))))
    reported, venue_sl = _venue_stop(pos)
    local_sl = _f(tr.get("sl"))
    if reported:
        tick = _tick_for(str(tr.get("symbol") or ""), pos) or abs(local_sl) * 1e-9
        if abs(venue_sl - local_sl) > tick:
            detail = ("venue reports no stop attached" if venue_sl == 0.0
                      else "local sl %s vs venue %s (tick %s)"
                      % (local_sl, venue_sl, tick))
            out.append(_entry(STOP_MISMATCH, tr, pos, local_sl=local_sl,
                              venue_sl=venue_sl, tick=tick, detail=detail))
    return out


# --------------------------------------------------------------- the boolean
def should_halt_entries(report: Any) -> bool:
    """The one thing the engine gates on. True when any discrepancy is material.

    A report it cannot read is itself an uncertainty, so a malformed or missing
    report halts. There is no argument that makes this function return False by
    accident.
    """
    if not isinstance(report, dict):
        return True
    disc = report.get("discrepancies")
    if not isinstance(disc, list):
        return True
    return any(bool(d.get("material")) for d in disc
               if isinstance(d, dict))


def halt_reason(report: Any) -> str:
    """Human-readable why, for the skip row and the alert."""
    if not isinstance(report, dict) or not isinstance(report.get("discrepancies"), list):
        return "reconciliation report unreadable"
    mat = [d for d in report["discrepancies"]
           if isinstance(d, dict) and d.get("material")]
    if not mat:
        return ""
    head = mat[0]
    extra = " (+%d more)" % (len(mat) - 1) if len(mat) > 1 else ""
    return "%s %s: %s%s" % (head.get("class"), head.get("symbol") or
                            head.get("venue") or "-", head.get("detail"), extra)


def gate(mode: str = "live", **kw) -> Tuple[bool, str, Dict[str, Any]]:
    """(halt, reason, report) and it never raises. A reconciler that throws
    inside the engine loop must not be the reason entries keep flowing, so the
    exception path produces an UNKNOWN report like any other blindness."""
    try:
        rep = reconcile(mode, **kw)
    except Exception as e:
        rep = {"t": int(time.time()), "mode": mode, "venue_backed": True,
               "sources": [], "blind": ["*"], "local_open": 0,
               "venue_positions": 0, "clean": [], "stops_unverified": [],
               "discrepancies": [{"class": UNKNOWN, "material": True,
                                  "venue": "*", "trade_id": None,
                                  "symbol": None, "side": None, "venue_id": "",
                                  "detail": "reconcile failed: %s" % (str(e)[:200])}],
               "counts": {UNKNOWN: 1}, "material": 1, "halt": True,
               "reason": "reconcile failed"}
    return should_halt_entries(rep), halt_reason(rep), rep


# ------------------------------------------------------------- annotation
def _ensure_schema() -> None:
    """Additive `recon_state` column, storage.py migration style: idempotent,
    never drops, never rewrites."""
    if _schema_ready["done"]:
        return
    cols = [r["name"] for r in storage.query("PRAGMA table_info(trades)")]
    if "recon_state" not in cols:
        storage.execute("ALTER TABLE trades ADD COLUMN recon_state TEXT")
    _schema_ready["done"] = True


def apply_recon_state(report: Any) -> int:
    """Stamp the classification onto the trade rows and return how many changed.

    This writes ONE column. It never touches `status`: `storage.open_trades()`
    hardcodes WHERE status='open' and is the sole source for the kill switch,
    max_concurrent and the exposure ledger, so a live position parked anywhere
    else is an unmanaged, un-risk-budgeted position (ARCHITECTURE-V2 §2.1).
    Never raises — an annotation failure must not stop the engine.
    """
    if not isinstance(report, dict):
        return 0
    rows: List[Any] = []
    for key in ("clean", "discrepancies"):
        found = report.get(key)
        if isinstance(found, list):
            rows.extend(found)
    worst: Dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("trade_id") is None:
            continue
        cls = row.get("class")
        if cls not in _SEVERITY:
            continue
        tid = row["trade_id"]
        if tid not in worst or _SEVERITY[cls] > _SEVERITY[worst[tid]]:
            worst[tid] = cls
    wanted = {tid: _STATE_BY_CLASS[cls] for tid, cls in worst.items()}
    if not wanted:
        return 0
    n = 0
    try:
        _ensure_schema()
        for trade_id, state in wanted.items():
            cur = storage.query_one(
                "SELECT recon_state FROM trades WHERE id=?", (trade_id,))
            if cur is None or cur.get("recon_state") == state:
                continue
            storage.execute("UPDATE trades SET recon_state=? WHERE id=?",
                            (state, trade_id))
            n += 1
    except Exception as e:                  # annotation is never load-bearing
        print("reconcile annotate error:", e)
    return n
