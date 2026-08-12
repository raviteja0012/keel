"""Deployment-asset tests — the container equivalent of the risk rails.

Two of the things this file checks are money-safety properties, not hygiene:

  * `.dockerignore` must keep trading-bot/data/ out of the build context.
    trading.db's `settings` table holds venue api_key/api_secret/password in
    cleartext (venues.py _SECRET_FIELDS), and an image layer is permanent —
    `docker history` and any registry copy keep the file after a later layer
    deletes it. This is the exact defect ARCHITECTURE-V3 §3 calls "the sharpest
    security observation in the document".
  * docker-compose.yml must not publish any port on 0.0.0.0. Port 8767 is the
    control plane that live_switch mounts on; port 8766 serves
    `POST /api/commands`, which authenticates nothing.

Both are one careless character away at all times — `"8767:8767"` instead of
`"127.0.0.1:8767:8767"` — and neither fails visibly. A rail that depends on
review is not a rail (CONTRIBUTING.md), so it is asserted here.

WHERE THIS LIVES, AND WHY NOT IN trading-bot/tests/
---------------------------------------------------
House rules put every test in trading-bot/tests/. This one sits beside the
files it tests because the deployment assets are owned and changed as a unit,
and a test in a directory this change does not own would be dropped on merge.
It follows the trading-bot/tests/ harness exactly — plain script, no network,
no venue, no MT5, no Docker daemon required, prints "N passed, M failed", exits
non-zero on failure — so it can be moved under trading-bot/tests/ unchanged if
that is preferred.

Run:  python scripts/deploy/test_deploy_assets.py
"""
import os
import py_compile
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(REPO, "scripts", "deploy")


class Skip(Exception):
    """A check that cannot run here. Reported separately, never as a pass."""


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


DOCKERFILE = _read("Dockerfile")
DOCKERIGNORE = _read(".dockerignore")
COMPOSE = yaml.safe_load(_read("docker-compose.yml"))
COMPOSE_RAW = _read("docker-compose.yml")


