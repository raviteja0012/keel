# Linux deployment — containers on one cheap box

The engine, the control dashboard and the news agent, in three containers on one Linux host,
with SQLite on a persistent volume. No Windows, no RDP, no MT5, no MetaEditor recompile loop.
A user in India and a user in the USA each run their own node near their own venue.

```
scripts/deploy/
  provision-hetzner.sh    create the host (hcloud), firewall, cloud-init
  cloud-init.yaml         docker, ufw, ssh hardening, swap, daemon shutdown-timeout
  keel-backup.sh          online verified snapshot of the SQLite volume
  keel-restore.sh         put one back, with the confirmations that mistake needs
  keel-stop.sh            back up, then stop
  keel-resume.sh          deliberately re-open the entry gate
  keel-upgrade.sh         rebuild and replace, flat-book gated, volume preserved
  keel-token.sh           read the dashboard control token
  keel-freeze.sh          regenerate the pinned dependency set
  keel-supervise.py       PID 1: SIGTERM -> drain -> quiet window -> SIGINT
  keel-healthcheck.py     per-role health, engine checked at the thread not the socket
  keel-run-dashboard.py   binds dashboard_api's app explicitly
  constraints.txt         pinned dependency versions
  test_deploy_assets.py   the tests for all of the above
```

No risk rail is touched. `engine.py`, `params_store.py`, `live_switch.py`, `analysis.py` and
`storage.py` are unchanged by this work. Going live still needs both halves of the double gate.
This is plumbing.

---

## 1. Why this exists, and how it sits against ARCHITECTURE-V3

MT5 is the only reason this project needed Windows. It forces a Windows host, a compiled MQL5
expert advisor, a 5-second HTTP poll standing in for market data, and a recompile-and-reattach
loop every time the EA changes. The stated venues — Binance, 3Commas, TradingView, Webull —
are HTTP APIs. All of that Windows apparatus was MT5 tax on a system whose real requirement is
outbound HTTPS.

So MT5 is retired as the **primary** path. The adapter and the EA stay in the tree; they work,
and deleting them buys nothing. Nothing new gets architected around them.

