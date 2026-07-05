"""Cross-asset structural exposure — the answer to the correlation audit that
found 30 of 47 losers carried stacked correlated exposure.

Each instrument declares signed factor weights for a BUY (instruments
registry); an open position contributes side-signed, risk-weighted exposure to
each bucket (a half-risk B trade counts 0.5). The gate blocks a candidate
whose fill would push any bucket's |net| beyond `max_bucket_exposure` —
regardless of asset class, so long EURUSD + long GER40 + long XAUUSD all
stack in the USD/RISK buckets even though no two share a symbol.

Pure functions over plain dicts: unit-testable without the engine.
"""
from typing import Any, Dict, List, Optional, Tuple

import instruments


def _risk_weight(tr: Dict[str, Any], base_risk_pct: float) -> float:
    """Position weight in units of 'one full-risk trade'. Falls back to 1.0
    when risk_pct wasn't recorded (defensive: unknown risk must still consume
    budget, never bypass it)."""
    rp = tr.get("risk_pct") or 0.0
    if rp <= 0 or base_risk_pct <= 0:
        return 1.0
    return min(2.0, rp / base_risk_pct)


def bucket_exposures(open_trades: List[Dict[str, Any]],
                     base_risk_pct: float = 1.0) -> Dict[str, float]:
    """Net exposure per factor bucket across ALL open money positions."""
    buckets: Dict[str, float] = {}
    for tr in open_trades:
        if tr.get("mode") == "shadow":
            continue
        w = _risk_weight(tr, base_risk_pct)
        for bucket, f in instruments.factor_exposures(tr["symbol"], tr["side"]).items():
            buckets[bucket] = buckets.get(bucket, 0.0) + f * w
    return {k: round(v, 3) for k, v in buckets.items()}


def check_candidate(symbol: str, side: str, risk_pct: float,
                    open_trades: List[Dict[str, Any]],
                    max_bucket_exposure: float,
                    base_risk_pct: float = 1.0
                    ) -> Tuple[bool, str, Dict[str, Any]]:
    """(ok, reason_if_blocked, snapshot). The snapshot is written to the
    decision record either way, so exposure state at every decision is
    auditable after the fact."""
    current = bucket_exposures(open_trades, base_risk_pct)
    w = min(2.0, (risk_pct / base_risk_pct) if base_risk_pct > 0 and risk_pct > 0 else 1.0)
    candidate = instruments.factor_exposures(symbol, side)
    projected = dict(current)
    for bucket, f in candidate.items():
        projected[bucket] = round(projected.get(bucket, 0.0) + f * w, 3)

    snapshot = {"current": current, "candidate": {k: round(v * w, 3) for k, v in candidate.items()},
                "projected": projected, "cap": max_bucket_exposure}

    worst: Optional[Tuple[str, float]] = None
    for bucket, net in projected.items():
        # only gate when the candidate INCREASES the bucket's magnitude —
        # a hedging/reducing trade is never blocked by this rail
        if abs(net) > max_bucket_exposure and abs(net) > abs(current.get(bucket, 0.0)):
            if worst is None or abs(net) > abs(worst[1]):
                worst = (bucket, net)
    if worst:
        return False, ("bucket exposure cap: %s would reach %+.2f (cap %.2f) — "
                       "same underlying bet stacked across instruments"
                       % (worst[0], worst[1], max_bucket_exposure)), snapshot
    return True, "", snapshot


def class_concurrency(open_trades: List[Dict[str, Any]]) -> Dict[str, int]:
    """Open money-position count per asset class."""
    out: Dict[str, int] = {}
    for tr in open_trades:
        if tr.get("mode") == "shadow":
            continue
        cls = instruments.asset_class(tr["symbol"])
        out[cls] = out.get(cls, 0) + 1
    return out
