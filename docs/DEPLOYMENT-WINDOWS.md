# Windows deployment — Scheduled Tasks

The only autostart this repo shipped was macOS launchd (`install-multiasset-services.sh`,
`watchdog-install.sh`). The host that actually runs the paper clock is Windows 11, so until
now nothing survived a reboot: Windows Update restarts the box overnight, the engine does not
come back, and the gap lands in the middle of the ≥50-closed-paper-trades run the promotion
gate depends on. "Someone remembers to start it again" is not a deployment.

`scripts/windows/` is the Windows half. Same four verbs as the launchd script, plus two that
only exist because Windows needs them.

```
scripts/windows/
  keel-services.ps1      install | status | uninstall | restart | probe | selftest
  keel-service-run.ps1   wraps one python process: log redirection + rotation
  keel-health-probe.ps1  the watchdog — restarts whichever port stopped answering
  keel-common.ps1        shared paths, rotation, HTTP probe, restart budget
```

Nothing here touches a risk rail. `engine.py` is unchanged, the promotion gate is unchanged,
and going live still needs both halves of the double gate (`live_switch` **and**
`AllowTradeExecution` in the EA). This is plumbing that keeps the clock running.

## What gets installed

Four tasks in the Task Scheduler folder `\Keel\`:

| Task | Runs | Listens | Notes |
|---|---|---|---|
| `Keel-server` | `server.py` | 8766 | engine, EA endpoints, legacy dashboard |
| `Keel-dashboard` | `dashboard_api.py` | 8767 | control dashboard, 127.0.0.1 only |
| `Keel-newsagent` | `news_agent.py` | — | separate process by design |
| `Keel-healthprobe` | `keel-health-probe.ps1` | — | every 2 min: probe, restart, record |

Each service task: trigger **at startup** (30 s delay), principal **SYSTEM** (so it runs with
nobody logged on), **restart on failure** every minute, **no execution time limit**, one
instance only.

### Why Scheduled Tasks and not NSSM

It ships with Windows. A machine that manages money should not need a third-party service
wrapper downloaded from somewhere to survive a reboot. Everything NSSM is usually reached for
— run at boot, run logged off, restart on crash, no console window — is a Scheduled Task
setting.

Two things Task Scheduler genuinely does not do, which is why there are two extra scripts:

- **No stdout redirection.** launchd has `StandardOutPath`; Task Scheduler has nothing.
  `keel-service-run.ps1` owns the file handle instead — and owning it is also what makes
  rotation possible, because these processes are meant to run for months without exiting, so
  a rotate-on-restart scheme would never fire once.
- **Restart-on-failure only fires when the process exits.** The failure that matters here is
  the process still alive with a wedged port. That is what `keel-health-probe.ps1` is for; it
  is the equivalent of the `watchdog-install.sh` reachability check, on a timer.

## Before you install

- **The repo must be on plain local disk.** Not OneDrive, not Dropbox. `data/trading.db` is
  SQLite in WAL mode and a sync agent writing underneath the engine corrupts the exact file
  the kill switches read (`CONTRIBUTING.md`). The installer refuses a cloud-synced path;
  `-AllowCloudSyncedPath` overrides it and you should not use it.
- **A real venv interpreter**, with the requirements installed into *that* interpreter. The
  installer runs a preflight inside it and refuses to register anything if `yaml`, `flask`,
  `requests`, `fastapi`, `uvicorn` or `httpx` is missing. It also refuses the Microsoft Store
  `python.exe` alias in `AppData\Local\Microsoft\WindowsApps\` — that is a reparse stub that
  only resolves inside an interactive session and launches nothing under SYSTEM.
- **An elevated PowerShell** for `install`, `uninstall` and `restart`. `status`, `selftest`
  and `probe -DryRun` do not need it.

## Install

Look first, then commit. Neither of these first two changes anything:

```powershell
cd C:\dev\slc-trading-bot\scripts\windows
.\keel-services.ps1 install -DryRun -Python C:\venvs\keel\Scripts\python.exe
.\keel-services.ps1 selftest -Python C:\venvs\keel\Scripts\python.exe
```

`-DryRun` prints the exact executable, argument string and working directory for every task.
`selftest` proves the parts that are worth proving rather than assuming on this particular
host: the interpreter really has the dependencies, the argument string really survives this
machine's profile path, both child streams really land in the log, the rotation cap really
holds, and the watchdog's restart budget arithmetic is right. It registers nothing.

Then, in an **elevated** PowerShell:

```powershell
.\keel-services.ps1 install -Python C:\venvs\keel\Scripts\python.exe
```

Useful switches:

| Switch | Default | Why you would change it |
|---|---|---|
| `-RunAs System \| CurrentUser` | `System` | `CurrentUser` uses S4U: still passwordless, still runs logged off, but the account needs the *Log on as a batch job* right. Use it if the services must reach something only that user can. |
| `-LogMaxMB` / `-LogKeep` | 20 / 5 | Total log ceiling is roughly `LogMaxMB × (LogKeep + 1)` per service. |
| `-ProbeMinutes` | 2 | How often the watchdog looks. |
| `-ProbeGraceSeconds` | 180 | How long after a restart the watchdog leaves a service alone to bind. |
| `-MaxRestartsPerHour` | 5 | Past this the watchdog stops and records `giveup` instead of looping. |
| `-StopExisting` | off | Kill a manually-started python holding 8766/8767 before registering. Off by default — that process may be managing stops. |
| `-NoStart` | off | Register but do not start; they come up at the next boot. |

Ports come from `config.yaml` (`server.port`, `dashboard.port`), read through the interpreter
being installed, not guessed.

### The install config

`install` writes `trading-bot/state/windows-services.json` — the absolute interpreter path,
the repo root, the ports, the log caps, the probe budget. That directory is gitignored, which
is the point: the machine-specific paths never enter the repo, and the tasks themselves carry
only the wrapper path plus a service name.

**Re-run `install` after** moving the repo, rebuilding or moving the venv, or changing a port
in `config.yaml`. The wrapper refuses to start if the config was written for a different repo
root, rather than silently running against another clone's database.

## Verify

```powershell
.\keel-services.ps1 status
```

```
repo   : C:\dev\slc-trading-bot
config : C:\dev\slc-trading-bot\trading-bot\state\windows-services.json  (written 2026-08-08T22:17:11, run as System)
python : C:\venvs\keel\Scripts\python.exe