# ============================================================ .dockerignore
# A re-implementation of Docker's ignore matching, so these assertions test
# BEHAVIOUR ("is data/trading.db in the context?") rather than the presence of
# a line of text that a later edit could negate two lines further down.
# Docker semantics: patterns are matched against the cleaned relative path,
# `*` and `?` do not cross `/`, `**` crosses any number of segments, a leading
# `!` re-includes, and the LAST matching pattern wins.
def _pattern_to_regex(pat):
    i, n, out = 0, len(pat), ["^"]
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                i += 2
                if i < n and pat[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")     # **/ matches zero or more dirs
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("(?:/.*)?$")                    # a dir match covers its contents
    return re.compile("".join(out))


def _ignored(path):
    """True if `path` (repo-relative, / separators) is excluded from the context."""
    excluded = False
    for raw in DOCKERIGNORE.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = line[1:].strip() if negate else line
        pat = pat.strip("/")
        if _pattern_to_regex(pat).match(path):
            excluded = not negate
    return excluded


# Anything on this list reaching a layer is a credential disclosure or a
# corruption vector. Each entry is a real path in this repo's shape.
MUST_BE_IGNORED = [
    "trading-bot/data",
    "trading-bot/data/trading.db",
    "trading-bot/data/trading.db-wal",
    "trading-bot/data/trading.db-shm",
    "trading-bot/state",
    "trading-bot/state/dashboard_token",
    "trading-bot/state/news_agent.log",
    "trading-bot/state/news_decisions.jsonl",
    "trading-bot/.backups",
    "trading-bot/.backups/engine.py.2026",
    ".venv",
    ".venv/Scripts/python.exe",
    ".git",
    ".git/config",
    ".env",
    "trading-bot/.env",
    "secrets.yaml",
    "hallucination_check.jsonl",
    "legacy",
    "legacy/pattern-strategy-fastapi/trading-bot/data/trading.db",
    "trading-bot/__pycache__/engine.cpython-312.pyc",
    "SLCDataBridge.mq5",
]

# ...and the image is useless without these.
MUST_BE_PRESENT = [
    "trading-bot/server.py",
    "trading-bot/engine.py",
    "trading-bot/storage.py",
    "trading-bot/params_store.py",
    "trading-bot/dashboard_api.py",
    "trading-bot/dash_auth.py",
    "trading-bot/news_agent.py",
    "trading-bot/config.yaml",
    "trading-bot/requirements.txt",
    "trading-bot/brokers/ccxt_venue.py",
    "trading-bot/strategies/__init__.py",
    "trading-bot/dashboard/multiasset.html",
    "trading-bot/tests/test_risk_rails.py",
]


def test_dockerignore_excludes_every_credential_path():
    leaked = [p for p in MUST_BE_IGNORED if not _ignored(p)]
    assert not leaked, ("these would enter an image layer: %s" % leaked)


def test_dockerignore_keeps_the_application():
    missing = [p for p in MUST_BE_PRESENT if _ignored(p)]
    assert not missing, ("the image would not contain: %s" % missing)


def test_every_dockerfile_copy_source_survives_the_ignore_file():
    """Deny-all is safe but it is also easy to over-deny.

    A COPY whose source was filtered out of the context fails the build with
    "not found", and the two files are edited independently. This walks the
    Dockerfile's own COPY sources and checks each one is actually reachable —
    so adding a COPY without allowing its path fails here rather than on the
    trading host at deploy time.
    """
    # Join backslash continuations first: a multi-source COPY is usually written
    # across several lines, and checking only the first one would miss most of
    # its sources.
    joined = re.sub(r"\\\s*\n\s*", " ", _strip_comments(DOCKERFILE))
    missing = []
    for line in joined.splitlines():
        m = re.match(r"^\s*COPY\s+(.*)$", line)
        if not m:
            continue
        # `COPY --from=<stage>` reads a previous build stage, not the context,
        # so the ignore file does not apply to it.
        if "--from=" in m.group(1):
            continue
        parts = [p for p in m.group(1).split() if not p.startswith("--")]
        if len(parts) < 2:
            continue
        for src in parts[:-1]:                     # last token is the destination
            src = src.strip().strip("\\").rstrip("/")
            if not src:
                continue
            if _ignored(src):
                missing.append(src)
            elif not os.path.exists(os.path.join(REPO, src)):
                missing.append("%s (does not exist)" % src)
    assert not missing, ("Dockerfile COPY sources not in the build context: %s"
                         % missing)


def test_dockerignore_is_deny_all_first():
    lines = [l.strip() for l in DOCKERIGNORE.splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert lines and lines[0] == "*", (
        "first effective pattern must be '*' (deny-all, then allow) so a "
        "secret added to the repo later cannot enter the context by default; "
        "got %r" % (lines[0] if lines else None))


# ============================================================ compose: ports
def _published(svc):
    out = []
    for p in svc.get("ports") or []:
        out.append(p if isinstance(p, str) else str(p.get("published", "")))
    return out


def test_no_port_is_published_beyond_host_loopback():
    """The single most expensive mistake available in this file.

    8767 is the control plane; 8766 accepts unauthenticated close_trade. A
    published port with no host-IP prefix means 0.0.0.0 — the whole internet.
    """
    bad = []
    for name, svc in COMPOSE["services"].items():
        for spec in _published(svc):
            if not (spec.startswith("127.0.0.1:") or spec.startswith("[::1]:")):
                bad.append("%s -> %s" % (name, spec))
    assert not bad, (
        "published on every interface: %s. Use '127.0.0.1:HOST:CTR' and reach "
        "it over an SSH tunnel or a tailnet." % bad)


def test_no_service_shares_the_host_network():
    bad = [n for n, s in COMPOSE["services"].items()
           if s.get("network_mode") == "host"]
    assert not bad, ("network_mode: host bypasses port publication entirely, "
                     "so the loopback bindings stop applying: %s" % bad)


def test_compose_has_no_literal_wildcard_bind():
    """Catches a 0.0.0.0 publish written in a form the parser above would miss
    (long syntax, a comment someone uncommented, an override pasted in)."""
    for m in re.finditer(r'^\s*-\s*"?(0\.0\.0\.0|\*):\d+:', COMPOSE_RAW, re.M):
        raise AssertionError("explicit wildcard publish: %r" % m.group(0).strip())


# ============================================================ compose: state
def test_data_and_state_are_named_volumes_on_the_local_driver():
    vols = COMPOSE.get("volumes") or {}
    for want in ("keel_data", "keel_state"):
        assert want in vols, "missing named volume %r" % want
        drv = (vols[want] or {}).get("driver", "local")
        assert drv == "local", (
            "%s uses driver %r. SQLite WAL needs POSIX advisory locks and a "
            "shared-memory index; on NFS/CIFS/virtiofs they silently stop "
            "meaning anything." % (want, drv))


def test_engine_and_dashboard_mount_both_volumes():
    for name in ("engine", "dashboard"):
        mounts = " ".join(COMPOSE["services"][name].get("volumes") or [])
        assert "keel_data:/app/trading-bot/data" in mounts, \
            "%s does not mount the database volume" % name
        assert "keel_state:/app/trading-bot/state" in mounts, \
            "%s does not mount the state volume" % name


def test_newsagent_cannot_reach_the_credential_database():
    """news_agent.py does not import storage; it reaches the engine over HTTP.

    It is also the process that parses arbitrary RSS from the open internet, so
    it is the last one that should be able to open the file holding the venue
    API keys. Least privilege, enforced by the mount list.
    """
    mounts = " ".join(COMPOSE["services"]["newsagent"].get("volumes") or [])
    assert "keel_data" not in mounts, (
        "the news agent has been given the database volume; it has no reason "
        "to hold the venue credentials")


def test_no_bind_mount_of_data_or_state():
    """A host path here is how the volume ends up on a Storage Box or an sshfs
    mount, which is the WAL-corruption path."""
    for name, svc in COMPOSE["services"].items():
        for v in svc.get("volumes") or []:
            spec = v if isinstance(v, str) else v.get("source", "")
            if ("/app/trading-bot/data" in spec or "/app/trading-bot/state" in spec):
                src = spec.split(":")[0]
                assert not (src.startswith("/") or src.startswith(".") or ":" in src[:3]), \
                    "%s bind-mounts %r; use a named volume" % (name, src)


# ============================================================ compose: stop
def _seconds(v):
    m = re.match(r"^(\d+)\s*([smh]?)$", str(v).strip())
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def test_engine_stop_grace_outlasts_the_drain():
    """Docker SIGKILLs at the end of stop_grace_period.

    The supervisor's stop path is: write halt_new_entries, wait up to one poll
    for the engine to observe it, wait ~0.6 of a cycle for the quiet window,
    then SIGINT and allow KEEL_CHILD_GRACE_S. At the default 20s poll that is
    roughly 20 + 12 + 20 = 52s. A grace period below that reintroduces exactly
    the mid-position kill the supervisor exists to prevent.
    """
    grace = _seconds(COMPOSE["services"]["engine"].get("stop_grace_period", "10s"))
    assert grace is not None and grace >= 60, (
        "engine stop_grace_period is %ss; needs >= 60s to cover the drain"
        % grace)


def test_every_service_restarts_and_is_health_checked():
    for name, svc in COMPOSE["services"].items():
        assert svc.get("restart") in ("unless-stopped", "always"), \
            "%s has restart=%r" % (name, svc.get("restart"))
        assert svc.get("healthcheck"), "%s has no healthcheck" % name


def test_every_service_has_resource_limits():
    for name, svc in COMPOSE["services"].items():
        lim = (((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {})
        assert lim.get("memory"), "%s has no memory limit" % name
        assert lim.get("cpus"), "%s has no cpu limit" % name


def test_engine_is_the_last_process_the_oom_killer_takes():
    eng = COMPOSE["services"]["engine"].get("oom_score_adj")
    news = COMPOSE["services"]["newsagent"].get("oom_score_adj")
    assert eng is not None and news is not None and eng < news, (
        "under memory pressure the kernel must take the news agent before the "
        "process holding open positions (engine=%r newsagent=%r)" % (eng, news))


def test_no_service_holds_a_credential_in_source():
    """docker-compose.yml is committed. Credentials live in the runtime DB."""
    banned = ("DASHBOARD_TOKEN", "API_KEY", "API_SECRET", "BOT_TOKEN",
              "TELEGRAM", "DISCORD", "WEBHOOK")
    for name, svc in COMPOSE["services"].items():
        for k in (svc.get("environment") or {}):
            assert not any(b in k.upper() for b in banned), \
                "%s sets %s in a committed file" % (name, k)


# ============================================================ Dockerfile
def test_final_stage_runs_as_a_non_root_user():
    users = re.findall(r"^USER\s+(\S+)", DOCKERFILE, re.M)
    assert users, "no USER directive: the container would run as root"
    assert users[-1] not in ("root", "0", "0:0"), "final USER is %r" % users[-1]


def _strip_comments(text):
    """Dockerfile/# comments removed, so a line explaining that a toolchain is
    absent is not mistaken for one installing it."""
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))


def test_build_toolchain_does_not_reach_the_final_image():
    stages = re.split(r"^FROM ", DOCKERFILE, flags=re.M)[1:]
    assert len(stages) >= 2, "not a multi-stage build"
    final = _strip_comments(stages[-1])
    for tool in ("build-essential", "gcc", "g++", "make"):
        assert not re.search(r"\b%s\b" % re.escape(tool), final), \
            "%r installed in the final stage" % tool
    # ...and the venv must arrive by copy from the builder, not by reinstalling.
    assert "--from=builder" in final, \
        "final stage does not copy the prebuilt venv from the builder stage"


def test_image_declares_healthcheck_and_supervised_entrypoint():
    assert "HEALTHCHECK" in DOCKERFILE, "no HEALTHCHECK"
    assert "keel-supervise.py" in DOCKERFILE, \
        "ENTRYPOINT must be the supervisor: nothing in the app installs a " \
        "SIGTERM handler, and engine_loop is a daemon thread with no stop event"
    assert re.search(r"^WORKDIR\s+/app/trading-bot", DOCKERFILE, re.M), \
        "cwd must be trading-bot/: state/ is written with relative paths"


def test_sqlite3_present_so_invariant_7_recovery_is_not_dead_code():
    """storage._attempt_recover() shells out to `sqlite3` and returns False
    without it, which disables the recovery arm of CLAUDE.md invariant 7."""
    assert re.search(r"install[^\n]*sqlite3", DOCKERFILE), \
        "sqlite3 is not installed in the runtime stage"


def test_dependencies_are_pinned():
    assert "constraints.txt" in DOCKERFILE, "build does not use constraints.txt"
    path = os.path.join(DEPLOY, "constraints.txt")
    assert os.path.exists(path), "scripts/deploy/constraints.txt is missing"
    pins = [l for l in open(path, encoding="utf-8").read().splitlines()
            if l.strip() and not l.startswith("#")]
    assert len(pins) >= 10, "constraints.txt has only %d pins" % len(pins)
    unpinned = [l for l in pins if "==" not in l]
    assert not unpinned, "not pinned with ==: %s" % unpinned
    assert any(l.startswith("ccxt==") for l in pins), \
        "ccxt is unpinned; two nodes would run different venue code"


# ============================================================ scripts
def test_deploy_scripts_compile():
    for f in ("keel-supervise.py", "keel-healthcheck.py", "keel-run-dashboard.py",
              "keel-run-engine.py", "keel-config-guard.py"):
        py_compile.compile(os.path.join(DEPLOY, f), doraise=True)


def test_supervisor_halts_through_params_store_not_storage():
    """CONTRIBUTING.md: every parameter write carries an origin and a reason and
    lands in param_changes. A drain that used storage.set_setting would close
    the entry gate with no audit row saying a container stop did it."""
    src = open(os.path.join(DEPLOY, "keel-supervise.py"), encoding="utf-8").read()
    assert "params_store.set_param" in src, "drain does not use params_store"
    assert 'origin=DRAIN_ORIGIN' in src or 'origin="human"' in src, \
        "drain write has no origin"
    assert not re.search(r'storage\.set_setting\(\s*["\']halt_new_entries',
                         src), "drain bypasses params_store"


def test_supervisor_stops_the_child_with_sigint():
    """SIGINT is the only stop signal this codebase handles: werkzeug unwinds
    app.run() and NewsAgent.run() breaks its loop. Nothing installs a SIGTERM
    handler, so SIGTERM to the child is an immediate kill."""
    src = open(os.path.join(DEPLOY, "keel-supervise.py"), encoding="utf-8").read()
    assert "signal.SIGINT" in src and "send_signal" in src


def test_supervisor_only_drains_from_the_engine_role():
    src = open(os.path.join(DEPLOY, "keel-supervise.py"), encoding="utf-8").read()
    assert 'KEEL_DRAIN_ON_STOP"] = "0"' in src, (
        "a dashboard or news-agent restart must not halt a running engine")


def test_healthcheck_verifies_the_engine_thread_not_just_the_socket():
    """Flask keeps answering while the engine daemon thread is dead. A port
    probe would call that healthy."""
    src = open(os.path.join(DEPLOY, "keel-healthcheck.py"), encoding="utf-8").read()
    assert "engine_heartbeat_t" in src


def test_shell_scripts_are_strict_and_present():
    for f in ("keel-backup.sh", "keel-restore.sh", "keel-stop.sh",
              "keel-resume.sh", "keel-upgrade.sh", "keel-token.sh",
              "keel-freeze.sh", "provision-hetzner.sh"):
        p = os.path.join(DEPLOY, f)
        assert os.path.exists(p), "missing %s" % f
        src = open(p, encoding="utf-8").read()
        assert src.startswith("#!"), "%s has no shebang" % f
        # Strict mode anywhere before the first command that is not a comment
        # or an assignment. Unset-variable and pipe failures must abort a script
        # that stops a trading engine, not be shrugged off.
        assert re.search(r"^set -euo pipefail$", src, re.M), \
            "%s is not strict-mode (set -euo pipefail)" % f


def test_nothing_in_deploy_scripts_can_flip_trading_mode():
    """CLAUDE.md invariant 2: live requires live_switch's two-step confirm. No
    deploy script may be the shortcut, not even by accident."""
    for f in os.listdir(DEPLOY):
        if not (f.endswith(".sh") or f.endswith(".py")) or f.startswith("test_"):
            continue
        src = open(os.path.join(DEPLOY, f), encoding="utf-8").read()
        for m in re.finditer(r"trading_mode", src):
            line = src[src.rfind("\n", 0, m.start()) + 1:
                       src.find("\n", m.start())]
            stripped = line.strip()
            is_comment = stripped.startswith("#") or stripped.startswith("*")
            is_read = "SELECT" in line or "echo" in line or "print" in line
            assert is_comment or is_read, \
                "%s appears to WRITE trading_mode: %s" % (f, stripped)


# ==================================== no credential reaches an image layer
# config.yaml ships, and telegram_notifier.build_notifier() reads
# `telegram.bot_token` from it BEFORE the settings DB:
#     token = tg.get("bot_token") or ""
#     token = token or storage.get_setting("telegram_bot_token", "")
# So the field the file invites an operator to fill in is also the one that
# beats the dashboard, and filling it in before a build writes a live token
# into a layer that `docker history` keeps forever.
def _guard():
    return _load_module("keel-config-guard.py", "keel_config_guard_under_test")


def test_the_build_refuses_a_config_that_carries_a_token():
    g = _guard()
    findings = g.scan_text(
        'telegram:\n'
        '  enabled: true\n'
        '  bot_token: "123456789:AAHexampleexampleexampleexampleexam"\n'
        '  chat_id: "-1001234567890"\n', "config.yaml")
    keys = " ".join(findings)
    assert "bot_token" in keys, "a filled-in bot_token was allowed into a layer"
    assert "chat_id" in keys, "a filled-in chat_id was allowed into a layer"


def test_the_build_guard_catches_a_credential_a_yaml_parser_would_not_see():
    """A token pasted under a key nobody enumerated, or into a comment. Parsing
    the YAML would see neither; the layer keeps both."""
    g = _guard()
    assert g.scan_text("# TODO rotate 123456789:AAHexampleexampleexampleexampleexam\n",
                       "notes.yaml"), "credential in a comment slipped through"
    assert g.scan_text(
        "notify:\n  hook: https://discord.com/api/webhooks/1/abcdef\n", "x.yaml"), \
        "a webhook URL under an unenumerated key slipped through"


def test_the_build_guard_never_prints_the_credential():
    """A guard that echoes the secret into the build log has moved it, not
    caught it — and build logs are kept and shared."""
    g = _guard()
    secret = "987654321:AAHsecretsecretsecretsecretsecretse"
    findings = g.scan_text('  bot_token: "%s"\n' % secret, "config.yaml")
    assert findings
    blob = " ".join(findings)
    assert secret not in blob, "the guard printed the token"
    assert "AAHsecret" not in blob, "the guard printed part of the token"


def test_the_shipped_config_passes_the_guard():
    """If this fails, someone has a live credential in their working copy and
    the build is about to be refused — which is the point."""
    g = _guard()
    for rel in ("trading-bot/config.yaml", "trading-bot/config.example.yaml"):
        assert g.scan_text(_read(rel), rel) == [], \
            "%s carries a credential" % rel


def test_the_guard_accepts_the_empty_placeholders_it_must_not_flag():
    g = _guard()
    assert g.scan_text('telegram:\n  bot_token: ""\n  chat_id: ""\n', "c.yaml") == []
    assert g.scan_text('  bot_token:            # from the dashboard\n', "c.yaml") == []


def test_the_image_runs_the_config_guard_before_it_is_usable():
    """The guard is only a guard if the build actually runs it, after the COPY
    that brings config.yaml in — so a config change re-runs it."""
    copy_at = DOCKERFILE.find("COPY --chown=10001:10001 trading-bot/")
    run_at = DOCKERFILE.find("RUN python /app/bin/keel-config-guard.py")
    assert copy_at != -1, "the app COPY moved; re-check the guard's position"
    assert run_at != -1, "the Dockerfile does not run keel-config-guard.py"
    assert run_at > copy_at, "the guard runs before config.yaml is in the image"


# ============================================== the backup must not block a stop
def _bash():
    b = shutil.which("bash")
    if not b:
        raise Skip("no bash on this host; run this suite in the image or on CI")
    if not shutil.which("tar") or not shutil.which("gzip"):
        raise Skip("tar/gzip not on PATH")
    return b


def _shell_path(p):
    """Windows path -> a path the shell's tar will accept.

    On the deploy host this is a no-op. Here it matters: `tar tzf C:/x` makes
    GNU tar read "C" as a remote host, and a backslash is not a separator to
    it. The script under test is unchanged either way — only how this harness
    hands it a directory."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        return "/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/"))
    return p.replace("\\", "/")


def _run_backup_with_fake_docker(tar_exit):
    """Run the real keel-backup.sh with `docker` replaced by a stub.

    No daemon, no network. The stub answers each `docker compose ...` the
    script issues and, for the state archive, emits a REAL tar.gz and then
    exits with `tar_exit` — which is how GNU tar reports "file changed as we
    read it" (1) against the engine rewriting state/open_spread.json every
    cycle and the news agent appending to news_agent.log continuously.
    """
    import tempfile
    bash = _bash()
    work = tempfile.mkdtemp()
    bindir = os.path.join(work, "bin")
    payload = os.path.join(work, "payload")
    dest = os.path.join(work, "backups")
    os.makedirs(bindir)
    os.makedirs(payload)

    # Run a byte-copy of the script with LF endings. Git stores these files LF,
    # but a Windows checkout with core.autocrlf=true materialises them CRLF, and
    # bash then reads `set -euo pipefail\r` and dies with "invalid option name"
    # before the script does anything. That is a real hazard for anyone who
    # COPIES a Windows working tree to the Linux host instead of cloning there
    # (docs/DEPLOYMENT-LINUX.md §2 says clone), but it is not what these two
    # tests are measuring, and letting it mask the tar behaviour would leave
    # the actual defect untested.
    # Same relative position, so the script's own `dirname/../..` REPO_DIR
    # resolves to a directory rather than something above the temp root.
    staged = os.path.join(work, "scripts", "deploy")
    os.makedirs(staged)
    script = os.path.join(staged, "keel-backup.sh")
    with open(os.path.join(DEPLOY, "keel-backup.sh"), "rb") as src:
        body = src.read().replace(b"\r\n", b"\n")
    with open(script, "wb") as dst:
        dst.write(body)
    os.chmod(script, 0o755)
    with open(os.path.join(payload, "open_spread.json"), "w") as f:
        f.write("{}")

    stub = """#!/usr/bin/env bash
case "$*" in
  *"ps --status running --services"*) echo engine ;;
  *"integrity_check"*)                echo ok ;;
  *"SELECT count(*)"*)                echo 51 ;;
  *".backup"*)                        exit 0 ;;
  *" cat "*)                          printf 'SQLite format 3\\0' ;;
  *" rm -f "*)                        exit 0 ;;
  *"tar czf -"*)
      tar czf - -C "%s" . || true
      exit %d ;;
  *) exit 0 ;;
