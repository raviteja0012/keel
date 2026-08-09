"""SQLite persistence for the SLC trading bot.

One file: data/trading.db. Thread-safe via a module-level lock —
the engine, Flask handlers, and the agent all share this module.
"""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trading.db")
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_recover = {"last": 0.0, "in_progress": False}
_RECOVER_THROTTLE_S = 600        # at most one auto-recovery attempt per 10 min
_suspect = {"flag": False, "reason": ""}


def integrity_suspect() -> str:
    """Non-empty reason string while DB integrity is in doubt (corruption seen
    and not yet successfully recovered). The engine fails safe on this: no new
    entries may be opened while it is set (CLAUDE.md invariant 7)."""
    return _suspect["reason"] if _suspect["flag"] else ""


def _mark_suspect(reason: str) -> None:
    _suspect["flag"] = True
    _suspect["reason"] = reason


def _clear_suspect() -> None:
    _suspect["flag"] = False
    _suspect["reason"] = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, tf TEXT NOT NULL, t INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (symbol, tf, t)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,              -- paper | live
    trade_mode TEXT NOT NULL,        -- intraday | swing
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,              -- buy | sell
    status TEXT NOT NULL,            -- open | closed
    grade TEXT,                      -- A | B
    entry_time INTEGER, entry REAL, sl REAL, initial_sl REAL,
    tp1 REAL, tp2 REAL,
    lots REAL, risk_pct REAL, risk_amount REAL,
    exit_time INTEGER, exit_price REAL,
    pnl REAL DEFAULT 0, r_multiple REAL,
    ticket INTEGER,                  -- MT5 position id (live only)
    tp1_done INTEGER DEFAULT 0,
    mfe REAL DEFAULT 0, mae REAL DEFAULT 0,
    exit_reason TEXT, setup TEXT, signal_id INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER, symbol TEXT, trade_mode TEXT, side TEXT, grade TEXT,
    entry REAL, sl REAL, tp REAL, rr REAL,
    status TEXT,                     -- executed | skipped
    reason TEXT, setup TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    t INTEGER, mode TEXT, balance REAL, equity REAL,
    PRIMARY KEY (t, mode)
);
CREATE TABLE IF NOT EXISTS agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER, kind TEXT,            -- eval | change | info
    action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER, type TEXT, payload TEXT,
    status TEXT DEFAULT 'pending'    -- pending | sent | acked | expired
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,       -- forex | metals | indices | energies | crypto
    venue TEXT DEFAULT 'mt5',
    venue_symbol TEXT,
    session_calendar TEXT,           -- forex | metals | indices | crypto
    day_boundary TEXT,               -- broker | utc
    display_digits INTEGER,
    factor_exposures TEXT,           -- JSON {"USD":-1,"EUR":1,...} for a BUY
    news_entities TEXT,              -- JSON ["EUR","USD"] / ["BTC","crypto"] ...
    volume_min REAL, volume_step REAL, volume_max REAL,
    enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER, strategy TEXT, symbol TEXT, trade_mode TEXT, stage TEXT,
    action TEXT,                     -- executed | skipped | shadow | stand_aside | no_setup
    grade TEXT, checks TEXT, features TEXT,
    regime TEXT, session TEXT, news_ctx TEXT,
    reason TEXT, signal_id INTEGER, trade_id INTEGER
);
CREATE TABLE IF NOT EXISTS param_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER, origin TEXT,          -- agent | sanity | human | system | news
    strategy TEXT, asset_class TEXT,
    key TEXT, old TEXT, new TEXT,
    bounds TEXT, trigger_data TEXT,
    accepted INTEGER DEFAULT 1,      -- 0 = refused by the write layer (still logged)
    ack INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER,                       -- scheduled/publish time (epoch UTC)
    kind TEXT,                       -- scheduled | headline
    source TEXT, entities TEXT, impact TEXT,
    title TEXT, payload TEXT, hash TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS news_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER, event_id INTEGER, action TEXT,
    symbol TEXT, ticket INTEGER, reason TEXT
);
CREATE TABLE IF NOT EXISTS promotion_signoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t INTEGER, strategy TEXT, asset_class TEXT,
    sample_n INTEGER, expectancy_r REAL, data_trust TEXT,
    signed_by TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, mode);
