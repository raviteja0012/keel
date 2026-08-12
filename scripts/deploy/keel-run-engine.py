#!/usr/bin/env python3
"""Run server.py so that a stop signal stops the engine loop deterministically.

WHY THIS EXISTS
---------------
`server.py` starts `engine.engine_loop` as a daemon thread and then blocks in
`app.run()`. The machinery for a clean stop already exists in `engine.py`:
`engine.stop()` sets a `threading.Event`, `engine_loop` tests it at the top of
every pass, and the cycle's trailing `_stop.wait(poll_seconds)` returns the
instant it is set. Nothing calls it. `server.py` installs no signal handler, so
the interpreter's default SIGINT behaviour applies — `app.run()` unwinds, the
process exits, and the daemon thread is destroyed wherever it happened to be.
Possibly between `storage.enqueue_command("close_trade", ...)` and the ack.

`keel-supervise.py` used to paper over that by timing its SIGINT against
`engine_heartbeat_t`: watch for the heartbeat to advance, then sleep ~60% of a
poll interval, then signal, hoping to land in the gap between cycles. A guess
is not a stop, and it cost a full extra cycle on every container stop. This
wrapper turns the guess into a fact from outside `server.py`:

    SIGINT/SIGTERM -> engine.stop() -> wait for engine_loop to RETURN -> exit

WHAT IT DOES NOT DO
-------------------
It decides nothing about a trade, and it moves no rail out of `engine.py`
(CONTRIBUTING.md): the entire mechanism is one call to a function `engine.py`
already exposes. `agent_loop` and the news-calendar refresher are still daemon
threads that die with the interpreter; they write through `params_store`, which
is transactional, and neither holds a position. The engine loop is the one
whose interruption can leave the database disagreeing with the venue.

If the loop does not return in time this exits non-zero and says so, rather
than exiting 0 on a stop that was not clean.

WHY IT WRAPS RATHER THAN EDITS
------------------------------
The durable fix is a handler inside `server.py` that calls `engine.stop()` and
joins the loop thread; `server.py` is not part of this change. `runpy` rather
than `import server`, because server.py does all of its startup under
`if __name__ == "__main__"` and importing it starts nothing.

Usage:  keel-run-engine.py            (KEEL_APP_DIR selects the app directory)
"""
import os
import runpy
import signal
import sys
import threading
import time

APP_DIR = os.environ.get("KEEL_APP_DIR", "/app/trading-bot")
SERVER = os.path.join(APP_DIR, "server.py")

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import engine                                       # noqa: E402

# How long the loop gets to leave the cycle after being asked. It normally
# returns in milliseconds — the wait it is parked in is the one being set.
# The budget is for a pass that is mid-venue-call, and it must stay inside
# keel-supervise's own child grace, which is derived from poll_seconds.
LOOP_STOP_S = float(os.environ.get("KEEL_LOOP_STOP_S", "30"))

_loop_started = threading.Event()
_loop_exited = threading.Event()
_stop_signum = None


def log(msg: str) -> None:
    print("[run-engine] %s" % msg, flush=True)


def instrument_loop() -> None:
    """Wrap `engine.engine_loop` so this process can tell when it has returned.

    `server.py` starts the loop with `threading.Thread(target=engine.engine_loop,
    ...)` and keeps no reference to the thread, so there is nothing out here to
    join. Replacing the attribute BEFORE server.py runs means the thread it
    creates runs the wrapper, and the wrapper's `finally` is the single fact
    needed: the loop has left the cycle. The wrapper adds no behaviour — it
    calls the original and returns what it returns.
    """
    original = engine.engine_loop

    def tracked(*args, **kwargs):
        _loop_started.set()
        try:
            return original(*args, **kwargs)
        finally:
            _loop_exited.set()

    tracked.__name__ = getattr(original, "__name__", "engine_loop")
    tracked.__doc__ = original.__doc__
    engine.engine_loop = tracked


def _on_signal(signum, _frame):
    """Ask the loop to stop, then unwind Flask exactly as the default handler
    would. The wait happens in `drain_loop()` on the way out, not here."""
    global _stop_signum
    if _stop_signum is None:
        _stop_signum = signum
        log("received %s — asking engine_loop to finish its cycle"
            % signal.Signals(signum).name)
        engine.stop()
    raise KeyboardInterrupt


def drain_loop() -> int:
    """Do not let the interpreter exit while the loop is still in a cycle."""
    if not _loop_started.is_set():
        log("engine_loop never started; nothing to wait for")
        return 0

    engine.stop()           # idempotent, and covers a server.py exit that no
                            # signal caused
    t0 = time.time()
    if _loop_exited.wait(LOOP_STOP_S):
        log("engine_loop returned after %.1fs — stop was clean" % (time.time() - t0))
        return 0

    # Loud, and non-zero. The interpreter is about to exit and destroy the
    # thread mid-cycle; what must not happen is that looking like a clean stop.
    log("ENGINE LOOP DID NOT RETURN within %.0fs. The interpreter is about to "
        "exit and the loop thread will be destroyed mid-cycle. Reconcile "
        "against the venue before resuming entries." % LOOP_STOP_S)
    return 75               # EX_TEMPFAIL — the stop was not clean, and says so


def main() -> int:
    instrument_loop()
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log("starting %s with engine.stop() wired to SIGINT/SIGTERM" % SERVER)
    try:
        runpy.run_path(SERVER, run_name="__main__")
    except KeyboardInterrupt:
        pass                # werkzeug usually swallows it; belt and braces
    return drain_loop()


if __name__ == "__main__":
    sys.exit(main())