TASK                 STATE      LAST RUN             LAST RESULT  WRAPPER PID
Keel-server          Running    2026-08-08 22:31:04  0x41301      18244
Keel-dashboard       Running    2026-08-08 22:31:04  0x41301      18992
Keel-newsagent       Running    2026-08-08 22:31:05  0x41301      19120
Keel-healthprobe     Ready      2026-08-08 22:32:00  0x0          -

port 8766: answering HTTP 200 on /api/pairs   [python pid 42152]
port 8767: answering HTTP 200 on /api/health   [python pid 29752]

logs:
  win_server.log                4.2 MB  (+2 rotated)
  ...

watchdog (last 5 events):
  (none — nothing has needed restarting)
```

`LAST RESULT` is the raw Task Scheduler code: `0x0` finished cleanly, `0x41301` currently
running, `0x41303` has never run, `0x1` the wrapper exited non-zero (normal after a service
crash — the task restarts), `0x2` Windows could not find the executable.

Two things `status` will not tell you, so check them yourself:

- **It really comes back from a reboot.** Reboot, and *before logging in*, hit
  `http://<host>:8766/api/pairs` from another machine. Logging in first proves nothing —
  a task with the wrong principal starts on logon and looks identical.
- **The EA can reach it.** See the firewall note below.

## Reading the logs

Everything lands in `trading-bot/state/` (gitignored, never commit it — it holds credentials
and live runtime data):

