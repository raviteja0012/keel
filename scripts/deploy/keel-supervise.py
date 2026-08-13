#!/usr/bin/env python3
"""PID 1 for a Keel container: turn SIGTERM into a stop the engine survives.

WHY THIS EXISTS
---------------
Docker stops a container with SIGTERM, and nothing in the application tree
installs a SIGTERM handler. The default disposition therefore applies: the
interpreter dies immediately, wherever the engine thread happens to be —
possibly between `storage.enqueue_command("close_trade", ...)` and the ack, or
partway through `manage_open_trades`. Every `compose stop`, host reboot and
OOM-kill is that immediate termination. This process is what stands in front
of it.

THE STOP, IN ORDER
------------------
  1. `halt_new_entries = True` through `params_store.set_param` (origin
     "human", audited in `param_changes`) BEFORE the child is signalled at all.
     This is the half that matters: an interrupted management pass leaves a
     stop where it was, an interrupted *entry* leaves a position the database
     may not know about.
  2. SIGINT to the child — not SIGTERM. Both `app.run()` (werkzeug) and
     `NewsAgent.run()` unwind on KeyboardInterrupt; neither has a SIGTERM
     handler.
  3. The child does the deterministic part itself. For the engine role the
     child is `keel-run-engine.py`, which calls `engine.stop()` — the
     `threading.Event` `engine_loop` checks — and does not let the interpreter
     exit until the loop has left the cycle.
  4. PID 1 duties: zombie reaping, and the child's exit status becomes this
     process's exit status so restart policies see the truth.

THE START, AND WHY IT CAN CLOSE THE ENTRY GATE
----------------------------------------------
`restart: unless-stopped` also resumes after a stop that never reached step 1:
a segfault, an OOM kill, `docker kill`, a host that lost power. Those bypass
the handler entirely, so the container comes back with `halt_new_entries`
exactly as open as it was — the engine resumes taking new positions after an
event nobody has looked at yet.

So this process records `keel_supervisor_run_state = running` in the database
before it launches the engine, and rewrites it to `stopped_drained` only when a
drained stop completes. A start that finds `running` still there knows the
previous run died without draining, and closes the entry gate before the child
starts. Open positions still get managed; nothing NEW is opened until a human
runs `keel-resume.sh`. The stops you cannot explain are exactly the ones that
must not resume unattended.

That marker is supervisor bookkeeping, like `engine_heartbeat_t`, so it is a
plain `storage.set_setting`. The entry gate itself is a risk parameter and
still goes through `params_store` with an origin and a reason — nothing here
writes `halt_new_entries` directly.

WHAT CHANGED, AND WHY THE OLD SHAPE WAS WRONG
---------------------------------------------
This file used to do all of the stop work INSIDE the signal handler while
`main()` sat in `_child.wait()` with no timeout. On POSIX that call holds
`Popen._waitpid_lock` across a blocking `waitpid()`, and CPython installs
handlers without SA_RESTART, so the Python handler runs from inside the EINTR
retry — with the lock held. The handler's own `_child.wait(timeout=...)` takes
the busy-loop path, which only ever tries `self._waitpid_lock.acquire(False)`;
a non-reentrant lock already held by this same thread never yields. Every stop
therefore ran the whole grace window out to `TimeoutExpired` and escalated, on
a child that had already exited cleanly: a guaranteed extra 30s, and two log
lines ("child ignored SIGINT ... escalating", "child still alive — SIGKILL")
that were simply false. That log is the one an operator reads after an
unexplained stop, so the cost was never only the delay.

The handler now records the signal number and returns. Every wait in this file
carries a timeout, so no path can hold that lock across a blocking call, and
the stop sequence runs on the main loop with nothing held.

The old code also timed its SIGINT off `engine_heartbeat_t`, sleeping ~60% of a
poll interval to aim for the gap between cycles, because `engine_loop` had no
stop event when this file was written. It has one now, so the guess is gone.

Usage:  keel-supervise.py --role engine -- python /app/bin/keel-run-engine.py
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from typing import Optional

# The app is importable only from the trading-bot directory; the DB path is
# derived from storage.py's own location, so cwd matters for state/ but not
# for data/.
APP_DIR = os.environ.get("KEEL_APP_DIR", "/app/trading-bot")

DRAIN_ORIGIN = "human"          # the only origin whitelisted for halt_new_entries

# Run-state marker. Written before the child starts, rewritten only by a
# completed drain, so anything that kills this process leaves RUNNING behind
# as the evidence that the next start must not trust the entry gate.
RUN_STATE_KEY = "keel_supervisor_run_state"
RUN_STATE_RUNNING = "running"
RUN_STATE_DRAINED = "stopped_drained"

# The child command that means "this container is hosting the long-running
# engine". A `docker compose run --rm engine python tests/...` shares the role
# and the entrypoint but is not a service start: it must neither trip the
# startup gate nor clear the marker a real engine left behind.
SERVICE_CMD_MARKER = "keel-run-engine.py"

# How often main() looks in on the child. Every wait in this file has a
# timeout; the module docstring says what a wait without one costs.
POLL_S = 0.2

_child: "subprocess.Popen | None" = None
_stop_signum: "int | None" = None      # written by the handler, read by main()


def log(msg: str) -> None:
    print("[supervise] %s" % msg, flush=True)


# ------------------------------------------------------------------ app access
def _app_modules():
    """Import the application's storage/params_store against APP_DIR."""
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    import params_store
    import storage
    storage.init()
    return params_store, storage


