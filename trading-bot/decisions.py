"""Decision audit — every evaluated candidate AND every decision not to trade
becomes a queryable row (Phase 1 §3.8; constraint 6).

Two write paths:
  * record(): always writes — executions, skips, shadows.
  * record_state(): for the per-poll analysis notes ("no bias", "bar data
    stale — standing aside"). The engine evaluates every symbol every ~20s,
    so these are deduped: a row is written only when the (stage, reason)
    for that symbol|mode|strategy CHANGES, giving a transition log rather
    than 4,000 identical rows a day.

The regime/session/news columns make regime- and session-performance studies
plain SQL instead of a reconstruction project.
"""
import json
import time
from typing import Any, Dict, Optional

import storage
import sessions

_last_state: Dict[str, str] = {}     # key -> "stage|reason" last written


def record(strategy: Optional[str], symbol: str, trade_mode: str, stage: str,
           action: str, reason: str = "", grade: Optional[str] = None,
           checks: Any = None, features: Any = None,
           regime: Any = None, news_ctx: Any = None,
           signal_id: Optional[int] = None,
           trade_id: Optional[int] = None) -> int:
    try:
        return storage.execute(
            "INSERT INTO decisions(t,strategy,symbol,trade_mode,stage,action,"
            "grade,checks,features,regime,session,news_ctx,reason,signal_id,trade_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), strategy, symbol, trade_mode, stage, action, grade,
             json.dumps(checks, default=str) if checks is not None else None,
             json.dumps(features, default=str) if features is not None else None,
             json.dumps(regime, default=str) if regime is not None else None,
             sessions.session_label(symbol),
             json.dumps(news_ctx, default=str) if news_ctx is not None else None,
             reason, signal_id, trade_id))
    except Exception as e:                 # auditing must never kill the engine
        print("decision record error:", e)
        return 0


def record_state(strategy: Optional[str], symbol: str, trade_mode: str,
                 stage: str, reason: str, **kw) -> Optional[int]:
    """Deduped stand-aside/no-setup logging: writes only on state transitions."""
    key = "%s|%s|%s" % (symbol, trade_mode, strategy or "-")
    state = "%s|%s" % (stage, reason)
    if _last_state.get(key) == state:
        return None
    _last_state[key] = state
    return record(strategy, symbol, trade_mode, stage,
                  kw.pop("action", "stand_aside"), reason, **kw)


def funnel(hours: int = 24) -> Dict[str, Any]:
    """Stage/action aggregation for the dashboard funnel view."""
    since = int(time.time()) - hours * 3600
    rows = storage.query(
        "SELECT stage, action, COUNT(*) n FROM decisions WHERE t>=? "
        "GROUP BY stage, action ORDER BY n DESC", (since,))
    total = sum(r["n"] for r in rows)
    return {"since_hours": hours, "total": total, "by_stage": rows}


def recent(limit: int = 200, **filters) -> list:
    where, args = ["1=1"], []
    for col in ("strategy", "symbol", "trade_mode", "stage", "action"):
        v = filters.get(col)
        if v:
            where.append("%s=?" % col)
            args.append(v)
    if filters.get("since"):
        where.append("t>=?")
        args.append(int(filters["since"]))
    args.append(int(limit))
    return storage.query(
        "SELECT * FROM decisions WHERE %s ORDER BY t DESC LIMIT ?"
        % " AND ".join(where), tuple(args))