| File | What |
|---|---|
| `win_server.log`, `win_dashboard.log`, `win_newsagent.log` | stdout **and** stderr of each service, plus `[keel-run]` lines from the wrapper |
| `win_<name>.log.1` … `.5` | rotated, newest first |
| `win_healthprobe.log` | every watchdog pass, healthy or not |
| `keel_watchdog.jsonl` | append-only ledger: one JSON line per restart, giveup or refusal |
| `win_bootstrap.log` | failures that happened *before* a log could be opened — read this first when a task starts and instantly dies |
| `news_agent.log`, `news_decisions.jsonl` | the news agent's own logs, unchanged |

```powershell
Get-Content C:\dev\slc-trading-bot\trading-bot\state\win_server.log -Tail 50 -Wait
```

Rotation is by size and happens inside the running process: at the cap the wrapper closes the
file, shifts `.1`→`.2`→…, reopens, and notes the roll in the new file. A single line can
overshoot the cap by its own length — the roll happens after the write that crosses it — so
treat the cap as a bound, not an exact size.

## The health probe

Every `-ProbeMinutes`, for each service:

1. `GET http://127.0.0.1:<port><path>` (`/api/pairs`, `/api/health`). **Any** HTTP status
   counts as alive — this is a liveness probe, not a correctness probe. Restarting a live
   engine because a route was renamed would be worse than the symptom.
2. On failure it waits 5 s and asks once more. One timeout is a hiccup; a restart is the
   expensive action, so two failures five seconds apart is the bar.
3. Then, in order, it stands aside if: the task is not registered, the task is **Disabled**
   (a human turned that off on purpose), a restart happened inside the grace window, or the
   service has already been restarted `-MaxRestartsPerHour` times in the last hour. That last
   one records `giveup` and stops — a restart loop on a trading engine churns the DB and hides
   the real fault, so it is deliberately allowed to stay down until someone looks.
4. Restarting stops the task, waits up to 20 s for the port to free, and reaps a leftover
   listener **only if it is a python process**. If something else owns the port it records
   `foreign_port_owner` and does nothing.

`news_agent.py` has no listener, so the only honest check is whether its task is running.

Run it by hand any time:

```powershell
.\keel-services.ps1 probe -DryRun     # report only, restarts nothing, writes no events
.\keel-services.ps1 probe             # elevated: will actually restart
```

**Known gap:** a `giveup` is recorded and shown by `status`, but nothing sends it to Telegram
or Discord. The notifier reads its credentials from the runtime DB, which would mean a python
call inside the watchdog; that was left out rather than added carelessly. Until it exists,
`keel_watchdog.jsonl` is something a human has to look at.

## Restart and uninstall

```powershell
.\keel-services.ps1 restart                      # all three services (elevated)
.\keel-services.ps1 restart -Service server      # just one
.\keel-services.ps1 uninstall                    # elevated
```

