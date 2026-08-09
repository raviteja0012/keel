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
                         scripts/deploy/keel-run-dashboard.py \
                         /app/bin/

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

# keel-supervise.py is PID 1. It reaps zombies, drains the entry gate on
# SIGTERM (halt_new_entries via params_store, audited), waits for the quiet part
# of the engine cycle, and stops the child with SIGINT — the only stop signal
# this codebase actually handles, since nothing in the tree installs a SIGTERM
# handler and engine_loop is a daemon thread with no stop event.
#
# STOPSIGNAL stays the default SIGTERM: the supervisor wants it, and overriding
# it here would hide the translation somewhere an operator would not look.
ENTRYPOINT ["python", "/app/bin/keel-supervise.py", "--role", "engine", "--"]
CMD ["python", "server.py"]
