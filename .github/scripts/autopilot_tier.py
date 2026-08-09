"""Decide how far an autopilot change may travel, by reading the diff.

Ported from the Lakeside store's autopilot with one structural change: this
system places trades, so there is no auto-merge tier at all. The model opens a
draft pull request and stops. A person merges. Always.

What this script still decides is whether the change was even a legitimate thing
for a robot to attempt. A ticket that says "loosen the daily stop" is a request
a human must handle, and the reliable place to catch it is here, in the diff,
not in the model's judgement about its own work. A judgement the model makes
about what it just wrote is one it can talk itself into.

Exit 0 always: this reports, it does not fail the build. The verdict goes to the
pull request and to the ticket comment.
"""
import json
import os
import re
import subprocess
import sys

# Touching any of these means a human reads the diff before it can merge, and the
# PR is labelled so it cannot be skimmed. Same list as CONTRIBUTING.md and
# .github/workflows/guard.yml -- three places, one meaning, kept in step.
RAILS = re.compile(
    r"^(trading-bot/(engine|params_store|live_switch|analysis|storage)\.py"
    r"|trading-bot/brokers/.*\.py"
    r"|SLCDataBridge\.mq5"
    r"|CLAUDE\.md)$")

# Changes a robot must not make unattended at all, regardless of what a ticket
# asked for. Each of these is a request to weaken a rail, and a ticket asking
# for one is a conversation, not a task.
FORBIDDEN = [
    (re.compile(r"^\-.*RISK_PCT_CEILING\s*=\s*1\.0", re.M), "raises the risk ceiling"),
    (re.compile(r"^\+.*RISK_PCT_CEILING\s*=\s*(?!1\.0)", re.M), "raises the risk ceiling"),
    (re.compile(r"^\+.*DAILY_STOP_CEILING\s*=\s*(?!5\.0)", re.M), "moves the daily-stop ceiling"),
    (re.compile(r"^\+.*trading_mode.*[\"']live[\"']", re.M), "writes trading_mode = live"),
    (re.compile(r"^\+.*AllowTradeExecution\s*=\s*true", re.M), "arms EA trade execution"),
    (re.compile(r"^\-.*\"agent\":\s*\{", re.M), "edits the agent parameter whitelist"),
    (re.compile(r"^\+.*read_only\s*=\s*False", re.M), "arms a venue for execution"),
]


def diff_against(base: str) -> str:
    return subprocess.run(["git", "diff", base + "...HEAD"],
                          capture_output=True, text=True).stdout


def changed_files(base: str) -> list:
    out = subprocess.run(["git", "diff", "--name-only", base + "...HEAD"],
                         capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f.strip()]


def main() -> int:
    base = os.environ.get("BASE_REF", "origin/main")
    files = changed_files(base)
    diff = diff_against(base)

    rails_touched = [f for f in files if RAILS.match(f)]
    forbidden = [why for pat, why in FORBIDDEN if pat.search(diff)]
    tests_touched = any(f.startswith("trading-bot/tests/") for f in files)

    if forbidden:
        verdict, reason = "refuse", (
            "This change would %s. A robot does not get to make that change, and a "
            "ticket asking for it needs a person." % "; ".join(sorted(set(forbidden))))
    elif rails_touched and not tests_touched:
        verdict, reason = "human-required", (
            "Touches a safety rail (%s) with no test change. CONTRIBUTING.md asks that "
            "a fix ship with the test that would have caught the bug."
            % ", ".join(rails_touched))
    elif rails_touched:
        verdict, reason = "human-required", (
            "Touches a safety rail (%s). Tests changed too, which is right, but a rail "
            "change is read by a person before it merges." % ", ".join(rails_touched))
    else:
        verdict, reason = "review-normal", (
            "No rail files touched. Normal review: draft PR, human merges."
            " Autopilot never merges anything here.")

    result = {"verdict": verdict, "reason": reason,
              "rails_touched": rails_touched, "forbidden": sorted(set(forbidden)),
              "tests_touched": tests_touched, "files": len(files)}

    print(json.dumps(result, indent=2))
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write("verdict=%s\n" % verdict)
            f.write("reason=%s\n" % reason.replace("\n", " "))
            f.write("rails=%s\n" % ",".join(rails_touched))
    return 0


if __name__ == "__main__":
    sys.exit(main())