CREATE INDEX IF NOT EXISTS idx_signals_t ON signals(t);
CREATE INDEX IF NOT EXISTS idx_decisions_t ON decisions(t);
CREATE INDEX IF NOT EXISTS idx_news_events_t ON news_events(t);
"""

# Additive column migrations for tables that predate the multi-asset work.
# Each entry is (table, column, DDL type/default). Idempotent.
_MIGRATIONS = [
    ("trades", "strategy", "TEXT"),
    ("trades", "asset_class", "TEXT"),
    # Round-turn cost deducted from a paper close: commission, taker fee and
    # slippage. Recorded rather than merely subtracted so the gross and net
    # numbers stay reconcilable, and so a suspicious cost is visible in the
    # trade row instead of hiding inside the P&L.
    ("trades", "costs", "REAL DEFAULT 0"),
    ("signals", "strategy", "TEXT"),
    ("commands", "expires_at", "INTEGER"),   # honored by next_command
    ("commands", "sent_t", "INTEGER"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Additive ALTERs for columns introduced after a table already existed."""
    for table, col, decl in _MIGRATIONS:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        if col not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))


def init() -> None:
    global _conn
    with _lock:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _apply_migrations(_conn)
        _conn.commit()


def _is_corrupt(e: Exception) -> bool:
    s = str(e).lower()
    return ("malformed" in s or "is not a database" in s
            or "disk image" in s or "database corruption" in s)


def _attempt_recover() -> bool:
    """On DB corruption, rebuild via sqlite3 .recover, verify integrity, swap
    in, and switch to WAL. Throttled and lock-guarded; returns True on success.
    Never raises — the caller degrades gracefully if this returns False."""
    global _conn
    import subprocess, shutil
    # The CLI is the better tool: ".recover" salvages rows out of damaged pages
    # that a normal read cannot reach. But stock Windows ships no sqlite3.exe and
    # the old fallback was the literal path /usr/bin/sqlite3, so this whole
    # function returned False on the ONLY operating system that can host MT5 —
    # invariant 7's recovery arm was dead code exactly where it was needed.
    #
    # So: use the CLI when it exists, and fall back to a pure-Python rebuild via
    # iterdump when it does not. The fallback salvages less, and says so.
    sqlite_bin = shutil.which("sqlite3")
    if sqlite_bin is None and os.path.exists("/usr/bin/sqlite3"):
        sqlite_bin = "/usr/bin/sqlite3"
    now = time.time()
    if _recover["in_progress"] or (now - _recover["last"] < _RECOVER_THROTTLE_S):
        return False
    _recover["in_progress"] = True
    _recover["last"] = now
    try:
        print("[storage] DB CORRUPTION detected — attempting .recover rebuild")
        try:
            if _conn is not None:
                _conn.close()
        except Exception:
            pass
        _conn = None
        tmp = _DB_PATH + ".recovered"
        if os.path.exists(tmp):
            os.remove(tmp)
        if sqlite_bin:
            print("[storage] rebuilding with the sqlite3 CLI (.recover)")
            p1 = subprocess.Popen([sqlite_bin, _DB_PATH, ".recover"],
                                  stdout=subprocess.PIPE)
            p2 = subprocess.Popen([sqlite_bin, tmp], stdin=p1.stdout)
            p1.stdout.close()
            p2.communicate(timeout=180)
            if p2.returncode != 0:
                print("[storage] .recover failed (rc=%s)" % p2.returncode)
                return False
        else:
            # No CLI: rebuild from whatever the Python driver can still read.
            # iterdump stops at the first unreadable page rather than salvaging
            # around it, so this recovers less than .recover would. Partial is
            # still better than latching suspect and refusing to trade, and the
            # integrity_check below is the same gate either way.
            print("[storage] no sqlite3 CLI on PATH — pure-Python rebuild "
                  "(salvages less than .recover; install sqlite3 for a fuller "
                  "recovery)")
            src = sqlite3.connect(_DB_PATH)
            dst = sqlite3.connect(tmp)
            salvaged, stopped = 0, None
            try:
                with dst:
                    for stmt in src.iterdump():
                        try:
                            dst.execute(stmt)
                            salvaged += 1
                        except sqlite3.DatabaseError as e:
                            stopped = str(e)
                            break
            except sqlite3.DatabaseError as e:
                stopped = str(e)
            finally:
                src.close()
                dst.close()
            print("[storage] salvaged %d statements%s"
                  % (salvaged, "" if not stopped else "; stopped at: %s" % stopped[:120]))
            if salvaged == 0:
                return False
        chk = sqlite3.connect(tmp)
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        chk.close()
        if ok != "ok":
            print("[storage] recovered DB still not ok: %s" % ok)
            return False
        ts = time.strftime("%Y%m%d-%H%M%S")
        os.replace(_DB_PATH, _DB_PATH + ".corrupt-" + ts)
        os.replace(tmp, _DB_PATH)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _apply_migrations(_conn)
        _conn.commit()
        _clear_suspect()
        print("[storage] recovery OK — DB rebuilt + WAL enabled; corrupt copy kept")
        return True
    except Exception as e:
        print("[storage] recovery error: %s" % e)
        return False
    finally:
        _recover["in_progress"] = False


