"""Bounded, audited parameter writes — the ONLY sanctioned way to change a
runtime parameter (Phase 1 §3.7, lesson 3: no silent self-tuning).

Guarantees enforced HERE, not by call-site discipline:
  * per-origin whitelist — the agent physically cannot touch risk_pct, stops,
    max_concurrent, trading_mode, or anything outside its five keys;
  * hard code ceilings — even a human through the dashboard cannot set
    risk_pct above 1.0 or weaken the kill switches beyond the ceilings;
  * numeric bounds shared with the auditor — hallucination_check reads the
    SAME constants instead of keeping its own copy;
  * every attempt (accepted or refused) becomes a structured row in
    `param_changes` with old/new values and the trigger data behind it;
  * human precedence — an automated origin may not counter-flip a key a human
    set recently (default 7 days) — this ends the min_grade tug-of-war.

`trading_mode` is deliberately NOT settable here; the live switch has its own
two-step confirmation path (live_switch.py) and paper/off go through it too.
"""
import json
import time
from typing import Any, Dict, Optional, Tuple

import storage

# ---------------------------------------------------------------- bounds
# Hard ceilings (code constants — invariants 8/9 of CLAUDE.md). Runtime
# settings live INSIDE these; nothing at runtime can move the ceilings.
RISK_PCT_CEILING = 1.0          # % per trade, playbook §10
DAILY_STOP_CEILING = 5.0        # % — dashboard may tighten below, never above
WEEKLY_STOP_CEILING = 10.0
MAX_CONCURRENT_CEILING = 6
MAX_BUCKET_EXPOSURE_CEILING = 4.0

# key -> (lo, hi) inclusive numeric bounds for runtime values.
BOUNDS: Dict[str, Tuple[float, float]] = {
    "risk_pct": (0.1, RISK_PCT_CEILING),
    "b_setup_risk_factor": (0.1, 1.0),
    "min_rr": (1.8, 3.0),
    "atr_buffer": (0.25, 0.60),
    "vol_mult": (0.0, 3.0),
    "max_concurrent": (1, MAX_CONCURRENT_CEILING),
    "max_concurrent_per_class": (1, MAX_CONCURRENT_CEILING),
    "max_correlated": (1, 6),
    "max_bucket_exposure": (0.5, MAX_BUCKET_EXPOSURE_CEILING),
    "daily_stop_pct": (0.5, DAILY_STOP_CEILING),
    "weekly_stop_pct": (1.0, WEEKLY_STOP_CEILING),
    "regime_max": (1.5, 4.0),
    "regime_b_ban": (1.0, 3.0),
    "max_spread_frac": (0.02, 0.25),
}

# Keys each origin may write. The agent list is the historical five ONLY.
WHITELISTS: Dict[str, set] = {
    "agent": {"min_grade", "atr_buffer", "min_rr",
              "agent_disabled_pairs", "agent_disabled_modes"},
    "sanity": {"min_rr", "atr_buffer"},
    "news": set(),                       # news layer never tunes parameters
    "human": {
        "risk_pct", "b_setup_risk_factor", "min_rr", "atr_buffer", "vol_mult",
        "max_concurrent", "max_concurrent_per_class", "max_correlated",
        "max_bucket_exposure", "daily_stop_pct", "weekly_stop_pct",
        "min_grade", "regime_max", "regime_b_ban", "max_spread_frac",
        "modes", "enabled_pairs", "watch_pairs", "paper_balance",
        "agent_enabled", "agent_disabled_pairs", "agent_disabled_modes",
        "halt_new_entries",
    },
}
_VALID_GRADES = ("A", "B")
_AUTOMATED = ("agent", "sanity", "news", "system")
HUMAN_PIN_DAYS = 7


class ParamRejected(Exception):
    """Raised when a write violates the whitelist/bounds/pin rules."""


def bounds_for(key: str) -> Optional[Tuple[float, float]]:
    return BOUNDS.get(key)


def _log(origin: str, key: str, old: Any, new: Any, accepted: bool,
         reason: str, trigger_data: Any, strategy: Optional[str],
         asset_class: Optional[str]) -> int:
    b = BOUNDS.get(key)
    return storage.execute(
        "INSERT INTO param_changes(t,origin,strategy,asset_class,key,old,new,"
        "bounds,trigger_data,accepted,ack) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
        (int(time.time()), origin, strategy, asset_class, key,
         json.dumps(old, default=str), json.dumps(new, default=str),
         json.dumps(list(b)) if b else None,
         json.dumps({"reason": reason, "data": trigger_data}, default=str),
         1 if accepted else 0))


def _last_human_change_t(key: str) -> int:
    row = storage.query_one(
        "SELECT t FROM param_changes WHERE key=? AND origin='human' "
        "AND accepted=1 ORDER BY t DESC LIMIT 1", (key,))
    return int(row["t"]) if row else 0


def set_param(key: str, value: Any, origin: str, reason: str = "",
              trigger_data: Any = None, strategy: Optional[str] = None,
              asset_class: Optional[str] = None) -> Any:
    """Validated parameter write. Returns the stored value.
    Raises ParamRejected (after logging the refused attempt) on violation."""
    wl = WHITELISTS.get(origin)
    old = storage.get_setting(key)

    def refuse(why: str) -> None:
        _log(origin, key, old, value, False, why, trigger_data,
             strategy, asset_class)
        raise ParamRejected("%s: %s" % (key, why))

    if wl is None:
        refuse("unknown origin %r" % origin)
    if key not in wl:
        refuse("origin %r may not modify this key" % origin)

    # human-pin: automation may not counter-flip a recent human decision
    if origin in _AUTOMATED:
        pinned_t = _last_human_change_t(key)
        if pinned_t and time.time() - pinned_t < HUMAN_PIN_DAYS * 86400:
            refuse("pinned by human change %dh ago (pin lasts %dd)"
                   % ((time.time() - pinned_t) // 3600, HUMAN_PIN_DAYS))

    b = BOUNDS.get(key)
    if b is not None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            refuse("expected a number in [%s, %s]" % b)
        if not (b[0] <= v <= b[1]):
            refuse("value %s outside bounds [%s, %s]" % (value, b[0], b[1]))
        value = int(v) if key.startswith("max_conc") or key == "max_correlated" else v
    if key == "min_grade" and value not in _VALID_GRADES:
        refuse("min_grade must be one of %s" % (_VALID_GRADES,))

    storage.set_setting(key, value)
    _log(origin, key, old, value, True, reason, trigger_data,
         strategy, asset_class)
    return value


def recent_changes(limit: int = 100, unacked_only: bool = False) -> list:
    where = "WHERE ack=0" if unacked_only else ""
    return storage.query(
        "SELECT * FROM param_changes %s ORDER BY t DESC LIMIT ?" % where,
        (int(limit),))


def ack_change(change_id: int) -> bool:
    storage.execute("UPDATE param_changes SET ack=1 WHERE id=?", (change_id,))
    row = storage.query_one("SELECT ack FROM param_changes WHERE id=?", (change_id,))
    return bool(row and row["ack"])
