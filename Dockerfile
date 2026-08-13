# Keel — Linux container image for the engine, the control dashboard and the
# news agent. One image, three roles; docker-compose.yml picks the role.
#
# Build context is the repo root. Read .dockerignore before changing anything
# here: it is deny-all-then-allow, and it is the only thing standing between
# `trading-bot/data/trading.db` (which holds venue api_key/api_secret in
# cleartext) and a permanent image layer.
#
#   docker build -t keel:local .
#
# NOT published to any public registry: LICENSE.md forbids distribution outside
# the team, and docs/ARCHITECTURE-V3.md §6 treats a public image as evidence of
# holding out to the public, which is the condition the 15-person CTA exemption
# rests on. Build on the host, or push to a PRIVATE registry.

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

# The wheels for this dependency set (flask, fastapi, uvicorn, httpx, ccxt,
# pyyaml, requests) are all manylinux today, so this toolchain is usually
# unused. It stays because "usually" is not a build guarantee: a transitive
# dependency that ships an sdist next month would otherwise turn a deploy into
# a debugging session on the trading host. It costs nothing — this stage is
# discarded and only the venv is copied forward.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies before source: this layer is the slow one and it must not be
# invalidated by a strategy edit.
COPY trading-bot/requirements.txt /tmp/requirements.txt

# requirements.txt is unpinned (`>=`), which means two nodes provisioned a week
# apart can run different ccxt against the same exchange — ARCHITECTURE-V3 §5
# item 25 names that as a defect to fix before crypto execution is wired.
# constraints.txt pins the fully resolved set without editing requirements.txt,
# which this change does not own. Regenerate it with keel-freeze.sh after any
# dependency change, and read the diff: a ccxt bump is a venue behaviour change.
COPY scripts/deploy/constraints.txt /tmp/constraints.txt

RUN pip install -r /tmp/requirements.txt -c /tmp/constraints.txt


# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

# No build-essential here. The final image carries a Python runtime, the venv
# and the source — nothing that compiles code.
#   tini      : not used as PID 1 (keel-supervise.py is), kept out deliberately.
#   curl/wget : not installed; keel-healthcheck.py is stdlib-only.
# sqlite3 IS installed on purpose: storage.py:_attempt_recover() shells out to
# `sqlite3` and returns False without it, which silently disables the recovery
# arm of CLAUDE.md invariant 7. On Windows that is a known dead path; on Linux
# it costs ~1.5 MB to make it live.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KEEL_APP_DIR=/app/trading-bot

COPY --from=builder /opt/venv /opt/venv

# Fixed uid/gid, not a floating system-assigned one: the data and state volumes
# outlive the image, and an operator who has to chown a restored backup needs a
# number that does not change between rebuilds.
RUN groupadd --gid 10001 keel \
 && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin keel

WORKDIR /app
COPY --chown=10001:10001 trading-bot/ /app/trading-bot/
COPY --chown=10001:10001 scripts/deploy/keel-supervise.py \
                         scripts/deploy/keel-healthcheck.py \
                         scripts/deploy/keel-run-engine.py \
                         scripts/deploy/keel-run-dashboard.py \
                         scripts/deploy/keel-config-guard.py \
                         /app/bin/

# The build stops here if config.yaml carries a credential.
#
# config.yaml ships (it is startup defaults), and telegram_notifier resolves
# `telegram.bot_token` from it BEFORE the DB — so the field an operator is
# invited to fill in by the file's own comment is also the one that wins over
# the dashboard, and filling it in before a build puts a live token in a layer
# that `docker history` keeps forever. This RUN is what lets
# docs/DEPLOYMENT-LINUX.md say "nothing in this deployment carries a
# credential" and mean it. It is invalidated by the COPY above, so it re-runs
# whenever config.yaml changes. See scripts/deploy/keel-config-guard.py for
# what it looks for and why it refuses instead of quietly scrubbing.
RUN python /app/bin/keel-config-guard.py /app/trading-bot

# These are the volume mountpoints. Creating them here, owned by keel, is what
# makes a FRESH named volume come up keel-owned — Docker seeds an empty named
# volume from the image path including its ownership. A volume that already
# exists keeps whatever ownership it had; docs/DEPLOYMENT-LINUX.md §10 has the
# chown for that case.
RUN mkdir -p /app/trading-bot/data /app/trading-bot/state \
 && chown -R 10001:10001 /app/trading-bot/data /app/trading-bot/state \
 && chmod +x /app/bin/*.py

# state/ is written with relative paths (engine.py writes "state/open_spread.json",
# news_agent.py logs to "state/news_agent.log"), so cwd is load-bearing.
WORKDIR /app/trading-bot

USER 10001:10001

# Roles differ only in the healthcheck and the command; compose overrides both.
# This default is the engine, because an image run bare should be the thing
# that manages positions, not a control plane.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "/app/bin/keel-healthcheck.py", "--role", "engine"]

# keel-supervise.py is PID 1. It reaps zombies, closes the entry gate on
# SIGTERM (halt_new_entries via params_store, audited) BEFORE signalling
# anything, then stops the child with SIGINT — the only stop signal this
# codebase handles, since nothing in the tree installs a SIGTERM handler. It
# also closes the entry gate at START if the previous run died without
# draining, so a segfault or an OOM kill cannot come back trading unattended.
#
# The child is keel-run-engine.py, not `python server.py` directly. server.py
# starts engine_loop as a daemon thread and never calls engine.stop(), so a
# bare SIGINT unwinds Flask and destroys the loop thread wherever it happens to
# be. keel-run-engine.py calls engine.stop() and holds the interpreter open
# until the loop has returned. That is the deterministic stop the supervisor
# used to approximate by timing SIGINT against engine_heartbeat_t.
#
# STOPSIGNAL stays the default SIGTERM: the supervisor wants it, and overriding
# it here would hide the translation somewhere an operator would not look.
ENTRYPOINT ["python", "/app/bin/keel-supervise.py", "--role", "engine", "--"]
CMD ["python", "/app/bin/keel-run-engine.py"]