esac
""" % (_shell_path(payload), tar_exit)
    stub_path = os.path.join(bindir, "docker")
    with open(stub_path, "w", newline="\n") as f:
        f.write(stub)
    os.chmod(stub_path, 0o755)

    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env["PATH"]
    p = subprocess.run(
        [bash, _shell_path(script), _shell_path(dest)],
        capture_output=True, text=True, env=env, timeout=180)
    states = [f for f in os.listdir(dest)] if os.path.isdir(dest) else []
    return p, [f for f in states if f.startswith("keel-state-")], dest


def test_a_routine_tar_warning_does_not_block_the_stop():
    """The defect: `tar czf -` over a live state/ exits 1 as a matter of course,
    `set -euo pipefail` turned that into a failed backup, and keel-stop.sh then
    REFUSED TO STOP THE STACK — leaving --no-backup (no evidence at all) as the
    operator's only route, in the moment they most wanted a clean stop."""
    p, states, dest = _run_backup_with_fake_docker(tar_exit=1)
    assert p.returncode == 0, (
        "backup exited %d on the routine 'file changed as we read it'.\n"
        "stdout:\n%s\nstderr:\n%s" % (p.returncode, p.stdout, p.stderr))
    assert states, "no state archive was written"
    out = subprocess.run(
        [_bash(), "-c", "tar tzf '%s'" % _shell_path(os.path.join(dest, states[0]))],
        capture_output=True, text=True)
    assert out.returncode == 0, "the archive it kept is not readable"
    assert "open_spread.json" in out.stdout, \
        "the archive is missing the files it was supposed to hold"


