"""Live-trading switch — code-isolated, two-step, promotion-gated (Phase 5).

This module is the ONLY sanctioned path to `trading_mode = live`:
  * params_store rejects `trading_mode` for every origin;
  * the legacy Flask `/api/settings` route is de-fanged for the live value
    (server.py refuses it and points here);
  * the dashboard mounts this router under /api/live.

The switch is deliberately hard to trip and easy to reverse:

  step 0  every endpoint here requires the dashboard control token
          (dashboard_api.require_token dependency on the router);
  step 1  POST /request  -> refused unless (a) no fail-safe is active
          (DB integrity, manual halt) and (b) at least one strategy×class
          promotion cell has an OPEN gate: >=50 closed paper trades,
          positive expectancy, GROUNDED data-trust verdict, rails
          demonstrably exercised, and a MANUAL SIGN-OFF row (constraint 1 —
          never automatic). Returns a one-time confirm token + phrase.
  step 2  POST /confirm {token, phrase} within 60s, with the phrase typed
          back exactly. Only then does trading_mode flip — and the EA's
          AllowTradeExecution input (default false, EA-side) remains the
          independent second half of the double gate: this switch cannot
          place a live order by itself.

  POST /paper flips back with no ceremony — de-escalation is always easy.
  POST /signoff records the human promotion sign-off for a cell (also
  token-gated; it does NOT flip anything).

Every action lands in agent_log and notifications.
"""
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends

import analysis
import storage
from dash_auth import require_token

router = APIRouter(dependencies=[Depends(require_token)])

CONFIRM_TTL_S = 60
CONFIRM_PHRASE = "GO LIVE"
_pending: Dict[str, Any] = {}          # one outstanding request at a time


def _notify(msg: str) -> None:
    try:
        import notifier
        notifier.send(msg)
    except Exception:
        pass


def _open_cells() -> Dict[str, Any]:
    return {k: v for k, v in analysis.promotion_status()["cells"].items()
            if v["gate_open"]}


def _blockers() -> Optional[str]:
    suspect = storage.integrity_suspect()
    if suspect:
        return "DB integrity suspect: %s" % suspect
    if storage.get_setting("halt_new_entries", False):
        return "manual halt is active — resume before going live"
    return None


@router.get("/status")
def status():
    cells = analysis.promotion_status()["cells"]
    return {"trading_mode": storage.get_setting("trading_mode", "paper"),
            "open_gates": [k for k, v in cells.items() if v["gate_open"]],
            "blocker": _blockers(),
            "pending_request": bool(_pending
                                    and _pending.get("exp", 0) > time.time())}


@router.post("/signoff")
def signoff(body: Dict[str, Any] = Body(...)):
    """Record the manual promotion sign-off for one strategy×class cell.
    Records who/when plus the sample snapshot it was based on. Does NOT
    change trading_mode."""
    strat = str(body.get("strategy", "")).strip()
    a_class = str(body.get("asset_class", "")).strip()
    signed_by = str(body.get("signed_by", "")).strip()
    if not (strat and a_class and signed_by):
        return {"ok": False, "reason": "strategy, asset_class, signed_by required"}
    cell = analysis.promotion_status()["cells"].get("%s|%s" % (strat, a_class))
    if not cell:
        return {"ok": False, "reason": "no closed paper trades for that cell"}
    st = cell["stats"]
    storage.execute(
        "INSERT INTO promotion_signoffs(t,strategy,asset_class,sample_n,"
        "expectancy_r,data_trust,signed_by,note) VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time()), strat, a_class, st["n"], st.get("expectancy_r"),
         cell["checks"]["data_trust"]["value"], signed_by,
         str(body.get("note", ""))[:400]))
    storage.log_agent("info", "promotion_signoff",
                      "%s×%s signed off by %s (n=%d, exp %.3fR)"
                      % (strat, a_class, signed_by, st["n"],
                         st.get("expectancy_r") or 0))
    _notify("📋 Promotion sign-off recorded: <b>%s × %s</b> by %s"
            % (strat, a_class, signed_by))
    return {"ok": True}


@router.post("/request")
def request_live():
    """Step 1 of 2. Validates the gate and issues a one-time confirm token."""
    if storage.get_setting("trading_mode") == "live":
        return {"ok": False, "reason": "already live"}
    blocker = _blockers()
    if blocker:
        storage.log_agent("info", "live_request", "REFUSED: %s" % blocker)
        return {"ok": False, "reason": blocker}
    cells = _open_cells()
    if not cells:
        storage.log_agent("info", "live_request",
                          "REFUSED: no strategy×class promotion gate is open")
        return {"ok": False,
                "reason": "no promotion gate is open — need >=50 closed paper "
                          "trades with positive expectancy on GROUNDED data, "
                          "exercised rails, and a manual sign-off"}
    token = secrets.token_urlsafe(16)
    _pending.clear()
    _pending.update({"token": token, "exp": time.time() + CONFIRM_TTL_S})
    gate_cell = ", ".join(sorted(cells))
    storage.log_agent("info", "live_request",
                      "live switch requested (gates open: %s)" % gate_cell)
    return {"ok": True, "confirm_token": token,
            "confirm_phrase": CONFIRM_PHRASE,
            "expires_in_s": CONFIRM_TTL_S, "gate_cell": gate_cell}


@router.post("/confirm")
def confirm_live(body: Dict[str, Any] = Body(...)):
    """Step 2 of 2. Requires the fresh token AND the typed phrase."""
    token = str(body.get("token", ""))
    phrase = str(body.get("phrase", ""))
    if not _pending or _pending.get("exp", 0) < time.time():
        _pending.clear()
        return {"ok": False, "reason": "no live request pending (or expired) — start over"}
    if not secrets.compare_digest(token, _pending.get("token", "")):
        return {"ok": False, "reason": "bad confirm token"}
    if phrase.strip() != CONFIRM_PHRASE:
        return {"ok": False, "reason": "confirmation phrase mismatch — type %r"
                                       % CONFIRM_PHRASE}
    # re-validate at confirm time: the world may have changed in 60s
    blocker = _blockers()
    if blocker:
        _pending.clear()
        return {"ok": False, "reason": blocker}
    if not _open_cells():
        _pending.clear()
        return {"ok": False, "reason": "promotion gate closed since the request"}
    _pending.clear()
    storage.set_setting("trading_mode", "live")
    storage.log_agent("change", "trading_mode",
                      "paper -> LIVE via two-step confirmed switch")
    _notify("🔴 <b>LIVE TRADING ENABLED</b> via two-step switch.\n"
            "The EA's AllowTradeExecution input is the final gate — orders "
            "only execute if it is true. Start at minimum size (playbook).")
    return {"ok": True, "trading_mode": "live",
            "note": "EA AllowTradeExecution must ALSO be true for any order "
                    "to execute (double gate)"}


@router.post("/paper")
def back_to_paper():
    """De-escalation is one call — never gated."""
    storage.set_setting("trading_mode", "paper")
    _pending.clear()
    storage.log_agent("change", "trading_mode", "switched back to paper (dashboard)")
    _notify("🟢 Trading mode switched back to <b>PAPER</b>")
    return {"ok": True, "trading_mode": "paper"}
