#!/usr/bin/env python3
"""Container healthcheck for each Keel role. Exit 0 healthy, 1 unhealthy.

WHY NOT JUST CURL THE PORT
--------------------------
`server.py` hosts the engine as a daemon thread inside the Flask process. Flask
keeps answering HTTP perfectly while that thread is dead, wedged on a venue
call, or stuck behind storage.py's module RLock. A port probe would report a
healthy container hosting an engine that has managed nothing for an hour.

So the engine check is two-sided: the socket answers AND `engine_heartbeat_t`
has advanced recently. `engine_loop` writes that setting at the top of every
cycle, unconditionally — before the feed-age branch, before any stand-aside —
precisely so "engine process down" and "engine up, no data" are distinguishable.
This uses it for the first of those.

Deliberately NOT checked here: `integrity_suspect()` and the kill switches. A
suspect database is a page-a-human condition, not a restart-the-container
condition — restarting does not repair a database, and a restart loop on a
process that manages stops is worse than the fault it is reacting to. Those
belong to alerts.py.

Stdlib only: a healthcheck that can fail because a dependency moved is not a
healthcheck.

Usage:  keel-healthcheck.py --role engine
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

APP_DIR = os.environ.get("KEEL_APP_DIR", "/app/trading-bot")


def fail(msg: str) -> "int":
    print("UNHEALTHY: %s" % msg, flush=True)
    return 1


def ok(msg: str) -> "int":
    print("ok: %s" % msg, flush=True)
    return 0


def _http_ok(url: str, timeout: float = 5.0) -> "tuple[bool, str]":
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return False, "%s returned HTTP %s" % (url, r.status)
            r.read(2048)
            return True, ""
    except urllib.error.HTTPError as e:
        return False, "%s returned HTTP %s" % (url, e.code)
    except Exception as e:                          # noqa: BLE001
        return False, "%s unreachable (%r)" % (url, e)


def _cfg() -> dict:
    try:
        import yaml
        with open(os.path.join(APP_DIR, "config.yaml")) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def check_engine() -> int:
    cfg = _cfg()
    port = int(os.environ.get("SLC_PORT", (cfg.get("server") or {}).get("port", 8766)))
    poll = int((cfg.get("engine") or {}).get("poll_seconds", 20))

    good, why = _http_ok("http://127.0.0.1:%d/api/pairs" % port)
    if not good:
        return fail(why)

    # Liveness of the thread that actually manages money.
    sys.path.insert(0, APP_DIR)
    try:
        import storage
        storage.init()
        hb = storage.get_setting("engine_heartbeat_t")
    except Exception as e:                          # noqa: BLE001
        return fail("cannot read engine_heartbeat_t (%r)" % e)

    if not hb:
        return fail("engine has never written a heartbeat")

    # Three missed cycles. Two is inside normal jitter for a cycle that made a
    # slow venue call; four would let a wedged engine look healthy for over a
    # minute at the default 20s poll.
    age = time.time() - int(hb)
    limit = max(60, poll * 3)
    if age > limit:
        return fail("engine heartbeat %.0fs old (limit %ds) — port answers, "
                    "engine thread is not running" % (age, limit))
    return ok("port %d answering, engine heartbeat %.0fs old" % (port, age))


def check_dashboard() -> int:
    cfg = _cfg()
    port = int((cfg.get("dashboard") or {}).get("port", 8767))
    good, why = _http_ok("http://127.0.0.1:%d/api/health" % port)
    return ok("dashboard %d answering" % port) if good else fail(why)


def check_newsagent() -> int:
    """No listener, so liveness comes from the log the cycle always writes.

    `NewsAgent.run()` logs "Next cycle in Ns" on every pass, so the mtime of
    state/news_agent.log advances once per poll interval. A process that is
    alive but no longer cycling stops touching it.
    """
    cfg = _cfg()
    poll = int((cfg.get("news_agent") or {}).get("poll_seconds", 120))
    path = os.path.join(APP_DIR, "state", "news_agent.log")
    try:
        age = time.time() - os.path.getmtime(path)
    except FileNotFoundError:
        return fail("%s missing — news agent has not started a cycle" % path)
    limit = max(300, poll * 3)
    if age > limit:
        return fail("news_agent.log %.0fs stale (limit %ds)" % (age, limit))
    return ok("news agent cycling, log %.0fs old" % age)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", required=True,
                    choices=("engine", "dashboard", "newsagent"))
    args = ap.parse_args()
    return {"engine": check_engine,
            "dashboard": check_dashboard,
            "newsagent": check_newsagent}[args.role]()


if __name__ == "__main__":
    sys.exit(main())