def test_a_fatal_tar_error_is_still_a_failure_and_leaves_no_archive():
    """Tolerating exit 1 must not become tolerating everything. Exit 2 is
    tar's fatal code, and a half-written archive that nobody can read must not
    sit in the backup directory looking like a backup."""
    p, states, _ = _run_backup_with_fake_docker(tar_exit=2)
    assert p.returncode == 3, (
        "fatal tar error exited %d; expected 3 (DB snapshot written, state "
        "archive failed).\nstdout:\n%s\nstderr:\n%s"
        % (p.returncode, p.stdout, p.stderr))
    assert not states, "a failed state archive was left behind: %s" % states


def test_keel_stop_continues_when_only_the_state_archive_failed():
    """Exit 3 means the promotion-gate evidence — the trades table — is already
    snapshotted and verified. Refusing to stop over the lesser half is how a
    scripted safe stop turns into a stop done by hand with no drain at all."""
    src = open(os.path.join(DEPLOY, "keel-stop.sh"), encoding="utf-8").read()
    body = src[src.index("backing up before stopping"):src.index("open positions at stop time")]
    assert re.search(r'BK_RC"?\s*-eq\s*3', body), \
        "keel-stop.sh does not distinguish a state-archive-only failure"
    assert re.search(r"exit 1", body), \
        "keel-stop.sh no longer refuses when the DB snapshot itself failed"