def _c() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn  # type: ignore


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        try:
            cur = _c().execute(sql, params)
            _c().commit()
            return cur.lastrowid or 0
        except sqlite3.DatabaseError as e:
            if _is_corrupt(e):
                if _attempt_recover():
                    _clear_suspect()
                    cur = _c().execute(sql, params)
                    _c().commit()
                    return cur.lastrowid or 0
                _mark_suspect("DB corruption detected; recovery pending/throttled")
            raise


def query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with _lock:
        try:
            rows = _c().execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.DatabaseError as e:
            if _is_corrupt(e):
                if _attempt_recover():
                    _clear_suspect()
                    rows = _c().execute(sql, params).fetchall()
                    return [dict(r) for r in rows]
                # Flag the DB as untrustworthy BEFORE degrading to an empty
                # result: an empty read must never be mistaken for "no open
                # trades / no losses today" while the file is broken.
                _mark_suspect("DB corruption detected; recovery pending/throttled")
                return []          # don't crash the engine loop while throttled
            raise


def query_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


# ---------------- settings (runtime-tunable, DB wins over yaml) ------
def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def all_settings() -> Dict[str, Any]:
    out = {}
    for r in query("SELECT key, value FROM settings"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            out[r["key"]] = r["value"]
    return out


# ---------------- bars -----------------------------------------------
def store_bars(symbol: str, tf: str, bars: List[Dict[str, Any]]) -> None:
    if not bars:
        return
    with _lock:
        _c().executemany(
            "INSERT OR REPLACE INTO bars(symbol,tf,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?,?)",
            [(symbol, tf, b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v", 0)) for b in bars],
        )
        _c().commit()


def get_bars(symbol: str, tf: str, limit: int = 400) -> List[Dict[str, Any]]:
    rows = query(
        "SELECT t,o,h,l,c,v FROM bars WHERE symbol=? AND tf=? ORDER BY t DESC LIMIT ?",
        (symbol, tf, limit),
    )
    return list(reversed(rows))


# ---------------- trades ----------------------------------------------
def insert_trade(tr: Dict[str, Any]) -> int:
    cols = ",".join(tr.keys())
    ph = ",".join("?" * len(tr))
    return execute("INSERT INTO trades(%s) VALUES(%s)" % (cols, ph), tuple(tr.values()))


def update_trade(trade_id: int, fields: Dict[str, Any]) -> None:
    sets = ",".join("%s=?" % k for k in fields)
    execute("UPDATE trades SET %s WHERE id=?" % sets, tuple(fields.values()) + (trade_id,))


def open_trades(mode: Optional[str] = None) -> List[Dict[str, Any]]:
    if mode:
        return query("SELECT * FROM trades WHERE status='open' AND mode=?", (mode,))
    return query("SELECT * FROM trades WHERE status='open'")


# ---------------- command queue (server -> EA) ------------------------
# Commands carry an expiry (default 5 min) so a stale SL move or open can
# never execute arbitrarily late, and a served command is marked 'sent' and
# only re-served after RESEND_GRACE_S without an ack — an EA that executed
# but failed to ack no longer gets the same live order replayed every poll.
COMMAND_TTL_S = 300
RESEND_GRACE_S = 120


def enqueue_command(cmd_type: str, payload: Dict[str, Any],
                    ttl_s: int = COMMAND_TTL_S) -> int:
    now = int(time.time())
    return execute(
        "INSERT INTO commands(t,type,payload,status,expires_at) "
        "VALUES(?,?,?,'pending',?)",
        (now, cmd_type, json.dumps(payload), now + max(1, int(ttl_s))),
    )


def next_command() -> Optional[Dict[str, Any]]:
    """Claim the next command for the EA. ATOMIC.

    This used to be three statements — expire, select, claim — each taking and
    releasing the module lock on its own. server.py runs Flask with
    threaded=True, so two EA polls arriving together could both complete the
    SELECT before either ran the claiming UPDATE, and both would be handed the
    same row. For a trail_sl that is a harmless duplicate. For an open_trade it
    is two positions where the engine sized one, and no rail downstream can
    undo a fill that already happened.

    Holding the lock across all three statements closes it: the claim is
    conditional on the row still being claimable, and a losing racer gets
    rowcount 0 and simply returns nothing. It will poll again in a second."""
    now = int(time.time())
    with _lock:
        c = _c()
        c.execute("UPDATE commands SET status='expired' "
                  "WHERE status IN ('pending','sent') AND expires_at IS NOT NULL "
                  "AND expires_at < ?", (now,))
        cur = c.execute(
            "SELECT * FROM commands WHERE status='pending' "
            "OR (status='sent' AND sent_t IS NOT NULL AND sent_t < ?) "
            "ORDER BY id LIMIT 1", (now - RESEND_GRACE_S,))
        row = cur.fetchone()
        if row is None:
            c.commit()
            return None
        # Conditional claim: re-assert the state the SELECT saw. Belt and braces
        # given the lock, and the thing that keeps this correct if the lock is
        # ever relaxed or a second process appears.
        claimed = c.execute(
            "UPDATE commands SET status='sent', sent_t=? "
            "WHERE id=? AND (status='pending' "
            "OR (status='sent' AND sent_t IS NOT NULL AND sent_t < ?))",
            (now, row["id"], now - RESEND_GRACE_S))
        c.commit()
        if claimed.rowcount != 1:
            return None                    # somebody else took it; poll again
    payload = json.loads(row["payload"])
    payload["id"] = str(row["id"])
    payload["type"] = row["type"]
    return payload


def ack_command(cmd_id: str) -> bool:
    with _lock:
        cur = _c().execute(
            "UPDATE commands SET status='acked' WHERE id=? "
            "AND status IN ('pending','sent')", (cmd_id,))
        _c().commit()
        return cur.rowcount > 0


# ---------------- misc -------------------------------------------------
def log_agent(kind: str, action: str, detail: str = "") -> None:
    execute(
        "INSERT INTO agent_log(t,kind,action,detail) VALUES(?,?,?,?)",
        (int(time.time()), kind, action, detail),
    )


def record_equity(mode: str, balance: float, equity: float) -> None:
    execute(
        "INSERT OR REPLACE INTO equity(t,mode,balance,equity) VALUES(?,?,?,?)",
        (int(time.time()) // 60 * 60, mode, balance, equity),
    )
