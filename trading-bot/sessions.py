"""Per-asset-class session calendars, day/week boundaries, and staleness.

Design rule (Phase 1 §4): crypto is NOT forced into a forex-shaped session
model. For crypto the calendar never blocks entries — it contributes a *risk
factor* (thin weekend liquidity → half risk, per playbook §9) and study
labels. Forex/metals keep the proven weekend/Friday-cutoff gate; index and
energy CFDs get a daily maintenance break plus the weekend.

All functions take UTC epochs/datetimes. Broker-day boundaries additionally
take the broker clock offset the engine already derives from the EA terminal
info (bars are broker time, not UTC).
"""
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Dict, Optional, Tuple

import instruments

# Forex week (UTC): opens Sunday 22:00, closes Friday 22:00, with a 30-min
# no-new-entries buffer before the Friday close (ported from the legacy
# session gate that was validated in production).
_FRIDAY_CUTOFF_UTC = dt_time(21, 30)
_SUNDAY_OPEN_UTC = dt_time(22, 0)

# Index/energy CFDs trade nearly 24/5 on MT5 but have a daily maintenance
# break around the NY close where spreads blow out; block new entries there.
_CFD_BREAK_START = dt_time(21, 55)
_CFD_BREAK_END = dt_time(22, 15)

# How stale the newest setup-TF bar may be before the engine stands aside,
# expressed as a multiple of the bar period. Crypto trades 24/7 so gaps are
# genuinely anomalous; forex/metals/indices see legitimate session gaps.
_STALENESS_MULT = {"crypto": 2.0, "forex": 2.0, "metals": 2.0,
                   "indices": 2.0, "energies": 2.0}
# Absolute feed-freshness expectation (seconds) per class for health checks:
# over an FX weekend a 48h-old bar is normal; for crypto it means the feed died.
FRESH_LIMIT_S = {"crypto": 4 * 3600, "forex": 60 * 3600,
                 "metals": 60 * 3600, "indices": 60 * 3600,
                 "energies": 60 * 3600}


def _dt(now: Optional[float] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(now, tz=timezone.utc)


def _forex_open_at(dt: datetime) -> bool:
    wd, t = dt.weekday(), dt.time()
    if wd == 5:                                   # Saturday
        return False
    if wd == 6 and t < _SUNDAY_OPEN_UTC:          # Sunday before 22:00
        return False
    if wd == 4 and t >= _FRIDAY_CUTOFF_UTC:       # Friday 21:30+ buffer
        return False
    return True


def _cfd_open_at(dt: datetime) -> bool:
    if not _forex_open_at(dt):                    # weekend closed like FX
        return False
    t = dt.time()
    if _CFD_BREAK_START <= t < _CFD_BREAK_END:    # daily maintenance break
        return False
    return True


def is_market_open(symbol: str, now: Optional[float] = None) -> Tuple[bool, str]:
    """(open_for_new_entries, reason_if_closed). Never blocks crypto."""
    cal = instruments.get(symbol).get("session_calendar") or "forex"
    dt = _dt(now)
    if cal == "crypto":
        return True, ""
    if cal in ("forex", "metals"):
        if not _forex_open_at(dt):
            return False, "market closed (%s calendar, %s %s UTC)" % (
                cal, dt.strftime("%a"), dt.strftime("%H:%M"))
        return True, ""
    # indices / energies
    if not _cfd_open_at(dt):
        t = dt.time()
        if _CFD_BREAK_START <= t < _CFD_BREAK_END:
            return False, "daily CFD maintenance break (21:55–22:15 UTC)"
        return False, "market closed (%s calendar, %s %s UTC)" % (
            cal, dt.strftime("%a"), dt.strftime("%H:%M"))
    return True, ""


def session_label(symbol: str, now: Optional[float] = None) -> str:
    """Study label stamped on every decision. Crypto gets UTC-hour buckets +
    weekend flag instead of the FX session names."""
    dt = _dt(now)
    cal = instruments.get(symbol).get("session_calendar") or "forex"
    if cal == "crypto":
        buck = "h%02d" % dt.hour
        return ("weekend+" + buck) if dt.weekday() >= 5 else buck
    hour = dt.hour
    sessions = []
    if hour >= 22 or hour < 7:
        sessions.append("Sydney")
    if 0 <= hour < 9:
        sessions.append("Tokyo")
    if 8 <= hour < 17:
        sessions.append("London")
    if 13 <= hour < 22:
        sessions.append("NewYork")
    if 13 <= hour < 17:
        sessions.append("overlap")
    return "+".join(sessions) if sessions else "inter-session"


def entry_risk_factor(symbol: str, now: Optional[float] = None) -> Tuple[float, str]:
    """Session-driven risk multiplier for NEW entries (≤ 1.0, never raises
    risk). Crypto weekends trade at half risk (playbook §9: sweeps common,
    follow-through poor)."""
    dt = _dt(now)
    cls = instruments.asset_class(symbol)
    if cls == "crypto" and dt.weekday() >= 5:
        return 0.5, "crypto weekend — half risk (thin liquidity)"
    return 1.0, ""


def day_week_start(symbol_or_boundary: str, now: Optional[float] = None,
                   broker_offset_s: float = 0.0) -> Tuple[int, int]:
    """(day_start_epoch, week_start_epoch) for loss-window accounting.

    boundary 'utc' (crypto): calendar UTC day / ISO week.
    boundary 'broker' (MT5 CFDs/FX): the broker's midnight, i.e. UTC shifted
    by the terminal clock offset the EA reports — matches how the broker
    statements cut the day.
    """
    if symbol_or_boundary in ("utc", "broker"):
        boundary = symbol_or_boundary
    else:
        boundary = instruments.get(symbol_or_boundary).get("day_boundary") or "utc"
    off = broker_offset_s if boundary == "broker" else 0.0
    dt = _dt((now if now is not None else datetime.now(timezone.utc).timestamp()) + off)
    day0 = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = day0 - timedelta(days=day0.weekday())
    return int(day0.timestamp() - off), int(week0.timestamp() - off)


def staleness_limit_s(symbol: str, tf_seconds: int) -> float:
    cls = instruments.asset_class(symbol)
    return _STALENESS_MULT.get(cls, 2.0) * tf_seconds


def fresh_limit_s(symbol: str) -> int:
    return FRESH_LIMIT_S.get(instruments.asset_class(symbol), 12 * 3600)