def test_no_destructive_volume_removal_in_any_script():
    """`docker compose down -v` deletes the named volumes, and the trades table
    is the promotion-gate evidence. Nothing here should be one typo away."""
    for f in os.listdir(DEPLOY):
        if not f.endswith(".sh"):
            continue
        src = open(os.path.join(DEPLOY, f), encoding="utf-8").read()
        for pat in (r"down\s+(-\w+\s+)*-v\b", r"down\s+--volumes",
                    r"volume\s+rm", r"volume\s+prune"):
            for m in re.finditer(pat, src):
                line = src[src.rfind("\n", 0, m.start()) + 1:src.find("\n", m.start())]
                assert line.strip().startswith("#"), \
                    "%s contains a volume-destroying command: %s" % (f, line.strip())


# ================================================ behaviour, not just text
# The checks above read files. These two run the code against the real
# trading-bot modules on a throwaway database, because "the drain calls
# params_store" is a claim about a source string, and "the drain actually
# closes the entry gate and leaves an audit row" is the property that matters.
def _load_module(filename, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(DEPLOY, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_app_db():
    """Import the app's storage against a temp DB and return the module."""
    import tempfile
    app = os.path.join(REPO, "trading-bot")
    if app not in sys.path:
        sys.path.insert(0, app)
    import storage
    storage._DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
    storage._conn = None
    storage.init()
    return storage, app


def test_drain_actually_closes_the_entry_gate_and_leaves_an_audit_row():
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    sup = _load_module("keel-supervise.py", "keel_supervise_under_test")
    sup.APP_DIR = app

    assert not storage.get_setting("halt_new_entries"), "precondition: gate open"
    assert sup._halt_new_entries() is True, "drain reported failure"

    # The gate is shut...
    assert storage.get_setting("halt_new_entries") is True, \
        "halt_new_entries was not set; a stop would leave entries live"

    # ...and engine.params() — the function the loop actually reads — agrees.
    import engine
    assert engine.params()["halt_new_entries"] is True, \
        "the engine would not see the halt"

    # ...and it is auditable.
    rows = storage.query(
        "SELECT * FROM param_changes WHERE key='halt_new_entries' ORDER BY id DESC")
    assert rows, "no param_changes row: the gate closed with no record of why"
    assert rows[0]["origin"] == "human" and rows[0]["accepted"] == 1
    assert "keel-supervise" in (rows[0]["trigger_data"] or ""), \
        "the audit row does not say a container stop did this"


def test_drain_is_idempotent():
    """A restart loop must not write a param_changes row per crash."""
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    sup = _load_module("keel-supervise.py", "keel_supervise_idem")
    sup.APP_DIR = app
    sup._halt_new_entries()
    before = len(storage.query("SELECT id FROM param_changes"))
    sup._halt_new_entries()
    after = len(storage.query("SELECT id FROM param_changes"))
    assert before == after, "second drain wrote another audit row"


def test_healthcheck_calls_a_stale_heartbeat_unhealthy():
    """The failure a port probe cannot see: Flask answering, engine thread dead."""
    import time as _t
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    hc = _load_module("keel-healthcheck.py", "keel_healthcheck_under_test")
    hc.APP_DIR = app
    hc._http_ok = lambda url, timeout=5.0: (True, "")     # socket is fine

    storage.set_setting("engine_heartbeat_t", int(_t.time()))
    assert hc.check_engine() == 0, "a fresh heartbeat must be healthy"

    storage.set_setting("engine_heartbeat_t", int(_t.time()) - 3600)
    assert hc.check_engine() == 1, \
        "an hour-old heartbeat reported healthy — the check is worthless"

    storage.execute("DELETE FROM settings WHERE key='engine_heartbeat_t'")
    assert hc.check_engine() == 1, "no heartbeat at all must be unhealthy"


def test_stop_sequence_drains_before_it_signals():
    """Order is the whole safety property.

    If the SIGINT went first, the engine could be mid-entry when it lands. The
    gate must be shut before anything is signalled. Driven with a fake child so
    this tests the ordering rather than the host OS's signal semantics (Windows
    does not deliver SIGTERM the way Linux does).

    CHANGED, deliberately: this used to assert the sequence
    ["drain", "quiet", "signal:SIGINT"], where "quiet" was
    `_wait_for_quiet_window` — a sleep timed off `engine_heartbeat_t` to guess
    when the loop was between cycles. The old expectation was wrong twice over.
    It certified a heuristic as if it were the safety property (the property is
    "the loop is not mid-cycle", which a sleep cannot establish), and
    `engine.stop()` now exists, so the child stops the loop for real and the
    guess is gone. The step is removed, not renamed.
    """
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    os.environ["KEEL_DRAIN_ON_STOP"] = "1"
    os.environ["KEEL_CHILD_GRACE_S"] = "2"
    sup = _load_module("keel-supervise.py", "keel_supervise_stop")
    sup.APP_DIR = app

    events = []

    class FakeChild:
        pid = 4242
        returncode = 0
        def poll(self): return None
        def send_signal(self, s):
            events.append("signal:%s" % ("SIGINT" if s == signal.SIGINT else s))
        def wait(self, timeout=None):
            events.append("wait"); return 0
        def terminate(self): events.append("SIGTERM")
        def kill(self): events.append("SIGKILL")

    sup._child = FakeChild()
    real_halt = sup._halt_new_entries
    sup._halt_new_entries = lambda *a, **k: (events.append("drain"), real_halt())[1]

    sup.stop_child(signal.SIGTERM, gate_armed=False)

    assert events[:2] == ["drain", "signal:SIGINT"], \
        "stop sequence was %s; must drain, then SIGINT" % events
    assert storage.get_setting("halt_new_entries") is True
    assert "SIGKILL" not in events, "escalated to SIGKILL on a child that exited"


# ------------------------------------------------- the stop must not deadlock
class _CPythonPopenModel:
    """A Popen stand-in that reproduces CPython's POSIX waitpid locking.

    This is the whole point of the test below, so it is worth stating what is
    being modelled. `subprocess.Popen._wait()` has two paths:

      timeout is None -> `with self._waitpid_lock: self._try_wait(0)`. The lock
        is HELD across a blocking waitpid(). CPython installs signal handlers
        without SA_RESTART, so the Python handler runs from inside that call's
        EINTR retry — with the lock held by this very thread.
      timeout is not None -> a busy loop whose only acquisition is
        `self._waitpid_lock.acquire(False)`. A non-reentrant lock already held
        by this thread never yields, so the loop reaps nothing and runs out to
        TimeoutExpired.

    A stop sequence that runs inside the handler therefore times out every
    time, on a child that has already exited, and then escalates to SIGTERM and
    SIGKILL — inventing evidence that the child ignored the signal.
    """

    def __init__(self, on_first_wait=None):
        self.pid = 4242
        self.returncode = None
        self.signals = []
        # Only waits long enough to BE the grace window are counted. A short
        # poll expiring is how the supervisor is supposed to work; the grace
        # window expiring on a child that already exited is the defect.
        self.graces_expired = 0
        self._lock = threading.Lock()
        self._exited = False
        self._on_first_wait = on_first_wait

    # `Popen._internal_poll` gives up rather than block when the lock is busy.
    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if not self._lock.acquire(False):
            return None
        try:
            if self._exited:
                self.returncode = 0
            return self.returncode
        finally:
            self._lock.release()

    # Names, not signal constants: Windows has no signal.SIGKILL, and a model
    # that raises AttributeError the moment a regression escalates would report
    # the regression as an error in the harness instead of a failed assertion.
    def send_signal(self, sig):
        self.signals.append(getattr(sig, "name", str(sig)))
        if sig == signal.SIGINT:
            self._exited = True          # a child that handles SIGINT, exiting
    def terminate(self):
        self.signals.append("SIGTERM"); self._exited = True
    def kill(self):
        self.signals.append("SIGKILL"); self._exited = True

    def _fire(self, holding_lock):
        cb, self._on_first_wait = self._on_first_wait, None
        if cb:
            cb(holding_lock)

    def wait(self, timeout=None):
        if timeout is None:
            with self._lock:                       # blocking waitpid, lock held
                self._fire(holding_lock=True)      # EINTR: handler runs in here
                deadline = time.time() + 30
                while not self._exited and time.time() < deadline:
                    time.sleep(0.01)
                self.returncode = 0
                return 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._lock.acquire(False):
                try:
                    if self._exited:
                        self.returncode = 0
                        return 0
                finally:
                    self._lock.release()
            self._fire(holding_lock=False)         # handler runs between tries
            time.sleep(0.01)
        if timeout >= 1:
            self.graces_expired += 1
        raise subprocess.TimeoutExpired("child", timeout)


def test_stop_does_not_deadlock_on_the_popen_waitpid_lock():
    """The defect: a guaranteed extra 30s and a log that lies.

    Drive the supervisor's real wait loop with a child that models CPython's
    locking, deliver the stop signal from inside the wait exactly as EINTR
    does, and require that the child is stopped with ONE SIGINT and no
    escalation. Against the previous shape — stop work inside the handler,
    `_child.wait()` with no timeout in main() — the handler would hold the lock,
    both of its timed waits would expire, and this would see SIGTERM and
    SIGKILL on a child that exited on the SIGINT.
    """
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    os.environ["KEEL_DRAIN_ON_STOP"] = "1"
    os.environ["KEEL_CHILD_GRACE_S"] = "3"
    sup = _load_module("keel-supervise.py", "keel_supervise_deadlock")
    sup.APP_DIR = app
    sup._stop_signum = None

    child = _CPythonPopenModel(
        on_first_wait=lambda holding_lock: sup._on_signal(signal.SIGTERM, None))
    sup._child = child

    t0 = time.time()
    rc = sup.supervise(gate_armed=False)
    elapsed = time.time() - t0

    assert child.signals == ["SIGINT"], (
        "child was signalled %s; a child that exits on SIGINT must not be "
        "escalated to SIGTERM/SIGKILL" % child.signals)
    assert child.graces_expired == 0, (
        "%d grace window(s) expired against a child that had already exited — "
        "the waitpid lock is being held across the stop" % child.graces_expired)
    assert elapsed < 3, (
        "stop took %.1fs with a 3s grace; it should not consume the grace "
        "window at all" % elapsed)
    assert rc == 0
    assert storage.get_setting("halt_new_entries") is True


def test_the_signal_handler_touches_nothing_but_a_flag():
    """It can run with `Popen._waitpid_lock` held. Any call into the child, or
    any lock, is the deadlock. A child that explodes on attribute access proves
    the handler never reaches for one."""
    sup = _load_module("keel-supervise.py", "keel_supervise_handler")
    sup._stop_signum = None

    class Landmine:
        def __getattr__(self, name):
            raise AssertionError(
                "signal handler touched _child.%s; it must only record the "
                "signal number" % name)

    sup._child = Landmine()
    sup._on_signal(signal.SIGTERM, None)
    assert sup._stop_signum == signal.SIGTERM
    sup._child = None


def test_supervisor_never_waits_on_the_child_without_a_timeout():
    """The structural companion to the test above: one `.wait()` with no
    timeout anywhere in this file reintroduces the whole defect.

    Parsed, not grepped — the docstring in that file quotes `_child.wait()` while
    explaining the bug, and a test that cannot tell prose from a call site is
    the kind of test this repo has been bitten by.
    """
    import ast
    src = open(os.path.join(DEPLOY, "keel-supervise.py"), encoding="utf-8").read()
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "wait":
            timed = bool(node.args) or any(k.arg == "timeout" for k in node.keywords)
            if not timed:
                bad.append(node.lineno)
    assert not bad, (
        "keel-supervise.py:%s waits with no timeout; that holds "
        "Popen._waitpid_lock across waitpid() and the signal handler runs "
        "inside it" % bad)


# ------------------------------------------------ the start must not resume
def test_an_undrained_previous_run_halts_entries_at_the_next_start():
    """`restart: unless-stopped` after a segfault, an OOM kill or `docker kill`.
    None of those reach the stop handler, so nothing drained: the container
    would otherwise come back with the entry gate exactly as the crash left it.
    """
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    sup = _load_module("keel-supervise.py", "keel_supervise_startup")
    sup.APP_DIR = app

    storage.set_setting(sup.RUN_STATE_KEY, sup.RUN_STATE_RUNNING)   # crashed run
    assert not storage.get_setting("halt_new_entries"), "precondition: gate open"

    assert sup.startup_entry_gate() == "halted"

    assert storage.get_setting("halt_new_entries") is True, \
        "the engine would come back taking new positions after a stop nobody " \
        "has explained"
    import engine
    assert engine.params()["halt_new_entries"] is True, \
        "the engine would not see the halt"
    rows = storage.query("SELECT * FROM param_changes WHERE key='halt_new_entries'"
                         " ORDER BY id DESC")
    assert rows and rows[0]["origin"] == "human" and rows[0]["accepted"] == 1
    td = rows[0]["trigger_data"] or ""
    assert "keel-supervise" in td and "undrained" in td, \
        "the audit row does not say an undrained stop caused this: %s" % td
    assert storage.get_setting(sup.RUN_STATE_KEY) == sup.RUN_STATE_RUNNING, \
        "this run did not re-arm the marker; the next crash would look clean"


def test_a_drained_stop_leaves_the_next_start_alone():
    """The halt must come from the crash, not from every start. A drained stop
    already wrote halt_new_entries; re-deciding it here would make the marker
    meaningless and bury the real signal in noise."""
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    sup = _load_module("keel-supervise.py", "keel_supervise_startup_clean")
    sup.APP_DIR = app

    storage.set_setting(sup.RUN_STATE_KEY, sup.RUN_STATE_DRAINED)
    assert sup.startup_entry_gate() == "clean"
    assert not storage.get_setting("halt_new_entries")
    assert not storage.query("SELECT id FROM param_changes "
                             "WHERE key='halt_new_entries'")


def test_a_first_start_is_not_treated_as_a_crash():
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    sup = _load_module("keel-supervise.py", "keel_supervise_startup_first")
    sup.APP_DIR = app
    assert sup.startup_entry_gate() == "first-start"
    assert not storage.get_setting("halt_new_entries")


def test_the_startup_gate_arms_only_for_a_real_engine_start():
    """`docker compose run --rm engine python tests/test_risk_rails.py` shares
    the role and the entrypoint. It must neither trip the gate nor clear the
    marker a running engine left behind."""
    sup = _load_module("keel-supervise.py", "keel_supervise_armed")
    os.environ.pop("KEEL_STARTUP_GATE", None)
    assert sup.startup_gate_armed("engine", ["python", "/app/bin/keel-run-engine.py"])
    assert not sup.startup_gate_armed("engine", ["python", "tests/test_risk_rails.py"])
    assert not sup.startup_gate_armed("engine", ["sqlite3", "data/trading.db"])
    assert not sup.startup_gate_armed(
        "dashboard", ["python", "/app/bin/keel-run-dashboard.py"])


def test_the_engine_does_not_start_when_the_gate_cannot_be_closed():
    """Fail closed. If the previous stop cannot be classified AND the gate
    cannot be shut, a started engine is an engine that may open a position
    nobody decided to open. Refusing is loud; starting is silent."""
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    sup = _load_module("keel-supervise.py", "keel_supervise_refuse")
    sup.APP_DIR = app
    sup._run_state_get = lambda: sup.RUN_STATE_RUNNING
    sup._halt_new_entries = lambda *a, **k: False        # DB unwritable

    raised = False
    try:
        sup.startup_entry_gate()
    except Exception:
        raised = True
    assert raised, "startup gate returned normally with the entry gate open"

    # ...and main() turns that into a refusal to launch the child.
    sup.startup_entry_gate = lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
    sup._child = None
    old_argv, old_term, old_int = (sys.argv,
                                   signal.getsignal(signal.SIGTERM),
                                   signal.getsignal(signal.SIGINT))
    sys.argv = ["keel-supervise.py", "--role", "engine", "--",
                "python", "/app/bin/keel-run-engine.py"]
    try:
        rc = sup.main()
    finally:
        sys.argv = old_argv
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    assert rc == 3, "expected a refusal exit code, got %r" % rc
    assert sup._child is None, \
        "the engine was launched with an unknown entry-gate state"


def test_a_child_that_dies_on_its_own_halts_entries():
    """The supervisor can see this one directly — it holds the exit status.
    A SIGSEGV child is an unexplained stop whether or not anyone restarts it."""
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    sup = _load_module("keel-supervise.py", "keel_supervise_crash")
    sup.APP_DIR = app
    sup._stop_signum = None

    class Segfaulted:
        pid = 7
        returncode = -11
        def wait(self, timeout=None): return -11
        def poll(self): return -11

    sup._child = Segfaulted()
    rc = sup.supervise(gate_armed=True)

    assert rc == 139, \
        "a child killed by signal 11 must be reported as 128+11, got %r" % rc
    assert storage.get_setting("halt_new_entries") is True, \
        "the engine crashed and the entry gate stayed open"


# ------------------------------------ the child's stop is deterministic now
def test_run_engine_stops_the_loop_by_event_not_by_timing():
    """The property the heartbeat heuristic could never have: the loop is out
    of the cycle BEFORE the interpreter exits, and it does not cost a poll
    interval to find out.

    Driven with a stand-in that has engine_loop's actual shape — check the
    event, do work, then park in `_stop.wait(poll_seconds)` — because running
    the real loop would need a venue. The event, `engine.stop()` and the
    wrapper under test are all real.
    """
    _, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    run = _load_module("keel-run-engine.py", "keel_run_engine")
    import engine

    original = engine.engine_loop
    cycles = []

    def stand_in_loop(poll_seconds=20):
        engine._stop.clear()
        while not engine._stop.is_set():
            cycles.append(time.time())
            time.sleep(0.05)                      # the "work" half of a cycle
            engine._stop.wait(poll_seconds)

    try:
        engine.engine_loop = stand_in_loop
        run.instrument_loop()
        t = threading.Thread(target=engine.engine_loop, args=(30,), daemon=True)
        t.start()
        assert run._loop_started.wait(5), "stand-in loop never started"
        time.sleep(0.2)

        t0 = time.time()
        try:
            run._on_signal(signal.SIGTERM, None)   # raises KeyboardInterrupt
        except KeyboardInterrupt:
            pass
        rc = run.drain_loop()
        elapsed = time.time() - t0

        assert rc == 0, "clean stop reported as %r" % rc
        assert run._loop_exited.is_set(), "the loop had not returned"
        t.join(timeout=5)
        assert not t.is_alive(), "loop thread still running after the stop"
        assert elapsed < 5, (
            "took %.1fs to stop a loop with a 30s poll interval — this is "
            "waiting the interval out, not setting the event" % elapsed)
    finally:
        engine.engine_loop = original
        engine._stop.clear()


def test_run_engine_refuses_to_call_a_stuck_loop_a_clean_stop():
    """The failure this must never dress up: a loop that did not come out, on
    a process that is about to exit and destroy the thread mid-cycle."""
    _, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    run = _load_module("keel-run-engine.py", "keel_run_engine_stuck")
    import engine

    original = engine.engine_loop
    try:
        def deaf_loop(poll_seconds=20):
            while True:                            # never looks at _stop
                time.sleep(0.05)

        engine.engine_loop = deaf_loop
        run.instrument_loop()
        run.LOOP_STOP_S = 0.5
        threading.Thread(target=engine.engine_loop, daemon=True).start()
        assert run._loop_started.wait(5)

        rc = run.drain_loop()
        assert rc != 0, \
            "a loop that never returned was reported as a clean stop (rc=%r)" % rc
    finally:
        engine.engine_loop = original
        engine._stop.clear()


def test_the_engine_container_runs_the_deterministic_stop_wrapper():
    """A CMD of `python server.py` puts the timing guess back: server.py never
    calls engine.stop(), so SIGINT just unwinds Flask and the daemon thread
    dies wherever it is."""
    cmd = re.search(r"^CMD\s+(\[.*\]|.*)$", DOCKERFILE, re.M)
    assert cmd, "Dockerfile has no CMD"
    assert "keel-run-engine.py" in cmd.group(1), \
        "engine CMD is %s; it must go through keel-run-engine.py" % cmd.group(1)
    for svc in ("dashboard", "newsagent"):
        ep = COMPOSE["services"][svc].get("entrypoint") or []
        assert "keel-supervise.py" in " ".join(ep), \
            "%s does not run under the supervisor" % svc


def test_compose_does_not_pin_a_child_grace_below_the_poll_interval():
    """keel-supervise derives the grace from engine.poll_seconds so that
    raising the poll cannot silently start truncating stops. A literal in
    compose wins over that derivation and reintroduces it."""
    env = COMPOSE["services"]["engine"].get("environment") or {}
    grace = env.get("KEEL_CHILD_GRACE_S")
    if grace is None:
        return
    cfg = yaml.safe_load(_read("trading-bot/config.yaml"))
    poll = int((cfg.get("engine") or {}).get("poll_seconds", 20))
    assert int(grace) >= poll + 10, (
        "KEEL_CHILD_GRACE_S=%s is under poll_seconds+10 (%d): a stop would be "
        "cut off mid-cycle" % (grace, poll + 10))


def test_healthcheck_fails_when_the_socket_is_down():
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    hc = _load_module("keel-healthcheck.py", "keel_healthcheck_socket")
    hc.APP_DIR = app
    hc._http_ok = lambda url, timeout=5.0: (False, "refused")
    import time as _t
    storage.set_setting("engine_heartbeat_t", int(_t.time()))
    assert hc.check_engine() == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    skipped = 0
    for fn in fns:
        try:
            fn()
            print("ok  ", fn.__name__)
        except Skip as e:
            skipped += 1
            print("skip", fn.__name__, "-", e)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e)
        except Exception as e:
            failed += 1
            print("ERR ", fn.__name__, "-", repr(e))
    print("\n%d passed, %d failed" % (len(fns) - failed - skipped, failed))
    if skipped:
        # Named, not silent. A skip is a check that did not run, and the reason
        # has to be visible or the count above reads as more coverage than it is.
        print("%d skipped (see 'skip' lines above)" % skipped)
    sys.exit(1 if failed else 0)
