"""Alert taxonomy — four tiers, one table, one relay (ARCHITECTURE-V2 §7).

Today every rail rejection is pushed to Telegram AND Discord at the same
visual weight as TRADE OPENED. Eight pairs x two speeds is roughly 32
messages an hour, bursting hardest exactly when something is wrong; the
channel gets muted and the mute is still on when the one message that
matters arrives. There is no action a skip permits — that is the rail
working — so a skip is P4, logged and never pushed.

The tiers are §7's four, not a three-level simplification. P1 and P2 differ
in *delivery*, not in importance: P1 bypasses quiet hours and is never held
for a digest, P2 is one message with no repeat for 30 minutes. Collapsing
them either pages for a mode change or holds a reconciliation mismatch until
morning, and both of those are how a pager gets ignored.

  P1 PAGE      money or truth is wrong and a human must act now
  P2 ALERT     state changed in a way that changes what happens next
  P3 DIGEST    batched into one message at DIGEST_HOUR local
  P4 LOG ONLY  every rail rejection; queryable, never pushed

Two halves, deliberately split:

  raise_alert()  ANY process may call. Records only, never sends, and never
                 raises — a caller already handling a kill switch must not
                 also have to handle an alerting failure.
  relay_once()   exactly ONE process delivers, and it must hold the DB lease
                 to do it. `notifier._q` and `notifier._started` are
                 per-process module state, so three relays means three
                 Telegram messages per event: the alert-fatigue failure
                 arriving through its own fix. The lease enforces that at
                 the write layer rather than by call-site discipline.

That split is also the fix for the live_switch bug: `dashboard_api` never
calls `notifier.start()`, so the message announcing that real money is now
at risk was queued into a process with no worker and never left the box. It
now lands in `alerts` and the relay owner delivers it.

Fail closed, here, means: when in doubt the alert is still recorded and
still delivered. An unregistered kind is P2 rather than P4, a send that
throws leaves `delivered_t` NULL so the next pass retries it, and a
rate-limited burst collapses to one message with a count instead of being
dropped.
"""
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import storage

# ------------------------------------------------------------------ tiers
P1, P2, P3, P4 = "P1", "P2", "P3", "P4"
TIERS = (P1, P2, P3, P4)

# An unregistered kind is delivered, not swallowed. Silence is the failure
# mode this module exists to prevent, so the unknown case leans loud.
DEFAULT_TIER = P2

KINDS: Dict[str, str] = {
    # -- P1 PAGE: money or truth is wrong (§7 P1 table) -------------------
    "recon_drift": P1,             # VENUE_ONLY_TAGGED / QTY_MISMATCH / LOCAL_ONLY live
    "unconfirmed_submit": P1,      # an orders row stuck in state='unknown'
    "unconfirmed_stop": P1,        # requested != confirmed for 2+ cycles, live
    "fill_size_mismatch": P1,      # acked lots != requested lots (EA clamped)
    "feed_loss_live": P1,          # stale cycle/feed WITH open live positions
    "db_integrity_live": P1,       # storage.integrity_suspect() while live
    "clock_skew": P1,              # |local now - terminal.time_utc| > 30s
    "bad_ticket": P1,              # ticket <= 0 in the feed — the D1 canary
    "kill_switch_fired": P1,       # daily/weekly stop actually tripped

    # -- P2 ALERT: state changed, one message, no repeat for 30 min -------
    "kill_switch_armed": P2,
    "loss_governor": P2,
    "halt": P2,
    "trading_mode": P2,            # includes paper -> LIVE (the live_switch bug)
    "live_cells": P2,
    "strategy_stage": P2,
    "data_trust": P2,
    "news_calendar_stale": P2,
    "feed_stale": P2,              # no open live positions — degraded, not urgent
    "venue_unreachable": P2,
    "frozen_quote": P2,
    "engine_errors": P2,           # engine_consecutive_errors >= 3
    "corruption_recovery": P2,
    "command_expired": P2,         # queued command expired without an ack
    "queue_depth": P2,

    # -- P3 DIGEST: worth knowing by breakfast, never worth an interrupt --
    "agent_change_refused": P3,    # params_store already logged it; the count is the signal
    "promotion_progress": P3,
    "gate_ledger": P3,
    "shadow_outcome": P3,

    # -- P4 LOG ONLY: the rail working as designed ------------------------
    "rail_skip": P4,
    "no_setup": P4,
    "stand_aside": P4,
    "dedup_hit": P4,               # D20: not even logged today
}

