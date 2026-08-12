#!/usr/bin/env python3
"""Refuse to build an image whose config carries a credential.

THE HOLE THIS CLOSES
--------------------
`trading-bot/config.yaml` ships in the image, and it contains:

    telegram:
      enabled: false          # flip to true after filling token + chat_id
      bot_token: ""
      chat_id: ""

`telegram_notifier.build_notifier()` resolves credentials config-FIRST:

    token = tg.get("bot_token") or ""
    ...
    token = token or storage.get_setting("telegram_bot_token", "")

So the file is not a dead field an operator would ignore — it is the value that
WINS over the dashboard, and its own comment tells them to fill it in. Do that
before `docker compose build` and the token is in a layer. An image layer is
permanent: `docker history` and every registry copy keep it after a later layer
deletes the file. That directly contradicts CLAUDE.md invariant 13 and
CONTRIBUTING.md ("Credentials live in the runtime DB ... never in `config.yaml`,
never in a committed file"), and it contradicts what docs/DEPLOYMENT-LINUX.md
tells the operator the deployment guarantees.

WHY REFUSE RATHER THAN SCRUB
----------------------------
Stripping the value at build time would work, and it would be silent. The
operator would keep a live token in their working copy, believe the documented
posture held, and carry it into the next thing they build by hand. It would
also mean the config.yaml inside the image no longer matches the config.yaml in
the repo — silent drift on the one file whose values are load-bearing, which is
the argument keel-run-dashboard.py makes about rewriting tracked config.

A failed build is loud, is fixed in ten seconds, and leaves the file in the
image byte-identical to the repo. Fail closed: the build stops.

WHAT IT LOOKS FOR
-----------------
1. A credential-shaped KEY with a non-empty value (bot_token, api_key,
   api_secret, password, webhook_url, chat_id, ...).
2. A credential-shaped VALUE anywhere in the line, comments included — a
   Telegram token, a Discord/Slack webhook URL, an AWS access key id, a PEM
   private-key header. This is the half that catches a secret pasted under a
   key nobody thought to enumerate, and the "# my token: 123:AAH..." in a
   comment that a YAML parser would never see. That is why this scans text and
   does not import yaml: a parser sees only the keys, and pyyaml would also be
   a dependency a build-time guard does not need.

It never prints a value it flags — length only. A guard that echoes the
credential into the build log has moved it, not caught it.

Usage:
  keel-config-guard.py                     # scans $KEEL_APP_DIR (or ./trading-bot)
  keel-config-guard.py path [path ...]     # scan specific files or directories
Exit: 0 clean, 1 credential found, 2 nothing to scan.
"""
import os
import re
import sys

DEFAULT_TARGET = os.environ.get("KEEL_APP_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "trading-bot")

SKIP_DIRS = {"data", "state", ".backups", "__pycache__", ".git", "node_modules"}
SCAN_SUFFIXES = (".yaml", ".yml")

# Keys whose value belongs in the runtime DB and nowhere else. Matched as a
# YAML key at any indent: `  bot_token: <something>`.
CREDENTIAL_KEYS = (
    "access_key", "access_token", "api_key", "api_secret", "apikey", "apisecret",
    "auth_token", "bot_token", "chat_id", "client_secret", "credential",
    "discord_webhook", "discord_webhook_url", "login", "passphrase", "passwd",
    "password", "private_key", "secret", "secret_key", "session_token", "token",
    "webhook", "webhook_url",
)
_KEY_RE = re.compile(
    r"^\s*-?\s*(%s)\s*:\s*(.*)$" % "|".join(re.escape(k) for k in CREDENTIAL_KEYS),
    re.IGNORECASE)

# Shapes that are a credential wherever they appear, key or comment.
VALUE_SHAPES = (
    ("telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    ("discord webhook url",
     re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+", re.I)),
    ("slack webhook url", re.compile(r"https://hooks\.slack\.com/\S+", re.I)),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("pem private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def _value_is_empty(raw: str) -> bool:
    """True for the placeholder forms that carry nothing."""
    v = raw.strip()
    if "#" in v:                       # strip a trailing comment
        v = v.split("#", 1)[0].strip()
    return v in ("", '""', "''", "~", "null", "Null", "NULL", "{}", "[]")


def scan_text(text: str, path: str) -> "list[str]":
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()

        if not stripped.startswith("#"):
            m = _KEY_RE.match(line)
            if m and not _value_is_empty(m.group(2)):
                findings.append(
                    "%s:%d: key %r has a non-empty value (%d chars). "
                    "Credentials live in the runtime DB, entered through the "
                    "dashboard - not in a file that becomes an image layer."
                    % (path, n, m.group(1), len(m.group(2).strip())))

        for what, rx in VALUE_SHAPES:
            if rx.search(line):
                findings.append(
                    "%s:%d: this line contains something shaped like a %s. "
                    "Rotate it now - it has been on disk in the build context."
                    % (path, n, what))
    return findings


def _targets(paths: "list[str]") -> "list[str]":
    out = []
    for p in paths:
        if os.path.isfile(p):
            out.append(p)
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in sorted(files):
                if f.endswith(SCAN_SUFFIXES):
                    out.append(os.path.join(root, f))
    return out


def main(argv: "list[str]") -> int:
    paths = argv[1:] or [DEFAULT_TARGET]
    files = _targets(paths)
    if not files:
        print("keel-config-guard: nothing to scan under %s" % ", ".join(paths))
        return 2

    findings = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            # Cannot read it, cannot clear it. Fail closed rather than skip.
            findings.append("%s: unreadable (%s) - cannot clear it" % (f, exc))
            continue
        findings.extend(scan_text(text, f))

    if findings:
        print("=" * 72)
        print("BUILD REFUSED - a credential is present in the build context.")
        print("=" * 72)
        for f in findings:
            print("  !! %s" % f)
        print("")
        print("  Empty the field and put the value in the dashboard instead;")
        print("  the DB is read whenever config.yaml leaves it blank")
        print("  (telegram_notifier.build_notifier). If the value was ever")
        print("  committed or pushed, rotate it: SECURITY.md.")
        return 1

    print("keel-config-guard: %d file(s) scanned, no credential in the image "
          "context" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