**Read this next, because it will not agree with a document you may have already read.**
`docs/ARCHITECTURE-V3.md` is marked *decided*, and what it decided was the opposite: one
always-on **Windows** box per person, with §3 rejecting per-user docker compose (proposal #2)
and §10 promoting `docs/DEPLOYMENT-WINDOWS.md` to canonical runbook. That decision is sound
**given its premise**, which it states plainly in §4.1 and §3: every node must run MT5, so a
Windows host has to exist anyway, and adding Linux beside it buys a second box and a VPN hop on
the engine↔EA link.

Retiring MT5 removes that premise. With no MT5 there is no Windows host that must exist, and
V3's own reasoning then points here — its §3 concedes proposal #2 has "the right instinct" and
that "nothing risk-bearing moves", rejecting it only on the Windows-shaped grounds that no
longer apply. Note also that V3 §4.1 already contemplates a Linux node ("a $6 Linux box if
crypto-only"), deferred solely because CCXT was unwired.

What survives from V3 unchanged, and is honoured here:

| V3 holds | How this deployment honours it |
|---|---|
| §9.1 no hosted execution | the engine runs on the operator's own box; no shared control plane |
| §9.2 SQLite on local disk, no Postgres | named volume on the host's own filesystem, §8 |
| invariant 14: nothing between `manage_open_trades` and its rows | local volume only; NFS/virtiofs refused in writing, §8 |
| §6 credentials never leave the node | `.dockerignore` deny-all; no key in an image or a compose file, §5 |
| §6 no public distribution | private registry or local build only; `LICENSE.md` |
| §6 flat-book gate on updates | `keel-upgrade.sh` refuses with a position open, §9 |
| loopback control plane | every port published on 127.0.0.1, §6 |

**V3 §10 and §4.1 need amending to record the MT5 retirement.** This document does not do that —
it does not own that file. Someone should.

---

## 2. Pick a host, with honest numbers

Prices are from mid-2026 and **you must check them before buying**; providers move them and
regional pricing differs. The structural differences below matter more than the dollars.

| Host | Spec | ~ / month | The thing that actually decides it |
|---|---|---|---|
| **Hetzner CX22** (Falkenstein, Nuremberg, Helsinki) | 2 vCPU, 4 GB, 40 GB NVMe | **€3.79 + ~€0.60 IPv4 ≈ $4.80** | Best value on the list, local NVMe, static IPv4 you can allowlist at the venue. EU only. |
| **Hetzner CPX11** (Ashburn VA, Hillsboro OR) | 2 AMD vCPU, 2 GB, 40 GB | **~$5.60** | The US node. Same properties, US egress. |
| **Hetzner CAX11** (ARM, EU) | 2 Ampere vCPU, 4 GB, 40 GB | **€3.79 ≈ $4.15** | Cheapest. Regenerate `constraints.txt` on arm64 first (§9). |
| **DigitalOcean / Vultr / Linode, Mumbai or Bangalore** | 1 vCPU, 2 GB, 50 GB | **$12** | The India node. 1 GB tiers ($5–6) exist but are too tight to build on — see below. |
| **AWS Lightsail Mumbai** | 2 vCPU, 2 GB, 60 GB | **~$10** | Same role, marginally cheaper, noisier neighbours. |
| **Fly.io** | shared-cpu-1x, 1 GB + 3 GB volume | **~$6.15** | Cheap, but see the warning below. |
| **Railway** | Hobby + usage | **$5 + usage** | Redeploys and platform-initiated restarts are not yours to schedule. Weakest fit. |
| **Contabo** | 4 vCPU, 8 GB | **~$6** | Looks unbeatable; disk latency under contention is the reason it is not. |

**Minimum spec: 2 GB RAM.** Three Python processes with ccxt loaded sit around 500–700 MB
resident. A 1 GB box runs them but cannot also build the image — build elsewhere and pull from a
private registry, or add swap (cloud-init already adds 2 GB).

**Fly.io and Railway, stated plainly.** Both are fine for a stateless web app and awkward for
this. Fly Machines are rescheduled onto new hosts at the platform's discretion, which is an
unattended cold start of a process that manages stops; Fly volumes are pinned to a single host,
so the reschedule and the data are in tension. Both use shared egress IP pools, which defeats
the venue-side **IP allowlist** on the API key — and per ARCHITECTURE-V3 §6 an allowlisted,
withdrawals-disabled key is load-bearing for the money-transmission position, not a nicety. A
fixed-IP VPS keeps that available. If you use Fly anyway, buy a dedicated IPv4.

**Region.** Latency is not the binding constraint — the engine polls on a 20-second cycle, so
40 ms versus 200 ms to the venue is noise. Choose on jurisdiction and reliability instead.
ARCHITECTURE-V3 §6 is the authority: an India-resident operator runs **crypto spot only**, on
their own node with their own static IP, because FEMA closes offshore margin FX to residents and
SEBI's 2025 retail-algo framework wants a whitelisted static IP. That is a legal constraint that
happens to pick your host.

---

## 3. Provision

```bash
# once: authenticate the Hetzner CLI
hcloud context create keel

# the US node
scripts/deploy/provision-hetzner.sh keel-usa ash ~/.ssh/id_ed25519.pub

# the India node — Singapore is Hetzner's closest region; for a Mumbai box use
# the same cloud-init.yaml as user-data on DigitalOcean/Vultr/Linode
scripts/deploy/provision-hetzner.sh keel-india sin ~/.ssh/id_ed25519.pub
```

`cloud-init.yaml` is plain cloud-init and portable to any provider that takes user-data. It
installs Docker from the official repo, creates the non-root `keel` user, disables SSH password
auth, enables ufw with **only** port 22 open, adds 2 GB of swap, caps journald, and enables
unattended security upgrades **with automatic reboot off** — a reboot chosen by apt at 06:00 is
a reboot chosen while a position is open.

One line in there matters more than the rest:

```json
{ "shutdown-timeout": 120 }
```

When the host reboots, dockerd stops containers on **its own** timeout and then SIGKILLs
whatever is left. The default is 15 seconds. The supervisor's drain needs longer than that, and
`stop_grace_period` in compose cannot help you — only the daemon's timeout governs shutdown.
Leave it at 15 and every `reboot` is exactly the mid-position kill this design exists to
prevent.

Wait for it, then confirm:

```bash
ssh keel@<ip> 'cloud-init status --wait && docker --version && sudo ufw status'
```

---

## 4. Deploy

```bash
ssh keel@<ip>
git clone <your PRIVATE repo> /opt/keel/slc-trading-bot
cd /opt/keel/slc-trading-bot

docker compose build
docker compose up -d
```

**Clone on the host. Do not `scp` a Windows working tree.** With `core.autocrlf=true` — the
Windows default — every `.sh` in `scripts/deploy/` lands with CRLF endings, and bash then reads
`set -euo pipefail\r` and dies with `set: pipefail: invalid option name` before the script does
anything. `keel-stop.sh` is one of them, so the way you find out is while trying to stop
cleanly. Git stores these files LF; a clone on the host gets LF.

The build **refuses** if `trading-bot/config.yaml` carries a credential (§5). A `BUILD REFUSED`
message is that guard working, not a broken build.

`LICENSE.md` forbids distribution outside the team, and ARCHITECTURE-V3 §6 treats a public image
as evidence of holding out to the public — which is the condition the 15-person CTA exemption
rests on. **Build on the host, or push to a private registry. Never a public one.**

What comes up:

| Service | Process | Listens | Volumes |
|---|---|---|---|
| `keel-engine` | `server.py` — Flask + `engine_loop` + `agent_loop` as threads | `127.0.0.1:8766` | data + state |
| `keel-dashboard` | `dashboard_api.py` — the control plane, `live_switch` mounts here | `127.0.0.1:8767` | data + state |
| `keel-newsagent` | `news_agent.py` | nothing | **state only** |

The engine's three parts are one process because they cannot be separated: `feed_state` is
written by the Flask handlers and read by the loop.

The news agent gets **no database volume**. It does not import `storage` — it reaches the engine
over HTTP — and it is the process that parses arbitrary RSS from the open internet. It is the
last one that should be able to open the file holding the venue API keys.

---

## 5. Credentials

Nothing in this deployment carries one. Not in the image, not in `docker-compose.yml`, not in an
env file.

- **Venue API keys** go in through the dashboard at runtime and live in the DB on the volume
  (`venues.py`). Create every key with **withdrawals disabled** and **IP-allowlisted** to this
  node's address. `venues.upsert` defaults `read_only=True`: pasting a key grants visibility,
  never execution.
- **Telegram / Discord** — same, through the dashboard, per deployment.
- **The dashboard token** is generated on first start and persisted 0600 at
  `state/dashboard_token` on the volume. Read it with `scripts/deploy/keel-token.sh`.

`.dockerignore` is **deny-all, then allow**. The build context does not read `.gitignore`, and
`trading-bot/data/trading.db` holds `api_key`/`api_secret`/`password` in cleartext in the
`settings` table. A deny-list leaks the day someone adds a new secret path and forgets to come
back to it; a layer is permanent, kept by `docker history` and every registry copy even after a
later layer deletes the file. `test_deploy_assets.py` asserts the behaviour, not the text.

### The one file that could still have carried one, and the guard that stops it

`config.yaml` ships in the image — it is startup defaults, and the runtime DB wins after first
run. But it has a `telegram.bot_token` field, its own comment says *"flip to true after filling
token + chat_id"*, and `telegram_notifier.build_notifier()` reads it **before** the DB:

```python
token = tg.get("bot_token") or ""            # config.yaml first
token = token or storage.get_setting("telegram_bot_token", "")   # DB only if blank
```

So an operator doing exactly what the file told them, before a build, put a live token in a
permanent layer — while this section claimed nothing in the deployment carries a credential.

The build now runs `scripts/deploy/keel-config-guard.py` over the copied app directory and
**fails** if any credential-shaped key holds a value, or if any line — comments included —
contains something shaped like a Telegram token, a Discord/Slack webhook, an AWS key id or a PEM
private key. It prints the file, the line and the value's *length*, never the value.

It refuses rather than quietly stripping the field, for two reasons. A silent scrub leaves the
operator with a live token in their working copy and a false belief that the posture held. And
the `config.yaml` inside the image stays byte-identical to the one in the repo, which is the
same argument `keel-run-dashboard.py` makes about not rewriting tracked config at runtime.

Run it yourself before a build, or in a pre-commit hook:

```bash
python scripts/deploy/keel-config-guard.py trading-bot
```

If it fires on a value that was ever committed or pushed, rotate it — `SECURITY.md`.

---

## 6. Reaching the dashboard — it is not on the internet

Every published port is bound to `127.0.0.1` on the host. Nothing here answers a stranger, and
that is deliberate for two specific reasons:

- **8767** is the control plane. `live_switch` mounts on it. Someone who reaches it can halt
  your trading, or arm it.
- **8766** serves `POST /api/commands`, which **authenticates nothing today** — it validates only
  that the type is one of `trail_sl` / `move_sl_be` / `close_trade` and that a ticket is present.
  On a public interface that is a remote-close primitive for the whole internet.

**SSH tunnel — nothing to install:**

```bash
ssh -N -L 8767:127.0.0.1:8767 keel@<ip>
# then open http://127.0.0.1:8767 locally
scripts/deploy/keel-token.sh          # on the node, for the control token
```

**Tailscale — better if you want it from a phone:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
sudo tailscale serve --bg 8767        # tailnet -> loopback, TLS terminated
```

`tailscale serve` proxies tailnet traffic to loopback, so the loopback binding — the thing
`dashboard_api.py` and `live_switch.py` rest their security model on — is preserved rather than
worked around. Free personal plan, ten minutes.

**Do not** add `ports: - "8767:8767"`, do not put nginx in front of it on 0.0.0.0, and do not
"just for a minute" open 8767 in the firewall. Note also that **ufw does not filter ports Docker
publishes**: Docker's iptables rules sit in front of ufw's, which is a well-known trap. The
loopback bindings in `docker-compose.yml` are the real control; ufw and the Hetzner cloud
firewall are defence in depth behind them.

---

## 7. Verify

```bash
docker compose ps                       # all three Up, engine and dashboard healthy
docker compose exec engine python tests/test_risk_rails.py
python scripts/deploy/test_deploy_assets.py
```

Running the rails **inside the image** is the point: a green CI run on a GitHub runner says
nothing about this artefact. `trading-bot/tests/` is deliberately kept in the image for exactly
this.

Then check the things a container status cannot tell you:

```bash
# the engine thread, not just the socket
docker compose exec engine python /app/bin/keel-healthcheck.py --role engine

# database integrity and the gate clock
docker compose exec engine sqlite3 /app/trading-bot/data/trading.db \
  "PRAGMA quick_check; SELECT count(*) FROM trades WHERE status='closed';"

# is the entry gate open or closed, and did the last run stop cleanly?
docker compose exec engine sqlite3 /app/trading-bot/data/trading.db \
  "SELECT key,value FROM settings WHERE key IN
   ('trading_mode','halt_new_entries','keel_supervisor_run_state');"

# who closed the gate, and why — every drain leaves a row
docker compose exec engine sqlite3 /app/trading-bot/data/trading.db \
  "SELECT datetime(t,'unixepoch'),origin,new,accepted,trigger_data FROM param_changes
   WHERE key='halt_new_entries' ORDER BY id DESC LIMIT 5;"
```

`keel_supervisor_run_state` reads `running` while the engine is up and `stopped_drained` after a
clean stop. Finding `running` on a stopped stack means the last stop bypassed the handler, and
the next start will close the entry gate — §10.

The engine healthcheck checks the socket **and** that `engine_heartbeat_t` has advanced within
three poll cycles. Flask keeps answering HTTP perfectly while the engine daemon thread is dead
or wedged; a port probe would call that healthy. `engine_loop` writes that heartbeat at the top
of every cycle, unconditionally, precisely so "engine process down" and "engine up, no data" are
distinguishable.

**Compose does not restart unhealthy containers.** `restart: unless-stopped` reacts to a process
that *exits*; an unhealthy-but-alive container just reports `(unhealthy)` in `docker compose ps`
forever. Nothing in this repo pages you yet — `alerts.py` is written, tested, and imported by
nothing but its own test suite (ARCHITECTURE-V3 §0). Until it is wired, `docker compose ps` is
something a human has to look at. Do not mistake a healthcheck for a pager.

---

## 8. Reading the logs

```bash
docker compose logs -f --tail=100 engine
docker compose logs --since 1h dashboard
docker compose logs -f newsagent

# the files the app writes itself, on the state volume
docker compose exec engine tail -f /app/trading-bot/state/news_agent.log
docker compose exec engine ls -la /app/trading-bot/state/
```

Docker logs are capped at 10 MB × 5 per container, and journald at 500 MB. That is not tidiness:
a full disk is a SQLite corruption mechanism aimed at the file the kill switches read.

### The volume rule

`trading.db` is SQLite in **WAL** mode. At any instant the committed truth is spread across
`trading.db`, `trading.db-wal` and `trading.db-shm`, coordinated by POSIX advisory locks and a
shared-memory index. Both are only correct on a real local filesystem.

**Never put these volumes on NFS, CIFS/SMB, a Hetzner Storage Box, sshfs, or a Docker Desktop
virtiofs/gRPC-FUSE share.** The locks silently stop meaning anything, and the file they stop
protecting is the one holding the open positions and the kill-switch inputs (CLAUDE.md invariant
7, ARCHITECTURE-V3 §2 failure mode 4). Named volumes on the `local` driver only. Back the data
up off-box instead — that is what §9 is for. `test_deploy_assets.py` fails the build if a bind
mount or a non-local driver appears.

---

## 9. Back up, restore, upgrade

### Back up

```bash
scripts/deploy/keel-backup.sh                 # -> ./backups/
KEEP=30 scripts/deploy/keel-backup.sh /mnt/backups
```

Nightly, as the `keel` user:

```cron
17 3 * * *  cd /opt/keel/slc-trading-bot && KEEP=30 scripts/deploy/keel-backup.sh >> /var/log/keel-backup.log 2>&1
```

Not `cp trading.db`. Copying the main file while the engine runs gives you something that opens
without complaint and is missing every transaction since the last checkpoint — exactly the
recent trades you cared about. The script uses `sqlite3 .backup` (the online backup API), then
runs `PRAGMA integrity_check` **on the copy** and refuses to write the file if it fails, then
prints the closed-trade count so you would notice a backup holding fewer trades than the last
one. Retention deletes old snapshots only after a new verified one exists.

Why this matters more here than usual: the promotion gate is 50 closed trades with positive
expectancy per strategy × asset-class cell, counted out of the `trades` table. **That table is
the evidence.** Losing it does not lose a database, it restarts a clock that takes months, and
nothing can reconstruct it — the cost model, git SHA and fill assumptions behind each closed
trade exist only in the row.

**Exit codes, because `keel-stop.sh` reads them:**

| Exit | Meaning |
|---|---|
| 0 | DB snapshot and state archive both written and verified |
| 2 | the snapshot failed `integrity_check` — nothing was written, investigate with `hallucination_check.py` |
| 3 | DB snapshot written and verified; only the `state/` archive failed |
| 1 | something earlier failed — the service is not running, or the snapshot itself did not happen |

Exit 3 exists because of a defect worth remembering. The engine rewrites `state/open_spread.json`
every cycle and the news agent appends to `state/news_agent.log` continuously, so
`tar czf -` over `state/` exits **1** — `file changed as we read it` — as a matter of routine.
That is a warning: the archive is written and every other member is intact. Under `set -e` it
aborted the backup anyway, and `keel-stop.sh` then **refused to stop the stack** — leaving
`--no-backup`, which saves nothing at all, as the only route, in the moment a clean stop mattered
most. A scripted safe stop that is unreliable when it is needed gets done by hand without a
drain.

Now: exit 1 from tar is tolerated and reported, exit 2 (tar's fatal code) is not, and either way
the archive is only kept if it can be listed back — which is what catches a truncated stream
regardless of the code that came with it. `keel-stop.sh` continues on exit 3, because the
promotion-gate evidence is the `trades` table and that is already snapshotted and verified, and
still refuses on anything that means the DB snapshot did not happen.

`keel-state-*.tar.gz` **contains `dashboard_token`.** It is written 0600. Encrypt it before it
leaves the host:

```bash
age -r <your-age-pubkey> backups/keel-state-*.tar.gz > /mnt/offsite/state.age
# or: gpg -c --cipher-algo AES256 backups/keel-state-*.tar.gz
```

**A backup you have never restored is a hypothesis.** Restore one into a scratch directory
before you need to.

### Restore

```bash
scripts/deploy/keel-stop.sh
scripts/deploy/keel-restore.sh backups/keel-db-20260809T031700Z.db.gz
docker compose up -d
scripts/deploy/keel-resume.sh
```

It refuses to run while the stack is up (writing under a live WAL reader turns one bad database
into two), verifies the archive *before* touching the live file, prints the closed/open trade
counts and the `trading_mode` it is about to install, requires typing `YES`, keeps the replaced
file as `trading.db.pre-restore`, and deletes the stale `-wal`/`-shm` that belong to the old
database.

### Upgrade without losing the volume

```bash
scripts/deploy/keel-upgrade.sh --pull
```

Three refusals, in order of how expensive the mistake is:

1. **A position is open.** `storage._MIGRATIONS` is additive with no down path, and a schema
   change landing under an open position changes the rules mid-trade. ARCHITECTURE-V3 §6 makes
   the flat-book gate a condition of updating at all.
2. **No verified backup taken in this run.**
3. **Auto-resume.** The new build starts with entries halted; a human decides when it trades.

The volume survives because `docker compose up -d --build` replaces containers and leaves named
volumes alone. The only thing that destroys them is `docker compose down -v`, which appears
nowhere in `scripts/deploy/` — and `test_deploy_assets.py` fails if anyone adds it.

After it finishes, run the rails against the new image before resuming:

```bash
docker compose exec engine python tests/test_risk_rails.py
scripts/deploy/keel-resume.sh
```

On ARM (Hetzner CAX), regenerate the pins first: `scripts/deploy/keel-freeze.sh`.

---

## 10. Stopping, and why entries stay halted

`keel-supervise.py` is PID 1 in every container. On SIGTERM, in the engine container, it:

1. writes `halt_new_entries = True` through `params_store.set_param` (origin `human`, reason
   recorded, audited in `param_changes`);
2. sends the child **SIGINT**, not SIGTERM;
3. the child — `keel-run-engine.py` — calls `engine.stop()` and holds the interpreter open until
   `engine_loop` has returned.

Step 1 is the half that matters. An interrupted *management* pass leaves a stop where it was. An
interrupted *entry* can leave a position the database does not know about. Closing the entry
gate first means that by the time anything is signalled, no new position can be opened.

Step 2 is not a stylistic choice. Nothing in this tree installs a SIGTERM handler, so SIGTERM is
an immediate kill. SIGINT is the one signal the code already answers: werkzeug unwinds
`app.run()`, and `NewsAgent.run()` breaks its own loop on `KeyboardInterrupt`.

Step 3 is why the engine command is `keel-run-engine.py` and not `python server.py`. `server.py`
starts `engine_loop` as a daemon thread and never calls `engine.stop()`, so a bare SIGINT unwinds
Flask and destroys that thread wherever it happens to be. The wrapper sets the stop event the
loop already checks and waits for the loop to leave the cycle. A healthy stop looks like this,
and takes about two seconds:

```
[supervise] received SIGTERM
[supervise] halt_new_entries=True written and audited
[supervise] sending SIGINT to child pid 7
[run-engine] received SIGINT — asking engine_loop to finish its cycle
engine: stopped
[run-engine] engine_loop returned after 0.0s — stop was clean
[supervise] child exited cleanly (status 0)
```

`engine: stopped` is `engine_loop`'s own last line. Seeing it *before* the process exits is the
whole point: the loop left the cycle, it was not destroyed inside one.

> **If you remember the old behaviour:** every stop used to take about a minute and end with
> `child ignored SIGINT after 20s — escalating to SIGTERM` and `child still alive — SIGKILL`.
> Both lines were false. The supervisor ran its stop sequence inside the signal handler while
> `main()` sat in `_child.wait()` with no timeout, which holds `Popen._waitpid_lock` across
> `waitpid()`; the handler's own timed waits could never take that lock, so they always expired
> and always escalated — on a child that had already exited cleanly. If you have logs from
> before this change showing a SIGKILLed engine, they are not evidence of anything.

This happens on **every** stop — `docker compose stop`, `down`, a host reboot — because it lives
in the container, not in a script someone has to remember to run.

**Consequence, and it is deliberate: entries stay halted across the restart.** An engine that
silently resumes trading after an unexplained stop is failing open. Resume is a decision:

```bash
scripts/deploy/keel-resume.sh     # prints the open book and the kill-switch state, then asks
```

`keel-resume.sh` never touches `trading_mode`. Paper → live is `live_switch.py`'s two-step
confirm behind the promotion gate, and no deploy script may shortcut it (CLAUDE.md invariant 2).
`test_deploy_assets.py` asserts that no script in `scripts/deploy/` writes `trading_mode`.

### The stops that never reach the handler

A segfault, an OOM kill, `docker kill`, a host that loses power: none of them run step 1. The
drain enforces no-self-resume for SIGTERM only, and `restart: unless-stopped` would otherwise
bring the engine back with the entry gate exactly as open as the crash left it. The stops you
cannot explain are precisely the ones that must not resume unattended.

So the gate is also closed on the way **in**. The supervisor writes
`keel_supervisor_run_state = running` to the settings table before it launches the engine, and
rewrites it to `stopped_drained` only when a drained stop completes. A start that finds `running`
still there knows the previous run died without draining:

```
[supervise] previous run ended WITHOUT a drain (run state was still 'running').
[supervise] segfault, OOM kill, `docker kill` or a host that lost power — the
[supervise] handler never ran, so the entry gate is however the crash left it.
[supervise] halt_new_entries=True written and audited
[supervise] entries are HALTED. Positions are still managed. Resume with
            scripts/deploy/keel-resume.sh once you know what happened.
```

The engine still starts. That is deliberate — open positions need managing, and their stops are
at the venue either way. What it does not do is take a new position before a human has looked.

Two related behaviours worth knowing:

- If only the **child** dies (the common crash), the supervisor sees the exit status directly and
  closes the gate immediately, before the container even exits:
  `child exited on its own with status -11 (nothing drained it)`.
- If the supervisor cannot classify the previous stop **and** cannot close the gate — an
  unreadable database — it **refuses to start** with exit 3 rather than run an engine whose entry
  gate is in an unknown state. Repair the volume (§7 restore, §11), or, having checked
  `halt_new_entries` by hand, start once with `KEEL_STARTUP_GATE=0`.

One-off commands do not trip any of this. `docker compose run --rm engine python
tests/test_risk_rails.py` shares the role and the entrypoint but is not a service start, so it
neither halts entries nor clears the marker a running engine left behind.

`docker kill` is worth a note of its own: Docker treats it as a user-initiated stop, so
`unless-stopped` does **not** restart afterwards. The marker survives, and the gate closes on
whatever start comes next.

---

## 11. When a container will not start

Work down this list. Most of it is the volume.

```bash
docker compose ps -a                    # exit code and status
docker compose logs --tail=100 engine   # the actual traceback
docker inspect keel-engine --format '{{.State.ExitCode}} {{.State.OOMKilled}} {{.State.Error}}'
```

| Symptom | Cause | Fix |
|---|---|---|
| `PermissionError: '/app/trading-bot/data/trading.db'` | volume predates the image and is owned by root or another uid. Docker seeds ownership only into an **empty** named volume. | `docker compose down` then `docker run --rm -v keel_keel_data:/d -v keel_keel_state:/s alpine chown -R 10001:10001 /d /s` |
| `sqlite3.OperationalError: unable to open database file` | volume not mounted, or the mount path drifted from `/app/trading-bot/data` | `docker compose config \| grep -A2 volumes`; `docker volume inspect keel_keel_data` |
| `database is locked`, or corruption after a clean start | volume on NFS/CIFS/Storage Box/virtiofs — see §8 | move it to local disk; restore from backup |
| Exits 137, `OOMKilled: true` | out of memory | check `free -h`; confirm swap is on; raise the compose memory limit or the box. The engine is `oom_score_adj -500`, so if it is the victim, the box is genuinely too small. |
| `newsagent` never starts, no logs | `depends_on: engine: service_healthy` and the engine is not healthy | fix the engine first; `docker inspect keel-engine --format '{{json .State.Health}}'` |
| Engine `(unhealthy)` but logs look fine | port answers, `engine_heartbeat_t` stale — the daemon thread is dead or wedged | `docker compose exec engine python /app/bin/keel-healthcheck.py --role engine` for the reason; restart; if it recurs, that is a real engine bug, not a deployment one |
| Dashboard container up, tunnel refuses | it bound container-loopback and nothing can reach it | `KEEL_DASHBOARD_HOST` must be `0.0.0.0` in compose (§6 explains why that is still loopback-only externally) |
| `stop` takes ~30s+ and logs `child has not exited Ns after SIGINT` | the loop really is stuck in a cycle — a venue call that is not timing out. Look for `ENGINE LOOP DID NOT RETURN` from `[run-engine]` just before it | that is an engine bug, not a deployment one; the container exits 75 to say the stop was not clean. Reconcile against the venue before resuming entries |
| Container killed mid-cycle on reboot | `shutdown-timeout` missing from `/etc/docker/daemon.json` | add it (§3), `systemctl restart docker` |
| Build stops with `BUILD REFUSED - a credential is present` | `config.yaml` has a filled-in `bot_token`/`chat_id`, or a token pasted somewhere in a YAML file | empty the field, put the value in the dashboard, rebuild. Rotate it if it was ever committed (§5) |
| Engine exits 3 immediately, `STARTUP GATE FAILED` | the supervisor could not read the run-state marker or close the entry gate — usually an unreadable or unwritable `data/` volume | fix the volume (rows above), or restore (§9). Only after checking `halt_new_entries` by hand: start once with `KEEL_STARTUP_GATE=0` |
| Every start logs `previous run ended WITHOUT a drain` | something is killing the container without SIGTERM — OOM, or a `docker kill` in a script | check `docker inspect ... {{.State.OOMKilled}}` and `RestartCount`. Entries are halted each time, which is correct; the crash is the thing to fix |
| Any `.sh` dies with `set: pipefail: invalid option name` | CRLF line endings — the tree was copied from Windows rather than cloned | `git clone` on the host, or `sed -i 's/\r$//' scripts/deploy/*.sh` |
| Build fails resolving packages | pinned set no longer available, or wrong arch | `scripts/deploy/keel-freeze.sh`, read the diff |

Nuclear option, and note what it does **not** delete:

```bash
docker compose down          # removes containers. Volumes survive.
docker compose up -d --build
```

Never `down -v`. That deletes the gate evidence.

---

## 12. The awkward parts, written down

**`server.py` still never calls `engine.stop()`.** `engine.py` has the stop event and the
`stop()` that sets it; `server.py` starts `engine_loop` as a daemon thread, blocks in
`app.run()`, and installs no signal handler. Run `python server.py` directly — outside the
container, as `CONTRIBUTING.md` still tells you to for development — and Ctrl-C destroys the loop
thread wherever it is. `keel-run-engine.py` supplies the missing handler from the deployment
side, which means the guarantee in §10 holds **only inside the container**. The durable fix is
that handler living in `server.py` and joining the loop thread; `server.py` is not part of this
change. Delete the wrapper the day it lands.

**Only `engine_loop` gets the deterministic stop.** `agent_loop`, the news-calendar refresher and
the safety monitor are still daemon threads that die with the interpreter. They write through
`params_store`, which is transactional, and none of them holds a position — the engine loop is
the one whose interruption can leave the database disagreeing with the venue. It is a smaller
gap than the one that was there, not no gap.

**`POST /api/commands` still authenticates nothing.** The loopback binding is currently the only
thing protecting it. ARCHITECTURE-V3 §5 item 3 is the fix (require the dashboard token) and it
is not in this change's remit. Until it lands, do not expose 8766 to a tailnet without a proxy
that authenticates, and be aware that anything else running on the host can reach it.

**Three containers share one SQLite file.** That is what the code already does across three
processes, and WAL supports it — on a local filesystem, on one host. It does not extend to two
hosts, and there is no configuration in this repo that would make it safe to try.

**Nothing pages you.** Covered in §7, repeated here because it is the biggest gap and it is not a
deployment problem: `alerts.py` is finished, tested and orphaned. Wiring it (ARCHITECTURE-V3
Increment 1) buys more safety than any hosting choice in §2.

**Compose resource limits are not cgroup guarantees under every driver.** `deploy.resources.limits`
works with Compose V2 on Linux. On other runtimes it can be silently ignored — check
`docker stats` rather than assuming.

**`docker compose config` is a client-side lint.** It validates schema and interpolation. It does
not tell you the image builds, and it does not tell you the engine survives a stop. Only running
it does.

**`test_deploy_assets.py` does not run in CI yet.** `.github/workflows/tests.yml` globs
`tests/test_*.py` with `working-directory: trading-bot`, so a file under `scripts/deploy/` is
invisible to it — and the two assertions that matter most here (no port on 0.0.0.0, no
credential path in the build context) are therefore unenforced on push. Either add a step that
runs `python scripts/deploy/test_deploy_assets.py` from the repo root, or move the file to
`trading-bot/tests/`; it needs no changes to run from there. Whoever owns the workflow should
pick one.

---

## 13. Relationship to the Windows runbook

`docs/DEPLOYMENT-WINDOWS.md` and `scripts/windows/` stay. Anyone still running the MT5 leg needs
them, and they work. They are no longer the primary path, and no new work should assume them.

Cross-references that are now stale and belong to whoever owns those files:
`ARCHITECTURE-V3.md` §4.1 (the node is a Windows box), §10 (DEPLOYMENT-WINDOWS.md is the
canonical node runbook), and `CLAUDE.md`'s run instructions.