# Dedupe window and per-kind hourly message ceiling, by tier. P2's 30 minutes
# is §7's number. P1 repeats sooner because an unresolved page must recur,
# and is capped per hour because eight symbols drifting at once is one
# incident, not eight pages. P3/P4 are zero here because the relay never
# sends them at all: P3 leaves through the digest, P4 leaves nowhere.
_DEDUPE_S = {P1: 900, P2: 1800, P3: 3600, P4: 1800}
_RATE_PER_H = {P1: 6, P2: 4, P3: 0, P4: 0}

DIGEST_HOUR = 7                     # local hour the P3 digest goes out (§7)
RELAY_LEASE_S = 120                 # a dead relay owner's lease expires this fast
_LEASE_KEY = "alerts_relay_owner"
_DIGEST_DAY_KEY = "alerts_last_digest_day"

# pid alone is not an identity: a forked worker inherits it. The uuid makes
# two claimants distinguishable even inside one interpreter.
_INSTANCE = "%d:%s" % (os.getpid(), uuid.uuid4().hex[:8])

_PREFIX = {P1: "🔴 <b>PAGE</b>", P2: "🟠 <b>ALERT</b>",
           P3: "🟡 <b>NOTE</b>", P4: "· log"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_t INTEGER NOT NULL,
    severity TEXT NOT NULL,          -- P1 | P2 | P3 | P4
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    dup_count INTEGER DEFAULT 0,     -- repeats folded into this row
    last_t INTEGER,                  -- most recent occurrence of dedupe_key
    delivered_t INTEGER,
    delivery TEXT,                   -- single | rollup | folded | digest | log
    acknowledged_t INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_pending ON alerts(delivered_t, created_t);
CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts(dedupe_key, created_t);
"""

# Additive column migrations, same shape and same idempotence as
# storage._MIGRATIONS. These matter if the `alerts` table lands in
# storage.SCHEMA with only the seven columns §7 names: the bookkeeping
# columns are then added in place rather than by rewriting the table.
_MIGRATIONS = [
    ("alerts", "dup_count", "INTEGER DEFAULT 0"),
    ("alerts", "last_t", "INTEGER"),
    ("alerts", "delivery", "TEXT"),
    ("alerts", "detail", "TEXT"),
]

_ready = False


def _ensure() -> None:
    global _ready
    if _ready:
        return
    for stmt in _SCHEMA.split(";"):
        if stmt.strip():
            storage.execute(stmt)
    cols = [r["name"] for r in storage.query("PRAGMA table_info(alerts)")]
    for table, col, decl in _MIGRATIONS:
        if col not in cols:
            storage.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    _ready = True


# ------------------------------------------------------------- taxonomy
def tier_for(kind: str) -> str:
    return KINDS.get(kind, DEFAULT_TIER)


def dedupe_window_s(kind: str, severity: Optional[str] = None) -> int:
    return _DEDUPE_S[severity if severity in TIERS else tier_for(kind)]


def rate_limit_per_hour(kind: str, severity: Optional[str] = None) -> int:
    return _RATE_PER_H[severity if severity in TIERS else tier_for(kind)]


# ---------------------------------------------------------------- record
def raise_alert(kind: str, message: str, severity: Optional[str] = None,
                dedupe_key: Optional[str] = None, detail: Any = None) -> int:
    """Record an alert and return its row id (0 if nothing was recorded).

    Any process may call this. It never sends and it NEVER raises: the
    callers are kill switches, reconciliation and the live switch, and an
    alerting system that crashes those is worse than no alerting.

    `dedupe_key` is a discriminator within the kind (a symbol, a ticket),
    not a global key — kinds can never collide with each other.
    """
    try:
        _ensure()
        sev = severity if severity in TIERS else tier_for(kind)
        key = "%s|%s" % (kind, dedupe_key) if dedupe_key else kind
        now = int(time.time())
        prev = storage.query_one(
            "SELECT id FROM alerts WHERE dedupe_key=? AND created_t>=? "
            "ORDER BY id DESC LIMIT 1",
            (key, now - dedupe_window_s(kind, sev)))
        if prev:
            # A flapping condition is one alert plus a count, not 200 rows.
            storage.execute(
                "UPDATE alerts SET dup_count=dup_count+1, last_t=? WHERE id=?",
                (now, prev["id"]))
            return int(prev["id"])
        return storage.execute(
            "INSERT INTO alerts(created_t,severity,kind,message,dedupe_key,"
            "dup_count,last_t,delivered_t,delivery,detail) "
            "VALUES(?,?,?,?,?,0,?,?,?,?)",
            (now, sev, kind, str(message)[:2000], key, now,
             now if sev == P4 else None,          # P4's delivery IS the log row
             "log" if sev == P4 else None,
             json.dumps(detail, default=str) if detail is not None else None))
    except Exception as e:
        print("[alerts] raise error:", e)
        return 0


def acknowledge(alert_id: int) -> bool:
    try:
        _ensure()
        storage.execute("UPDATE alerts SET acknowledged_t=? WHERE id=? "
                        "AND acknowledged_t IS NULL", (int(time.time()), alert_id))
        row = storage.query_one("SELECT acknowledged_t FROM alerts WHERE id=?",
                                (alert_id,))
        return bool(row and row["acknowledged_t"])
    except Exception as e:
        print("[alerts] ack error:", e)
        return False


# ----------------------------------------------------------- relay lease
def own_relay() -> bool:
    """Claim or renew the single relay lease; True if this process holds it.

    Compare-and-set on the stored value, so two processes racing from cold
    cannot both win: SQLite serializes the UPDATEs and the loser's WHERE no
    longer matches. An owner that dies stops renewing and the lease expires
    after RELAY_LEASE_S.
    """
    try:
        _ensure()
        now = time.time()
        mine = json.dumps({"instance": _INSTANCE, "t": now})
        row = storage.query_one("SELECT value FROM settings WHERE key=?", (_LEASE_KEY,))
        if row is None:
            storage.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                            (_LEASE_KEY, mine))
        else:
            held = _lease_holder(row["value"])
            if (held["instance"] and held["instance"] != _INSTANCE
                    and now - held["t"] < RELAY_LEASE_S):
                return False
            storage.execute("UPDATE settings SET value=? WHERE key=? AND value=?",
                            (mine, _LEASE_KEY, row["value"]))
        after = storage.query_one("SELECT value FROM settings WHERE key=?", (_LEASE_KEY,))
        return bool(after) and _lease_holder(after["value"])["instance"] == _INSTANCE
    except Exception as e:
        print("[alerts] relay claim error:", e)
        return False


def _lease_holder(raw: Any) -> Dict[str, Any]:
    try:
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return {"instance": str(d.get("instance", "")), "t": float(d.get("t", 0))}
    except Exception:
        return {"instance": "", "t": 0.0}


def relay_owner() -> Dict[str, Any]:
    row = storage.query_one("SELECT value FROM settings WHERE key=?", (_LEASE_KEY,))
    held = _lease_holder(row["value"]) if row else {"instance": "", "t": 0.0}
    held["is_me"] = held["instance"] == _INSTANCE
    held["fresh"] = (time.time() - held["t"]) < RELAY_LEASE_S
    return held


# --------------------------------------------------------------- deliver
def _notifier_send(msg: str) -> None:
    import notifier                  # imported late: only the relay owner needs it
    notifier.send(msg)


def _send_one(send, text: str) -> bool:
    """True if the relay accepted the message. A broken relay is a deferral,
    never a loss: the row keeps delivered_t NULL and the next pass retries."""
    try:
        send(text)
        return True
    except Exception as e:
        print("[alerts] send failed (will retry):", e)
        return False


def _occurrences(r: Dict[str, Any]) -> int:
    return 1 + int(r["dup_count"] or 0)


def _format(r: Dict[str, Any]) -> str:
    text = "%s <b>%s</b>\n%s" % (_PREFIX.get(r["severity"], ""), r["kind"], r["message"])
    dup = int(r["dup_count"] or 0)
    if dup:
        text += "\n<i>+%d more in the last %d min</i>" % (
            dup, dedupe_window_s(r["kind"], r["severity"]) // 60)
    return text


def _format_rollup(kind: str, rows: List[Dict[str, Any]]) -> str:
    total = sum(_occurrences(r) for r in rows)
    return ("%s <b>%s</b> x%d\n%s\n<i>%d events rolled into one message "
            "(rate limit %d/h) — full list in the alerts table</i>"
            % (_PREFIX.get(rows[0]["severity"], ""), kind, total,
               rows[-1]["message"], total,
               rate_limit_per_hour(kind, rows[0]["severity"])))


def _mark(alert_id: int, now: int, how: str) -> None:
    storage.execute("UPDATE alerts SET delivered_t=?, delivery=? WHERE id=?",
                    (now, how, alert_id))


def _messages_since(kind: str, since: int) -> int:
    """Messages actually sent for this kind — folded rows do not count."""
    row = storage.query_one(
        "SELECT COUNT(*) n FROM alerts WHERE kind=? AND delivered_t>=? "
        "AND delivery IN ('single','rollup')", (kind, since))
    return int(row["n"]) if row else 0


def in_quiet_hours(now: Optional[float] = None) -> bool:
    """`alerts_quiet_hours` = [start_hour, end_hour] local. Unset or
    malformed means not quiet — the safe direction for an alerting system is
    to deliver."""
    try:
        window = storage.get_setting("alerts_quiet_hours", None)
        if not window or len(window) != 2:
            return False
        start, end = int(window[0]), int(window[1])
        hour = time.localtime(now if now is not None else time.time()).tm_hour
        return start <= hour < end if start <= end else (hour >= start or hour < end)
    except Exception:
        return False


def relay_once(send=None, now: Optional[float] = None) -> Dict[str, Any]:
    """Drain the pending P1/P2 queue. Only the lease holder does any work.

    Delivery order of business: dedupe already happened at record time, so
    what is left here is rate limiting per kind. Under the ceiling every row
    is its own message; over it, the whole pending set for that kind becomes
    one message with a count. Nothing is discarded either way.
    """
    out = {"sent": 0, "delivered": 0, "deferred": 0, "skipped": ""}
    try:
        _ensure()
        if not own_relay():
            out["skipped"] = "another process holds the relay lease"
            return out
        now = int(now if now is not None else time.time())
        send = send or _notifier_send
        quiet = in_quiet_hours(now)
        rows = storage.query(
            "SELECT * FROM alerts WHERE delivered_t IS NULL AND severity IN (?,?) "
            "ORDER BY created_t, id LIMIT 500", (P1, P2))
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        for kind, pending in by_kind.items():
            sev = pending[0]["severity"]
            if quiet and sev != P1:              # §7: only P1 bypasses quiet hours
                out["deferred"] += len(pending)
                continue
            allowance = (rate_limit_per_hour(kind, sev)
                         - _messages_since(kind, now - 3600))
            if allowance <= 0:
                out["deferred"] += len(pending)  # retried next hour / digested
                continue
            if allowance >= len(pending):
                for i, r in enumerate(pending):
                    if not _send_one(send, _format(r)):
                        out["deferred"] += len(pending) - i   # relay down, queue kept
                        break
                    _mark(r["id"], now, "single")
                    out["sent"] += 1
                    out["delivered"] += 1
            elif _send_one(send, _format_rollup(kind, pending)):
                _mark(pending[0]["id"], now, "rollup")
                for r in pending[1:]:
                    _mark(r["id"], now, "folded")
                out["sent"] += 1
                out["delivered"] += len(pending)
            else:
                out["deferred"] += len(pending)
    except Exception as e:
        print("[alerts] relay error:", e)
        out["skipped"] = "relay error: %s" % e
    return out


# ---------------------------------------------------------------- digest
def digest_due(now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    today = "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)
    return lt.tm_hour >= DIGEST_HOUR and storage.get_setting(_DIGEST_DAY_KEY) != today


def build_digest(now: Optional[float] = None,
                 hours: int = 24) -> Tuple[str, List[int]]:
    """One message for everything that did not deserve an interrupt: the P3
    queue, the P4 counts (which are the rail-cost signal, §8 screen 6), and
    what the pager actually sent. Returns (text, p3 row ids)."""
    now = int(now if now is not None else time.time())
    since = now - hours * 3600
    lt = time.localtime(now)
    lines = ["📋 <b>Daily digest</b> — %04d-%02d-%02d (last %dh)"
             % (lt.tm_year, lt.tm_mon, lt.tm_mday, hours)]

    p3 = storage.query(
        "SELECT * FROM alerts WHERE severity=? AND delivered_t IS NULL "
        "ORDER BY created_t, id LIMIT 200", (P3,))
    if p3:
        lines.append("\n<b>Noted</b>")
        for r in p3:
            n = _occurrences(r)
            lines.append("· %s%s — %s" % (r["kind"], " x%d" % n if n > 1 else "",
                                          r["message"]))

    skips = storage.query(
        "SELECT message, kind, COUNT(*) rows_n, SUM(dup_count) dups "
        "FROM alerts WHERE severity=? AND created_t>=? "
        "GROUP BY kind, message ORDER BY (COUNT(*)+IFNULL(SUM(dup_count),0)) DESC "
        "LIMIT 3", (P4, since))
    if skips:
        lines.append("\n<b>Top skip reasons</b>")
        for s in skips:
            lines.append("· %d x %s" % (int(s["rows_n"]) + int(s["dups"] or 0),
                                        s["message"]))

    pushed = storage.query(
        "SELECT severity, COUNT(*) n FROM alerts WHERE delivered_t>=? "
        "AND delivery IN ('single','rollup') GROUP BY severity", (since,))
    if pushed:
        lines.append("\n<b>Pushed</b>: " + ", ".join(
            "%s x%d" % (p["severity"], p["n"]) for p in pushed))

    backlog = undelivered_count()
    if backlog:
        lines.append("<b>Undelivered backlog</b>: %d" % backlog)

    return "\n".join(lines), [int(r["id"]) for r in p3]


def digest_once(send=None, now: Optional[float] = None,
                force: bool = False) -> Dict[str, Any]:
    """Send the batched P3 digest. Lease-guarded like the relay, so the
    dashboard process can queue P3 items without ever emitting a digest."""
    out = {"sent": 0, "batched": 0, "skipped": ""}
    try:
        _ensure()
        if not own_relay():
            out["skipped"] = "another process holds the relay lease"
            return out
        now = int(now if now is not None else time.time())
        if not (force or digest_due(now)):
            out["skipped"] = "not due"
            return out
        text, ids = build_digest(now)
        send = send or _notifier_send
        if not _send_one(send, text):
            out["skipped"] = "relay unavailable — digest will retry"
            return out
        for aid in ids:
            _mark(aid, now, "digest")
        lt = time.localtime(now)
        storage.set_setting(_DIGEST_DAY_KEY, "%04d-%02d-%02d"
                            % (lt.tm_year, lt.tm_mon, lt.tm_mday))
        out["sent"] = 1
        out["batched"] = len(ids)
    except Exception as e:
        print("[alerts] digest error:", e)
        out["skipped"] = "digest error: %s" % e
    return out


# ----------------------------------------------------------------- reads
def undelivered_count() -> int:
    row = storage.query_one(
        "SELECT COUNT(*) n FROM alerts WHERE delivered_t IS NULL")
    return int(row["n"]) if row else 0


def recent(limit: int = 100, **filters) -> List[Dict[str, Any]]:
    where, args = ["1=1"], []
    for col in ("severity", "kind", "dedupe_key"):
        if filters.get(col):
            where.append("%s=?" % col)
            args.append(filters[col])
    if filters.get("since"):
        where.append("created_t>=?")
        args.append(int(filters["since"]))
    if filters.get("undelivered"):
        where.append("delivered_t IS NULL")
    args.append(int(limit))
    return storage.query(
        "SELECT * FROM alerts WHERE %s ORDER BY created_t DESC, id DESC LIMIT ?"
        % " AND ".join(where), tuple(args))


def health() -> Dict[str, Any]:
    """For the dashboard liveness rail: a growing backlog means the relay
    owner is gone, and that is itself the thing worth seeing."""
    try:
        _ensure()
        oldest = storage.query_one(
            "SELECT MIN(created_t) t FROM alerts WHERE delivered_t IS NULL")
        age = int(time.time() - oldest["t"]) if oldest and oldest["t"] else 0
        pages = storage.query_one(
            "SELECT COUNT(*) n FROM alerts WHERE severity=? "
            "AND acknowledged_t IS NULL", (P1,))
        return {"undelivered_alerts": undelivered_count(),
                "oldest_undelivered_s": age,
                "unacknowledged_pages": int(pages["n"]) if pages else 0,
                "relay_owner": relay_owner()}
    except Exception as e:
        return {"undelivered_alerts": -1, "error": str(e)}
