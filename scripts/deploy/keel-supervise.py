#!/usr/bin/env python3
"""PID 1 for a Keel container: turn SIGTERM into a stop the engine survives.

WHY THIS EXISTS
---------------
`server.py` runs Flask in the main thread and starts `engine.engine_loop` as a
*daemon* thread. `engine_loop` is `while True:` with no stop event, and nothing
in the tree installs a SIGTERM handler. So the default disposition applies:
SIGTERM terminates the interpreter immediately, wherever the engine thread
happens to be — possibly between `storage.enqueue_command("close_trade", ...)`
and the ack, or partway through `manage_open_trades`.

Docker sends SIGTERM. Without something in front, every `compose stop`,
`compose down`, host reboot and OOM-kill is that immediate termination. This
process is that something.

WHAT IT ACTUALLY GUARANTEES, AND WHAT IT DOES NOT
-------------------------------------------------
Guaranteed:
  1. Before the child is signalled at all, `halt_new_entries = True` is written
     through `params_store.set_param` (origin "human", audited in
     `param_changes`). The engine reads it at the top of every cycle and
     `try_execute` refuses on it, so from that write onward no NEW position can
     be opened. This is the half that matters: an interrupted management pass
     leaves a stop where it was, an interrupted *entry* leaves a position the
     database may not know about.
  2. The supervisor then waits out the drain window before signalling, so the
     engine observes the halt and completes at least one full cycle under it.
  3. The child is stopped with SIGINT, not SIGTERM. Both `app.run()` (werkzeug)
     and `NewsAgent.run()` unwind on KeyboardInterrupt; neither has a SIGTERM
     handler. SIGINT is the only signal this codebase already knows how to
     answer.
  4. PID 1 duties: zombie reaping, and the child's exit status is this
     process's exit status so restart policies see the truth.

NOT guaranteed, stated plainly: this cannot make an arbitrary mid-cycle stop
safe. `engine_loop` has no stop event, so the supervisor aims for the quiet
part of the cycle (see `_wait_for_quiet_window`) rather than being told the
loop is idle. That is a heuristic. The durable fix is a `threading.Event`
checked by `engine_loop` and joined before the process exits, which lives in
`engine.py` — outside this file's remit. See docs/DEPLOYMENT-LINUX.md §9.

Usage:  keel-supervise.py --role engine -- python server.py
"""
import argparse
import os
import signal
import subprocess
import sys
import time

# The app is importable only from the trading-bot directory; the DB path is
# derived from storage.py's own location, so cwd matters for state/ but not
# for data/.
APP_DIR = os.environ.get("KEEL_APP_DIR", "/app/trading-bot")

DRAIN_ORIGIN = "human"          # the only origin whitelisted for halt_new_entries
_child: "subprocess.Popen | None" = None
_stopping = False


def log(msg: str) -> None:
    print("[supervise] %s" % msg, flush=True)


# ------------------------------------------------------------------ drain
def _poll_seconds() -> int:
    """Engine cycle length, from the DB if set, else config.yaml, else 20."""
    try:
        import yaml
        with open(os.path.join(APP_DIR, "config.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
        return int((cfg.get("engine") or {}).get("poll_seconds", 20))
    except Exception:
        return 20


def _halt_new_entries() -> bool:
    """Close the entry gate through the sanctioned write path.

    Deliberately NOT `storage.set_setting`: CONTRIBUTING.md requires every
    parameter write to carry an origin and a reason, and the refusal or the
    acceptance to land in `param_changes`. An operator reading the audit later
    must be able to see that it was a container stop that halted entries, not
    a strategy decision.
    """
    sys.path.insert(0, APP_DIR)
    try:
        import params_store
        import storage
        storage.init()
        if storage.get_setting("halt_new_entries"):
            log("entries already halted; nothing to drain")
            return True
        params_store.set_param(
            "halt_new_entries", True, origin=DRAIN_ORIGIN,
            reason="container stop drain (keel-supervise)",
            trigger_data={"pid": os.getpid(), "t": int(time.time())})
        log("halt_new_entries=True written and audited")
        return True
    except Exception as exc:                       # noqa: BLE001
        # Fail closed and say so. If the gate could not be closed we still stop
        # the container, but the operator must know the stop was not drained.
        log("DRAIN FAILED (%r) — stopping WITHOUT a closed entry gate" % exc)
        return False


def _wait_for_quiet_window(poll_s: int) -> None:
    """Sleep until the engine is most likely between cycles.

    `engine_loop` writes `engine_heartbeat_t` at the TOP of each cycle, then
    works, then sleeps `poll_seconds`. So a heartbeat that just advanced is the
    WORST moment to signal, and late in the sleep is the best. Watch for the
    heartbeat to advance, then wait most of a cycle.

    If the heartbeat never advances the engine is already dead or wedged, and
    waiting longer buys nothing — bail out after one cycle.
    """
    try:
        import storage
        storage.init()
        start = storage.get_setting("engine_heartbeat_t")
        deadline = time.time() + poll_s + 5
        while time.time() < deadline:
            if storage.get_setting("engine_heartbeat_t") != start:
                # cycle just began: let it finish its work, stop mid-sleep
                time.sleep(max(1.0, poll_s * 0.6))
                log("stopping in the quiet part of the engine cycle")
                return
            time.sleep(0.5)
        log("engine heartbeat did not advance in %ds — stopping anyway" % (poll_s + 5))
    except Exception as exc:                       # noqa: BLE001
        log("quiet-window check unavailable (%r); stopping after a short pause" % exc)
        time.sleep(2)


# ------------------------------------------------------------------ signals
def _terminate(signum, _frame) -> None:
    global _stopping
    if _stopping:
        return                                     # second signal: let it ride
    _stopping = True
    log("received %s" % signal.Signals(signum).name)

    if _child is None or _child.poll() is not None:
        sys.exit(0)

    if os.environ.get("KEEL_DRAIN_ON_STOP", "1") == "1":
        if _halt_new_entries():
            _wait_for_quiet_window(_poll_seconds())

    # SIGINT, because that is the one this codebase handles. werkzeug unwinds
    # its serve loop; news_agent.run() breaks out of `while True`.
    log("sending SIGINT to child pid %d" % _child.pid)
    try:
        _child.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return

    grace = int(os.environ.get("KEEL_CHILD_GRACE_S", "20"))
    try:
        _child.wait(timeout=grace)
        log("child exited cleanly")
        return
    except subprocess.TimeoutExpired:
        pass

    log("child ignored SIGINT after %ds — escalating to SIGTERM" % grace)
    _child.terminate()
    try:
        _child.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass

    log("child still alive — SIGKILL")
    _child.kill()


def main() -> int:
    global _child
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", required=True,
                    choices=("engine", "dashboard", "newsagent"),
                    help="which Keel process this container hosts")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- followed by the command to run")
    args = ap.parse_args()

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        ap.error("no command given (use: --role engine -- python server.py)")

    # Only the engine container owns the entry gate. Draining from the
    # dashboard or the news agent would halt a *running* engine every time an
    # unrelated container restarted.
    if args.role != "engine":
        os.environ["KEEL_DRAIN_ON_STOP"] = "0"

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    log("role=%s cmd=%s" % (args.role, " ".join(cmd)))
    _child = subprocess.Popen(cmd, cwd=APP_DIR)

    while True:
        try:
            return _child.wait()
        except KeyboardInterrupt:
            # handler already ran; keep waiting for the child to go
            continue


if __name__ == "__main__":
    sys.exit(main())
