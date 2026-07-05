"""Dashboard control-token auth — shared by dashboard_api and live_switch.

The token authorizes MUTATING endpoints only. Source of truth:
  1. DASHBOARD_TOKEN env var (recommended: macOS Keychain -> launchd env), else
  2. an auto-generated secret persisted at state/dashboard_token (0600).
Never in the repo, never in config.yaml, never in the DB (constraint 8).
"""
import os
import secrets
from typing import Optional

from fastapi import Header, HTTPException

BASE = os.path.dirname(os.path.abspath(__file__))
_TOKEN_FILE = os.path.join(BASE, "state", "dashboard_token")


def get_token() -> str:
    env = os.environ.get("DASHBOARD_TOKEN")
    if env:
        return env
    try:
        with open(_TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(_TOKEN_FILE, 0o600)
    return tok


def require_token(x_dashboard_token: Optional[str] = Header(None)) -> None:
    if not x_dashboard_token or not secrets.compare_digest(
            x_dashboard_token, get_token()):
        raise HTTPException(401, "missing/invalid X-Dashboard-Token")
