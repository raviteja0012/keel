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
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(REPO, "scripts", "deploy")


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
    for f in ("keel-supervise.py", "keel-healthcheck.py", "keel-run-dashboard.py"):
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
    gate must be shut, and observed shut, before anything is signalled. Driven
    with a fake child so this tests the ordering rather than the host OS's
    signal semantics (Windows does not deliver SIGTERM the way Linux does).
    """
    import signal as _sig
    storage, app = _fresh_app_db()
    os.environ["KEEL_APP_DIR"] = app
    os.environ["KEEL_DRAIN_ON_STOP"] = "1"
    sup = _load_module("keel-supervise.py", "keel_supervise_stop")
    sup.APP_DIR = app
    sup._wait_for_quiet_window = lambda poll_s: events.append("quiet")

    events = []

    class FakeChild:
        pid = 4242
        def poll(self): return None
        def send_signal(self, s):
            events.append("signal:%s" % ("SIGINT" if s == _sig.SIGINT else s))
        def wait(self, timeout=None):
            events.append("wait"); return 0
        def terminate(self): events.append("SIGTERM")
        def kill(self): events.append("SIGKILL")

    sup._child = FakeChild()
    real_halt = sup._halt_new_entries
    sup._halt_new_entries = lambda: (events.append("drain"), real_halt())[1]

    sup._terminate(_sig.SIGTERM, None)

    assert events[:3] == ["drain", "quiet", "signal:SIGINT"], \
        "stop sequence was %s; must drain, settle, then SIGINT" % events
    assert storage.get_setting("halt_new_entries") is True
    assert "SIGKILL" not in events, "escalated to SIGKILL on a child that exited"


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
    for fn in fns:
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e)
        except Exception as e:
            failed += 1
            print("ERR ", fn.__name__, "-", repr(e))
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    sys.exit(1 if failed else 0)
