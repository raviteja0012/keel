"""The command queue: exactly-once delivery, and who is allowed to fill it.

Two defects motivated these, both found by review rather than by a failing test.

  1. next_command() was three statements, each taking the module lock
     separately, under a Flask server running threaded=True. Two EA polls
     arriving together could both pass the SELECT before either claimed the
     row. A duplicated trail_sl is harmless. A duplicated open_trade is two
     positions where the engine sized one, and nothing downstream can undo a
     fill that already happened.

  2. POST /api/commands could close a live position and had no authentication,
     while server.host is 0.0.0.0.
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "q.db"))

import storage                                              # noqa: E402
import server                                               # noqa: E402
from dash_auth import get_token                             # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


storage.init()
client = server.app.test_client()


def _drain():
    storage.execute("DELETE FROM commands")


def test_a_command_is_served_exactly_once():
    _drain()
    storage.enqueue_command("open_trade", {"symbol": "EURUSD", "lots": 0.1})
    first = storage.next_command()
    second = storage.next_command()
    check("first poll gets the command", first is not None)
    check("second poll gets nothing", second is None,
          "DOUBLE SERVE: %r" % (second,))


def test_concurrent_polls_never_double_serve():
    """The actual race. Many threads poll at once against a single queued
    open_trade; exactly one may receive it."""
    _drain()
    storage.enqueue_command("open_trade", {"symbol": "GBPUSD", "lots": 0.1})
    got, lock = [], threading.Lock()
    start = threading.Event()

    def poll():
        start.wait()
        cmd = storage.next_command()
        if cmd:
            with lock:
                got.append(cmd["id"])

    threads = [threading.Thread(target=poll) for _ in range(24)]
    for t in threads:
        t.start()
    start.set()                       # release them together
    for t in threads:
        t.join(timeout=10)

    check("exactly one of 24 concurrent polls got the command",
          len(got) == 1, "served %d times: %r" % (len(got), got))
    check("and it was served, not lost", len(got) >= 1)


def test_many_commands_are_each_served_once():
    _drain()
    for i in range(12):
        storage.enqueue_command("trail_sl", {"ticket": i})
    seen, lock = [], threading.Lock()

    def drain():
        while True:
            cmd = storage.next_command()
            if not cmd:
                return
            with lock:
                seen.append(cmd["id"])

    threads = [threading.Thread(target=drain) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    check("every command delivered", len(seen) == 12, len(seen))
    check("no command delivered twice", len(set(seen)) == len(seen),
          "duplicates in %r" % seen)


def test_commands_post_requires_the_token():
    r = client.post("/api/commands", json={"type": "close_trade", "ticket": 42})
    check("unauthenticated close is refused", r.status_code == 401, r.status_code)
    r = client.post("/api/commands", json={"type": "close_trade", "ticket": 42},
                    headers={"X-Dashboard-Token": "not-the-token"})
    check("wrong token is refused", r.status_code == 401, r.status_code)


def test_commands_post_still_works_for_the_news_agent():
    r = client.post("/api/commands",
                    json={"type": "trail_sl", "ticket": 4242, "new_sl": 1.1},
                    headers={"X-Dashboard-Token": get_token()})
    check("the legitimate caller still gets through", r.status_code == 200,
          "%s %s" % (r.status_code, r.get_json()))


def test_commands_post_still_rejects_types_it_never_allowed():
    r = client.post("/api/commands", json={"type": "open_trade", "ticket": 1},
                    headers={"X-Dashboard-Token": get_token()})
    check("open_trade stays exclusive to the engine even with a valid token",
          r.status_code == 400, r.status_code)


def test_ea_routes_are_unchanged_and_need_no_token():
    """The EA polls and acks on different routes and was NOT changed, so no
    recompile is required. If this breaks, every deployed EA stops working."""
    check("EA can still poll", client.get("/api/commands/next").status_code == 200)
    check("EA can still ack",
          client.post("/api/commands/ack/999999").status_code == 200)


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