`uninstall` stops and removes all four tasks and the `\Keel\` folder. It leaves the logs, the
watchdog ledger and `windows-services.json` in `state/` — that is the record of what the
machine did while it was unattended, and nothing in this repo deletes runtime state. Nothing
runs afterwards; start by hand with `python server.py` from `trading-bot/`.

## When a service will not start

Work down this list. Most of it is visible in `state/win_bootstrap.log` or `win_<name>.log`.

| Symptom | Cause and fix |
|---|---|
| Task result `0x2`, nothing in any log | Windows could not find the executable. Check the wrapper path in the task's Actions tab still exists — this happens after the repo is moved. Re-run `install`. |
| Task starts, exits immediately, `win_bootstrap.log` says *no install config* or *config was written for …* | `state/windows-services.json` is missing or belongs to another checkout. Re-run `install`. Exit code 78. |
| `ModuleNotFoundError` in `win_<name>.log` | Dependencies are in a different interpreter than the one registered. `& "C:\venvs\keel\Scripts\python.exe" -m pip install -r trading-bot\requirements.txt`, then `install` again. `selftest` catches this before you register anything. |
| `OSError: [WinError 10048] … address already in use` | A manually-started python still holds the port. `.\keel-services.ps1 status` names the pid. Stop it, then `restart`. `install -StopExisting` does it for you. |
| Runs fine when you start the task by hand, fails at boot | Almost always a path the SYSTEM account cannot resolve: a mapped drive, a UNC path, or a venv inside a OneDrive-synced profile where Files On-Demand placeholders never hydrate for SYSTEM. Put the venv somewhere plain, e.g. `C:\venvs\keel`. |
| `-RunAs CurrentUser` task shows *The user account does not have permission* | S4U needs the *Log on as a batch job* right for that account (`secpol.msc` → Local Policies → User Rights Assignment). Or use the default `-RunAs System`. |
| Everything green locally, but the EA on the VPS cannot reach 8766 | No firewall rule. An interactive first run pops the Windows Firewall prompt; a SYSTEM task at boot has nobody to click it, so the rule is never created. Add it explicitly, scoped to the tunnel, never to the open internet: `New-NetFirewallRule -DisplayName "Keel engine 8766" -Direction Inbound -Protocol TCP -LocalPort 8766 -RemoteAddress <ea-host-ip> -Action Allow` |
| Task quietly stopped after about three days | The default `ExecutionTimeLimit` is 72 hours. The installer sets it to unlimited; if you edited the task by hand in the GUI, check *Stop the task if it runs longer than* is unticked. |
| Task shows `Disabled` | Something or someone disabled it. The watchdog deliberately will not re-enable it. `Enable-ScheduledTask -TaskPath '\Keel\' -TaskName Keel-server`. |
| Script refuses to run: *running scripts is disabled on this system* | The registered tasks pass `-ExecutionPolicy Bypass` and are unaffected. To run the installer interactively: `powershell -ExecutionPolicy Bypass -File .\keel-services.ps1 status`. |

## The awkward parts, written down

These are the details that make Windows deployment different from the launchd script, and the
reason `selftest` exists rather than a README claim that it works.

- **The user profile path contains an unmatched `(`** (`C:\Users\RaviPotluru(Stottand\…`).
  Unquoted, PowerShell's own parser stops at it. Every path in the task argument string is
  inside double quotes for that reason, and `selftest` runs the exact registered argument
  string through `powershell.exe` on this machine and checks the exit code, rather than
  assuming the quoting holds.
- **The venv lives outside the repo.** Its absolute path is pinned in
  `state/windows-services.json` at install time; the tasks themselves carry only the wrapper
  path and a service name, so the interpreter can move without re-registering four tasks —
  one `install` re-run rewrites it.
- **Scheduled Tasks have no PATH, no cwd and no user environment.** Everything is absolute:
  the full path to `powershell.exe` under `%SystemRoot%`, the full path to the wrapper. The
  wrapper sets the working directory to `trading-bot/` itself, because `news_agent.py` opens
  `state/news_agent.log` by relative path.
- **A clean exit is still a failure.** Task Scheduler only honours restart-on-failure for a
  non-zero exit code, so the wrapper translates a child's `0` to `1` on the way out. A service
  process that returns has failed whatever it claims.
- **Default task priority is 7**, which puts the process in `BELOW_NORMAL_PRIORITY_CLASS`. An
  engine polling on a clock should not be, so the installer sets priority 4.
- **UTF-8 is forced** (`PYTHONUTF8`, `PYTHONIOENCODING`) for the child processes. Under a
  SYSTEM logon the ANSI code page decides text encoding, and one non-ASCII news headline is
  otherwise a `UnicodeEncodeError` that takes the process down. The Mac never saw this because
  its locale is UTF-8 already.
- **The `.ps1` files carry a UTF-8 BOM.** Windows PowerShell 5.1 decodes BOM-less files as
  ANSI and mangles every non-ASCII character in them, including the ones in the messages it
  prints back to you.
- **Orphans.** Task Scheduler normally kills the whole tree, but if a python survives a hard
  stop it would hold the port and the restart would fail on bind. The watchdog reaps a
  leftover listener, and only ever a `python*` one.
