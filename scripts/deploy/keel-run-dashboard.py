#!/usr/bin/env python3
"""Launch dashboard_api's ASGI app with an explicit bind address.

WHY THIS EXISTS
---------------
`server.py` takes SLC_HOST/SLC_PORT from the environment and `news_agent.py`
takes SLC_SERVER_URL, so both containerise without touching source.
`dashboard_api.py` has no such seam: `_load_cfg()` reads `dashboard.host` from
config.yaml and nothing overrides it. Left alone in a container it binds
127.0.0.1 inside its own network namespace, where nothing — not the host, not
a published port — can reach it.

The two ways to fix that from outside the source tree are to rewrite config.yaml
inside the image, or to import the app and hand uvicorn a host. Rewriting a
tracked config file at container start creates silent drift between what the
repo says and what is running, on the one file whose `dashboard.host` line is
load-bearing for the control plane. So: import and bind.

THIS DOES NOT WEAKEN THE LOOPBACK RULE. The rule that matters is "a stranger
cannot reach the control plane", and in a container the enforcement point moves
from the process bind to the port publication. docker-compose.yml publishes
this port as `127.0.0.1:8767:8767` — host loopback only. Binding 0.0.0.0 inside
the namespace is what makes that mapping work at all; it is not exposure.

Default is 127.0.0.1, so a container started without KEEL_DASHBOARD_HOST is
unreachable rather than open. Fail closed on the control plane.
"""
import os
import sys

APP_DIR = os.environ.get("KEEL_APP_DIR", "/app/trading-bot")
sys.path.insert(0, APP_DIR)

import uvicorn                                    # noqa: E402

import dashboard_api                              # noqa: E402
from dash_auth import get_token, _TOKEN_FILE      # noqa: E402


def main() -> None:
    cfg = dashboard_api._load_cfg()
    host = os.environ.get("KEEL_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("KEEL_DASHBOARD_PORT", cfg["port"]))

    # Materialise the token now rather than on the first mutating request, so
    # `keel-token.sh` can read state/dashboard_token immediately after start.
    # get_token() persists it 0600 on the state volume; it is never printed.
    get_token()

    print("=" * 60)
    print(" Keel control dashboard (container)")
    print(" Bind    : %s:%d" % (host, port))
    print(" Reach it: SSH tunnel or tailnet to the host, then 127.0.0.1:%d" % port)
    print(" Token   : %s (read it with scripts/deploy/keel-token.sh)" % _TOKEN_FILE)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(" NOTE    : bound beyond loopback INSIDE the container namespace.")
        print("           This is only safe while the published port stays")
        print("           127.0.0.1:%d on the host. Check `docker compose ps`." % port)
    print("=" * 60)

    uvicorn.run(dashboard_api.app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