def _poll_seconds() -> int:
    """Engine cycle length, from config.yaml, else 20."""
    try:
        import yaml
        with open(os.path.join(APP_DIR, "config.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
        return int((cfg.get("engine") or {}).get("poll_seconds", 20))
    except Exception:
        return 20


# ------------------------------------------------------------------ drain
def _halt_new_entries(reason: str = "container stop drain (keel-supervise)") -> bool:
    """Close the entry gate through the sanctioned write path.

    Deliberately NOT `storage.set_setting`: CONTRIBUTING.md requires every
    parameter write to carry an origin and a reason, and the refusal or the
    acceptance to land in `param_changes`. An operator reading the audit later
    must be able to see that it was a container stop that halted entries, not
    a strategy decision.
    """
    try:
        params_store, storage = _app_modules()
        if storage.get_setting("halt_new_entries"):
            log("entries already halted; nothing to drain")
            return True
        params_store.set_param(
            "halt_new_entries", True, origin=DRAIN_ORIGIN, reason=reason,
            trigger_data={"pid": os.getpid(), "t": int(time.time())})
        log("halt_new_entries=True written and audited")
        return True
    except Exception as exc:                       # noqa: BLE001
        # Fail closed and say so. If the gate could not be closed we still stop
        # the container, but the operator must know the stop was not drained.
        log("DRAIN FAILED (%r) — stopping WITHOUT a closed entry gate" % exc)
        return False


# ------------------------------------------------------------------ run state
def _run_state_get() -> "str | None":
    _, storage = _app_modules()
    v = storage.get_setting(RUN_STATE_KEY)
    return None if v is None else str(v)


def _run_state_set(value: str) -> None:
    _, storage = _app_modules()
    storage.set_setting(RUN_STATE_KEY, value)


def _run_state_set_quietly(value: str) -> None:
    """Best effort, on the way out. A failure here cannot be allowed to change
    the exit status the restart policy reads, and it fails SAFE: an unwritten
    `stopped_drained` makes the next start treat this stop as unexplained and
    close the entry gate, which is the conservative direction."""
    try:
        _run_state_set(value)
    except Exception as exc:                       # noqa: BLE001
        log("could not record run state %r (%r); next start will treat this "
            "stop as unexplained and halt entries" % (value, exc))


def startup_gate_armed(role: str, cmd: "list[str]") -> bool:
    override = os.environ.get("KEEL_STARTUP_GATE")
    if override is not None:
        return override == "1"
    return role == "engine" and any(SERVICE_CMD_MARKER in c for c in cmd)


def startup_entry_gate() -> str:
    """Close the entry gate if the previous run did not stop through the drain.

    Returns "first-start", "clean" or "halted". Raises if the database cannot
    be read or the gate cannot be closed — main() turns that into a refusal to
    start, because a start with an unknown entry-gate state can open positions
    nobody decided to open.
    """
    prev = _run_state_get()

    if prev == RUN_STATE_RUNNING:
        log("previous run ended WITHOUT a drain (run state was still %r)."
            % RUN_STATE_RUNNING)
        log("segfault, OOM kill, `docker kill` or a host that lost power — the")
        log("handler never ran, so the entry gate is however the crash left it.")
        if not _halt_new_entries(
                "undrained previous run: entries halted at container start "
                "(keel-supervise startup gate)"):
            raise RuntimeError(
                "could not close the entry gate after an undrained stop")
        log("entries are HALTED. Positions are still managed. Resume with "
            "scripts/deploy/keel-resume.sh once you know what happened.")
        outcome = "halted"
    elif prev is None:
        log("no previous run state: first start against this database")
        outcome = "first-start"
    else:
        log("previous run stopped through the drain (run state %r)" % prev)
        outcome = "clean"

    _run_state_set(RUN_STATE_RUNNING)
    return outcome


# ------------------------------------------------------------------ signals
def _on_signal(signum, _frame) -> None:
    """Record the signal and get out.

    This can run on the main thread from inside a `Popen` internal that holds
    `_waitpid_lock`, so it must touch no lock and call no method on `_child`.
    Assigning one module global is the entire job; main() does the work.
    """
    global _stop_signum
    if _stop_signum is None:
        _stop_signum = signum
    # A second signal is deliberately ignored: the stop sequence is already
    # running and has its own escalation ladder.


# ------------------------------------------------------------------ stopping
def _child_grace() -> int:
    """How long the child gets after SIGINT, before escalation.

    Long enough for the child's own deterministic stop. `engine.stop()` sets an
    event that `_stop.wait(poll_seconds)` returns from immediately, so an idle
    loop exits at once; a loop mid-cycle has to finish its venue calls first.
    Derived from the poll interval rather than a flat 20s, so raising
    `poll_seconds` cannot quietly start truncating stops.
    """
    env = os.environ.get("KEEL_CHILD_GRACE_S")
    if env:
        return max(1, int(env))
    return max(20, _poll_seconds() + 10)


def _exit_code(rc: "int | None") -> int:
    """A child killed by signal N reports -N; a process exit status cannot.
    128+N is the shell convention and is what a restart policy should see."""
    if rc is None:
        return 0
    return rc if rc >= 0 else 128 - rc


def _await_child(grace: int) -> None:
    """Wait out the child, escalating only on evidence that it is still there."""
    try:
        _child.wait(timeout=grace)
        log("child exited cleanly (status %s)" % _child.returncode)
        return
    except subprocess.TimeoutExpired:
        pass

    # Note the wording: it did not exit in time. Whether it "ignored" the
    # signal is not something this process can know, and the old text claiming
    # it did was false on every single stop.
    log("child has not exited %ds after SIGINT — escalating to SIGTERM" % grace)
    _child.terminate()
    try:
        _child.wait(timeout=10)
        log("child exited on SIGTERM (status %s)" % _child.returncode)
        return
    except subprocess.TimeoutExpired:
        pass

    log("child still alive — SIGKILL")
    _child.kill()
    try:
        _child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log("child survived SIGKILL; leaving it to the kernel")


def _await_halt_ack(timeout_s: Optional[float] = None) -> bool:
    """Wait for the engine to acknowledge the closed entry gate.

    engine_loop writes `halt_ack_t` at the top of any cycle that observes
    halt_new_entries=True, AFTER its params() read. An ack newer than our write
    is therefore proof that a cycle has actually seen the gate — which is the
    thing the drain needs and the gate write alone does not give.

    Returns True if acknowledged, False on timeout. False means the stop is
    NOT drained: it is reported that way and the run state is left undrained, so
    the next start closes the gate itself rather than resuming into the unknown.
    A drain that cannot be confirmed is not a drain.
    """
    if timeout_s is None:
        timeout_s = float(os.environ.get("KEEL_HALT_ACK_S")
                          or max(10, _poll_seconds() * 2 + 5))
    try:
        _params_store, storage = _app_modules()
    except Exception as exc:                        # noqa: BLE001
        log("cannot confirm the drain (%r) — treating the stop as undrained" % exc)
        return False

    t0 = time.time()

    # An engine that is not cycling cannot open a position, so there is nothing
    # to wait for and no ack will ever come. Waiting anyway would burn the whole
    # timeout on every stop of an already-dead or never-started engine, which is
    # the common case for a container that failed at boot. Heartbeat older than
    # a couple of poll intervals, or absent entirely, means not running.
    stale_after = max(10.0, _poll_seconds() * 3.0)
    try:
        hb = float(storage.get_setting("engine_heartbeat_t", 0) or 0)
    except Exception:
        hb = 0.0
    if hb <= 0 or (t0 - hb) > stale_after:
        log("engine is not cycling (heartbeat %s) — nothing can open a position, "
            "so no ack is needed"
            % ("never written" if hb <= 0 else "%.0fs old" % (t0 - hb)))
        return True

    deadline = t0 + timeout_s
    while time.time() < deadline:
        if _child.poll() is not None:
            log("child exited while waiting for the halt ack")
            return True                 # nothing left that could open a position
        try:
            ack = float(storage.get_setting("halt_ack_t", 0) or 0)
        except Exception:
            ack = 0.0
        if ack >= t0:
            log("engine acknowledged the entry gate after %.1fs" % (time.time() - t0))
            return True
        time.sleep(0.25)

    log("engine did NOT acknowledge the entry gate within %.0fs — a cycle may "
        "still have been mid-flight, so this stop is NOT drained" % timeout_s)
    return False


def stop_child(signum: int, gate_armed: bool) -> int:
    """The stop sequence. Runs on the main loop, holding nothing."""
    log("received %s" % signal.Signals(signum).name)

    already_gone = _child.poll() is not None
    drained = True

    if os.environ.get("KEEL_DRAIN_ON_STOP", "1") == "1":
        drained = _halt_new_entries()
        # Writing the gate is not the same as the engine having SEEN it. A cycle
        # already past its params() read does not observe the halt, so signalling
        # straight after the write leaves a window where a position can still be
        # opened AFTER the drain was written and audited — the stop then files as
        # clean when it was not. Wait, bounded, for the engine to acknowledge.
        if drained and not already_gone:
            drained = _await_halt_ack()

    if already_gone:
        log("child had already exited (status %s)" % _child.returncode)
    else:
        log("sending SIGINT to child pid %d" % _child.pid)
        try:
            _child.send_signal(signal.SIGINT)
        except ProcessLookupError:
            log("child was gone before the signal landed")
        else:
            _await_child(_child_grace())

    # Only a stop that actually closed the entry gate counts as explained.
    if gate_armed and drained and not already_gone:
        _run_state_set_quietly(RUN_STATE_DRAINED)

    return _exit_code(_child.returncode)


def supervise(gate_armed: bool) -> int:
    """Wait for the child or for a stop signal, whichever comes first."""
    while _stop_signum is None:
        try:
            rc = _child.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            continue

        # Nothing drained this: the child went on its own. A crash, an OOM kill
        # of the child alone, or a clean exit that nobody asked for — all three
        # are the unexplained stop that `restart: unless-stopped` resumes from.
        log("child exited on its own with status %s (nothing drained it)" % rc)
        if gate_armed:
            _halt_new_entries(
                "engine process exited unexpectedly with status %s "
                "(keel-supervise)" % rc)
        return _exit_code(rc)

    return stop_child(_stop_signum, gate_armed)


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

    gate_armed = startup_gate_armed(args.role, cmd)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if gate_armed:
        try:
            startup_entry_gate()
        except Exception as exc:                   # noqa: BLE001
            log("STARTUP GATE FAILED (%r)" % exc)
            log("Cannot establish whether the previous stop drained, and cannot")
            log("close the entry gate. REFUSING TO START the engine: a start")
            log("with an unknown entry-gate state can open positions nobody")
            log("decided to open. Repair the database (docs/DEPLOYMENT-LINUX.md")
            log("§7 restore, §11 troubleshooting), or set KEEL_STARTUP_GATE=0")
            log("after checking halt_new_entries by hand.")
            return 3

    log("role=%s cmd=%s" % (args.role, " ".join(cmd)))
    _child = subprocess.Popen(cmd, cwd=APP_DIR)

    return supervise(gate_armed)


if __name__ == "__main__":
    sys.exit(main())
