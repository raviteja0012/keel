# Architecture V3 — the deployment topology decision

> **Superseded — historical.** This predates the venue/host split, the Robinhood adapter, the strategy hosts, the three-state idempotency probe and the current dashboard. For the platform as it actually is now, read [PLATFORM.md](PLATFORM.md). Kept for the design history and the reasoning that led here.


**Status:** decided. Supersedes the deployment sections of ARCHITECTURE-V2; retires `docs/LIVE-EXECUTION-VPS.md`.
**Decided against:** repo HEAD `7821968` (`feat(research): ingest paid research newsletters from the mailbox`), working tree `C:\dev\slc-trading-bot`.
**Not** the Desktop/OneDrive clone. That copy is stale, lacks `venues.py`, `brokers/`, `alerts.py`, `reconcile.py` and `docs/ARCHITECTURE-V2.md`, and sits under file sync where a live SQLite WAL triple would be a sync target. Delete it.

---

## 0. Verified state of the world

Every number below I checked against the tree at HEAD before writing a word of this. They are the facts the decision rests on.

| Claim | Verified |
|---|---|
| Closed trades | **The database is empty.** `trades 0, signals 0, decisions 0, bars 0, equity 0, commands 0, param_changes 0`. 37 `settings` rows. Not "no closed trades" — no data at all. The clock has never started. |
| Promotion gate | `analysis.py:215 GATE_MIN_TRADES = 50` per strategy × asset-class cell. |
| CCXT execution | **Does not exist.** `grep -n "venues\|ccxt\|adapter" trading-bot/engine.py` returns nothing. `requirements.txt` has no `ccxt`. `feed_state["prices"]` has one writer: `engine.ingest_feed`, called only from the MT5 EA POST. 100% of execution today crosses one Windows box. |
| The pager | **Built and connected to nothing.** `alerts.py` (full P1/P2/P3/P4 taxonomy, `KINDS` registers `feed_loss_live`, `db_integrity_live`, `clock_skew`, `kill_switch_fired`, `bad_ticket`, `recon_drift` as P1) and `reconcile.py` are imported by nothing except `tests/test_alerts.py` and `tests/test_reconcile.py`. Both suites pass. |
| Deadman | None. `engine.py:880` writes `engine_heartbeat_t`; the only reader is `analysis.health()`, which renders to a dashboard. Pull-only telemetry tells you the engine is dead if and only if you were already looking. |
| Windows autostart | **Exists at HEAD.** `scripts/windows/keel-services.ps1` (`install\|status\|uninstall\|restart\|probe\|selftest`) registers `Keel-server`, `Keel-dashboard`, `Keel-newsagent` as boot-start Scheduled Tasks under `NT AUTHORITY\SYSTEM` with restart-on-failure; `keel-health-probe.ps1` registers `Keel-healthprobe` to catch the failure Task Scheduler cannot — process alive, port wedged. `keel-services.ps1:140` refuses to install under `\OneDrive`, `\Dropbox`, `\iCloudDrive`, `\Google Drive`. `docs/DEPLOYMENT-WINDOWS.md` is the runbook. |
| Feed guard | `engine.py:894 if feed_age > 60:` → `continue` **before** `manage_open_trades(p)` at `engine.py:902`. It is a global loop bypass, not a per-venue guard. A stale EA feed suspends TP1 partials, breakeven, trailing and paper stop evaluation for the entire book. |
| Command queue race | `storage.py:370 next_command()` is three separate statements — expire-UPDATE, candidate SELECT, claim-UPDATE — each taking and releasing the module `RLock` independently, under `server.py:628 app.run(..., threaded=True)`. Two concurrent EA polls can be served the same `open_trade`. |
| Remote close | `server.py:144 @app.route("/api/commands", methods=["POST"])` has no authentication of any kind; it validates only that `type ∈ (trail_sl, move_sl_be, close_trade)` and that a ticket is present. `config.yaml:9 server.host: 0.0.0.0`. |
| Credentials | `venues.py:28 _SECRET_FIELDS = ("api_key","api_secret","password")` stored inside the `"venues"` settings row via `storage.set_setting`. Plaintext at rest. `dashboard_api.py:173 /api/settings` returns `storage.all_settings()` masking exactly four named keys — the venue secrets are not among them. |
| Recovery on Windows | `storage.py:185 sqlite_bin = shutil.which("sqlite3") or "/usr/bin/sqlite3"`. Stock Windows has neither, so `_attempt_recover()` returns False, `_mark_suspect` latches, and invariant 7's recovery arm is dead code on the only OS that can host MT5. |
| The good news | `SLCDataBridge.mq5:749` submits with the stop attached; `:835`/`:840` refuse to move a stop backward. Combined with `storage.py COMMAND_TTL_S = 300` / `RESEND_GRACE_S = 120`, a network partition today costs give-back — the trail, the breakeven, the news cut — **not** the stop. |
| Licence | `LICENSE.md`: "Copyright © 2026 Proaxive / Shakeeb Ahmed… No part may be copied, distributed, published, or disclosed outside the team without the owner's written permission." Every topology on the table distributes. |

---

## 1. The decision

**Keel is a fleet of single-tenant nodes with an owner-hosted read-only console.** Each person runs one always-on Windows host that they own: MT5 terminal, the SLCDataBridge EA, `server.py`, `engine.py`, `dashboard_api.py` and SQLite, all on that one box, all on loopback, installed by `scripts/windows/keel-services.ps1` which already exists. Each node dials **out** over HTTPS to a console the owner hosts on Vercel + Supabase. The console is a mirror and a mailbox, never a controller: the only thing that travels back is `halt_new_entries = True`, signed, nonce'd, 60-second TTL, one origin, one key, one direction. The single reason this settles it: **the engine never moves — only a copy of the evidence does.** Every other proposal on the table pays, in some currency, for relocating a stateful loop that manages real stops away from the disk that holds its own positions; this one relocates nothing, which is why its week-one deliverable requires zero new code, why nobody's exchange key ever leaves the machine that owns it, and why the one component that genuinely cannot be retrofitted later — the captured record of trades as they close — gets built early and cheaply instead of being reconstructed from files that were never written.

**This week's deliverable, in full:** clone to `C:\Keel\slc-trading-bot` on a Windows box that never sleeps, flip one line in `config.yaml`, run `keel-services.ps1 install`, point the EA at `127.0.0.1`. The clock starts. Nothing is built.

---

## 2. Adjudication — where the judges disagreed, and who was right

Three judges. Two ranked #5 first, one ranked #2 first with #5 a close second and described #5 as "#2 plus visibility". Their real disagreements are narrower and more useful than their rankings.

**On whether the console should be built early or deferred. Judge 1 said defer it (the 40–60h Next.js build buys zero safety and blocks the clock). Judge 3 said build it (the read model is the evidence apparatus, and uncaptured trades are gone forever). Both are right, because they are talking about two different artefacts and neither noticed.** The console is *capture* plus *rendering*. Capture is `projection.py` + `publisher.py` + a Supabase schema — roughly three evenings, and it is genuinely irretrievable if skipped, because a trade that closed while nothing was recording its cost model, broker fingerprint and git SHA can never be made commensurable later. Rendering is the Next.js app, and Judge 1 is right that it is 40–60 hours that buys nothing until there is something to render. **Ruling: build capture in Increment 3, before the first closed trade. Defer rendering to Increment 5, after it.** This resolution is not a compromise; it is what both judges were actually arguing for.

**On the substrate. Judge 2 ranked #2 (docker compose) first on the grounds that nothing risk-bearing moves and it is the fastest path to a running clock. Judge 2 was right about the constraint and wrong about the substrate.** "Nothing risk-bearing moves" is the correct test, and this decision satisfies it identically — `engine.py`, `strategy.py`, `storage.py`, the rails, the gate, `params_store.py`, `live_switch.py` are all untouched in Increments 0–2. But for *this* owner Docker is a Linux-shaped answer to a Windows-shaped problem. MT5 forces a Windows host into existence; #2 then adds a Linux host beside it and a Tailscale hop on the engine↔EA link, which is the most safety-critical wire in the system. If instead the friend runs Docker Desktop on Windows to avoid the second box, `trading.db`'s WAL and `-shm` land on virtiofs/gRPC-FUSE — #2's own failure mode 4. And it spends 35–45 hours rebuilding what `scripts/windows/` already does natively under SYSTEM with a port-probe watchdog. Judge 2's second-place placement of #5 with the note "#5 is #2 plus visibility" is the accurate summary; the ranking inverts it only because Judge 2 priced the console at full cost, which the ruling above removes.

**On whether the topology choice is second-order. The 3am lens said yes — all five designs ship the identical 3am outcome, so wiring `alerts.py` matters more than choosing a host. It was right, and it is the single most valuable finding in the whole exercise.** I verified it: `alerts.py` and `reconcile.py` have no importers outside their own tests. The pager is finished, tested and orphaned. Increment 1 of this plan is that lens's Phase 0, almost verbatim, and it is deliberately placed before anything topological.

**On the feed guard fix. The 3am lens said "split it so it suspends entries but never suspends `manage_open_trades`". Judge 1 objected that management on a stale feed means computing a structure trail off the last price a dead feed gave you. Judge 1 is right and the refinement is adopted:** on a stale feed, management runs in de-risk-only mode — breakeven and stop-tightening permitted, trail recomputation and new TP levels forbidden. Monotonically de-risking actions on a slightly stale price are strictly safer than no action; a trail recompute is not.

**On the 100-user columns. The regulatory lens said strike them (17 CFR 4.14(a)(10) caps the CTA exemption at 15 persons in a rolling 12 months plus no public holding-out). Judge 3 objected that choosing an architecture which structurally cannot reach 16 users, to stay under a limit at 15, optimises for a constraint the owner might deliberately clear. Both are right in their own scope and the ruling splits them:** the 100-user columns are struck as *planning targets* — no dollar in this document prices them — but the schema keeps pooling *possible*, because Judge 3's point that fleet sample rate is the only thing a fleet is actually worth (100 strategies × 3 asset classes × 50 trades ≈ 15,000 closed trades to validate a library, unreachable on one account) is correct and permanent. 15 is a counter in the `nodes` table, not a mood.

**On proposal #4's factual base. Judge 1 and Judge 3 both flagged that it analysed the stale OneDrive clone and declared `venues.py`, `brokers/`, `alerts.py` and `reconcile.py` do not exist. Verified: they all exist at HEAD.** A proposal that got the tree wrong cannot be trusted on what its port costs, and its 79-call-site count is against the wrong file set.

**On proposals #1 and #3 both misreading `engine.py:894`.** #1 said the feed guard "covers MT5 but not CCXT"; #3 called it "already the correct network-partition behavior". Verified: the `continue` sits before `manage_open_trades` at `:902`. It is neither per-venue nor correct — it is a global bypass of stop management, in every asset class, on six missed 5-second pushes.

**On what actually introduces unbounded loss. All three judges converged and they are right:** wiring `ccxt_venue` into `engine.py` — not the hosting choice — is the event that creates the first client-side stop and therefore the first genuinely unbounded position in this codebase's history. That wiring is gated behind `supports_attached_stop` and a host-continuity rail, and it is explicitly out of scope for this decision.

**On Judge 1's decisive finding.** `scripts/windows/` exists at HEAD and both lenses were written without knowing it (the 3am lens states "`watchdog-install.sh` is macOS launchd and does not port to the Windows host MT5 forces you onto" — true two commits before HEAD, false now). Verified. It retires the entire provisioning half of proposals #1 and #2 and it is why Increment 0 costs zero hours.

---

## 3. Why each rejected topology was rejected

**#1 — Tauri local-first desktop appliance.** This proposal contains the best individual code changes in the entire set and most of them are being adopted: `secrets_store.py` behind `venues.py`'s two-function seam, the `config.yaml` loopback flip, `supports_attached_stop`, resume detection, the flat-book update gate, the pre-migration DB backup. Its decisive geometry insight — MT5 already requires a Windows box, so put the engine on the box that must exist and `ServerHost` becomes `127.0.0.1` — is adopted wholesale and is the reason `docs/LIVE-EXECUTION-VPS.md` is retired. What is rejected is the wrapper: 70–110 hours of Tauri, PyInstaller and code-signing (3–10 business days of certificate identity validation that no amount of effort compresses) to rebuild, worse, what four PowerShell files already do. It also fails the brief's primary want outright — "available online, not chained to one laptop" — and its author concedes the honest product name is "self-hosted appliance that must not be run on a laptop", which is precisely this decision's node minus nine weeks. Its stated fatal flaw, the naked CCXT position, is not reachable today because CCXT is unwired; its *real* fatal flaw is the one it states second, that a duty-cycle-contaminated 50-trade sample makes the gate certify going live for the wrong reason, and that argument is the strongest single paragraph anyone wrote here. It is answered by requiring an always-on host, not by packaging.

**#2 — Self-hosted per-user docker compose, two-host.** The right instinct, adjudicated above: nothing risk-bearing moves, the DB stays on the disk of the process managing the stops, NFR-1 holds for free, and the owner operates nothing. Its `.dockerignore` finding is the sharpest security observation in the document — `trading-bot/data/` is gitignored but the Docker build context does not read `.gitignore`, and `venues.py` writes exchange keys as plaintext into the settings table, so one missing line publishes them in a GHCR layer. It is rejected because for a Windows-bound MT5 owner it adds a hypervisor filesystem under a live SQLite WAL or a second host and a mesh-VPN hop on the engine↔EA link, and it spends 35–45 hours rebuilding `scripts/windows/`. Its economic argument — "GHCR is free for public images, that flatness is the entire economic argument" — is also void: `LICENSE.md` forbids publication, and a public image is the clearest available evidence of "holding itself out generally to the public", which is the condition doing all the work in the 15-person exemption. Its author's own closing line ("compose for the owner alone, today, to start the clock — and no friends until the gate has opened once") is adopted as policy.

**#3 — Split-brain-by-venue hosted SaaS (Vercel + Supabase + one Fly.io Machine per user).** Its central technical finding is correct, permanent, and adopted: `engine.py`'s module globals (`feed_state`, `_recent_keys`, `_last_info`, `_tv_ctx`, `_open_spread`) are keyed by *symbol* and not by user, `storage._conn`/`_lock` are module singletons with no tenant parameter across ~131 call sites, `venues._cache` is keyed by venue *name*, and `params()` reads one global `all_settings()` — so one-process-per-user is forced by the code and the multi-tenant refactor can never be proven safe. That reasoning is why §7 of this document exists. But the topology named after it is unbuildable: the "100% hosted crypto" half of the split has no execution path, because `engine.py` contains zero venue references and `feed_state["prices"]` is written only by the MT5 EA POST. Beyond that it is rejected on custody — Supabase Vault would hold friends' exchange keys and the design concedes Supabase can decrypt them — on the Fly reschedule cold-start landing squarely on the `engine.py:894` global bypass several times a month unattended, and on a 2–3 week `storage.py` Postgres port whose most safety-critical line is the invariant-7 suspect-before-degrade path, where a pooler blip makes `open_trades()` return empty, `manage_open_trades` iterate nothing, and `loss_limits_hit` report a clean day.

**#4 — AWS Native single VPC (ECS Fargate + RDS + EC2 Windows).** Rejected, and its own `fatal_flaw` section is the reason, stated better than I could: it converts the engine's relationship with its state from two-state (readable or corrupt) to three-state by inserting an ENI, a security group, a NAT gateway and a 60–120 second RDS failover between `manage_open_trades` and the `open_trades()` call that is its first line — while `engine.py:880` keeps writing a green heartbeat from a separate path on a different pooled connection, so the outage is invisible on the only telemetry that exists. That is the brief's third hard constraint violated by construction. It compounds this with co-resident MT5 terminals on shared EC2 Windows hosts, which requires each friend's broker **master password** — full account authority, typically including withdrawal — categorically worse custody than a scoped API key, and proposal #3 was right to reject exactly this. $168/mo at one user with an empty database, and its file-level plan is anchored to the wrong working tree.

**#5 as originally specified — fleet + console.** Not rejected; adopted with two amendments. First, the node is not "a Windows VPS if they need MT5, a $6 Linux box if crypto-only" — the crypto-only tier does not exist yet, because CCXT is unwired, so every node today is one Windows box running everything on loopback (this is proposal #1's geometry, imported). Second, its own fatal flaw is accepted rather than argued with: the friends it is designed for largely cannot legally run the MT5 leg — US retail forex requires an RFED/FCM and offshore MT5 brokers are prohibited from accepting US retail clients; India's FEMA closes offshore margin FX to residents entirely. The consequence is not that the topology fails, it is that **the friend cohort is deferred until crypto spot execution exists and the gate has opened once**, which is where every other analysis independently landed anyway.

---

## 4. Target architecture

### 4.1 The node — one per person, owned by that person

One always-on Windows host. Everything on it, everything on loopback.

| Component | Where | Bind | Notes |
|---|---|---|---|
| MT5 terminal + `SLCDataBridge.mq5` v2.30 | the node | — | `ServerHost = 127.0.0.1`, `ServerPort = 8766`. WebRequest allow-list is one loopback URL. `AllowTradeExecution=false` until the double gate is deliberately opened. |
| `server.py` (Flask) | the node, task `Keel-server` | **`127.0.0.1:8766`** | EA protocol + legacy dashboard. `engine.engine_loop` and `agent.agent_loop` are daemon threads in this process — `feed_state` is written by Flask handlers and read by the loop, so they are physically inseparable. |
| `dashboard_api.py` (FastAPI) | the node, task `Keel-dashboard` | `127.0.0.1:8767` | Control plane. `live_switch` mounts here. Token-gated. Unchanged. |
| `news_agent.py` | the node, task `Keel-newsagent` | no listener | Unchanged. |
| `keel-health-probe.ps1` | the node, task `Keel-healthprobe` | — | Every 2 minutes: probe 8766 `/api/pairs` and 8767 `/api/health`, restart whichever port stopped answering, budget-capped, records to `state/keel_watchdog.jsonl`. |
| SQLite WAL `trading-bot/data/trading.db` | the node's local disk | — | Same disk as the loop that manages the stops. Permanent. Not under any sync root — the installer refuses. |
| Venue credentials | the node, OS-protected (Increment 2) | — | DPAPI on Windows. Never in a projection, log, backup or snapshot. |
| `publisher.py` (Increment 3) | the node, thread inside `Keel-server` | outbound HTTPS only | Bounded queue, drop-oldest, dropped-frame counter published inside the payload. |

All four tasks are boot-start under `NT AUTHORITY\SYSTEM` with restart-on-failure. A Windows Update reboot at 03:00 costs under a minute and self-heals with nobody logged in. That single property is what defeats proposal #1's fatal flaw, and it already exists.

Remote access to the node for the owner of that node: Tailscale, `tailscale serve --bg 8767`, which proxies tailnet→loopback and preserves the binding argument `dashboard_api.py` and `live_switch.py` rest on. Ten minutes, free personal plan, no code.

**Hardware, concretely.** An N100/N150 mini-PC (Beelink, Minisforum, GMKtec), 16 GB, 500 GB NVMe: **$250–350 one-time**. Idles 6–10 W, so 24/7 is ~88 kWh/year ≈ **$8/yr in India, $15/yr in the US**. Add a small UPS, $60. Total first-year cost of a node the owner physically controls and can power-cycle: **~$320–430, then ~$1/month.** Every rental alternative loses on 24-month TCO and none of them let you unplug the thing: Contabo Windows VPS $12–15/mo, Vultr 2 vCPU/4 GB Windows ~$28/mo, forex-specialist MT5 VPS $20–35/mo, or **$0** from Vantage / IC Markets / FXTM at roughly $5k equity or ~15 lots/month, which is the lever most MT5 users should pull. A laptop is not a node.

### 4.2 The console — one, owned by the owner

| Component | Where | Cost |
|---|---|---|
| Supabase Postgres + Auth + RLS | `supabase.com`, one project | **$0** (Free) at one user; **$25/mo** (Pro) the moment a second person exists, for daily backups and no auto-pause |
| Next.js console (Increment 5) | Vercel Hobby | **$0** — and it must stay non-commercial, see §6 |
| `healthchecks.io` inverted watchdog | free tier, 20 checks | **$0** |
| Update hosting | private GitHub Releases | **$0** |

**Total recurring, one user: $0/month.** Total recurring at 5: **$25/month**, paid by the owner, with each person paying for their own node. Compare #3 at $53 and #4 at $168 for a system with an empty database. There is no 100-user column in this document; see §2 and §9.

Egress sanity at the cap: snapshot ≈ 5 KB gzipped, 10 s cadence while any position is open, 60 s while flat. Worst case ≈ 1.3 GB/node/month; 15 nodes ≈ 20 GB against Pro's 250 GB. The flat-cadence rule is what makes this work, and with zero closed trades the fleet is mostly flat.

### 4.3 Data flow

**Outward (the projection).** `engine_loop` → `publisher.offer(projection.snapshot())` → bounded queue → worker thread → HTTPS POST → Supabase. Idempotent, UPSERT on `node_id`, cursor-carrying. A publishing gap repairs itself on the next successful frame; the cursor lets the console say "42 events not seen individually" rather than silently missing them. Nothing about publication can block a trade, ever.

**Inward (the brake).** The node polls; the console never pushes. Piggybacked on the publisher's response: pending intents for this `node_id`, Ed25519-verified against a `console_pubkey` pinned in the node's own DB at pairing time, monotonic nonce, 60-second expiry mirroring `live_switch.CONFIRM_TTL_S`. Applied through `params_store.set_param("halt_new_entries", True, origin="remote")`. **The only permitted key is `halt_new_entries`. The only permitted value is `True`.** Remote resume does not exist. Remote `trading_mode` does not exist — and that is mechanical, not doctrinal: flipping mode to paper with live positions open silences the live kill switch, because `loss_limits_hit` and the open-PnL path both key on mode.

**Liveness.** Two independent channels. The publisher tells you the node is talking. `healthchecks.io`, pinged at the **end of a successful cycle**, tells you the node completed work. Agreement means alive; disagreement is itself the alarm. This is the only construction that survives the monitored system's death, and it is the answer to the double-death blind spot where the uplink and the engine die together and the console shows a frozen but plausible picture.

---

## 5. What changes in this codebase, file by file, in dependency order

Nothing in Increments 0–2 touches a strategy, a rail or the gate.

### Increment 0 — start the clock (1 file, 1 line)

1. **`trading-bot/config.yaml:9`** — `server.host: 0.0.0.0` → `127.0.0.1`. Update the comment: MT5 is on this box now. This one line deletes the unauthenticated remote-close primitive at `server.py:144` as a side effect rather than as a project, and it is only coherent because the engine and the terminal are co-resident.

### Increment 1 — the pager and the four mundane killers

2. **`trading-bot/storage.py:370 next_command()`** — make it atomic. Hold `_lock` across the expire-UPDATE, the candidate SELECT and the claim-UPDATE on one connection inside `BEGIN IMMEDIATE`. `ack_command` already does `with _lock:`; this is the same pattern. Fixes a live duplicate-order race that exists today on one host.
3. **`trading-bot/server.py:144`** — require the dashboard token header on `POST /api/commands`. `news_agent.py` is the only legitimate caller; give it the token from `dash_auth`. Defence in depth behind the loopback bind, and mandatory before the node is ever reachable from a tailnet.
4. **`SLCDataBridge.mq5`** — add `InpAuthToken` and concatenate an auth header at the four `WebRequest` sites (`~:390`, `:483`, `:566`, `:630`, `:874`). ~6 lines of MQL5 and one recompile **now**, versus N MetaEditor sessions and N WebRequest allow-list edits **later**. This is the single most expensive-to-change interface in the system and there is exactly one deployed instance of it today. Do it before there are two.
5. **`trading-bot/storage.py:179-221 _attempt_recover()`** — replace the `shutil.which("sqlite3")` subprocess path with a pure-Python `iterdump` fallback so invariant 7's recovery arm exists on Windows. Keep `_is_corrupt` / `_mark_suspect` / `integrity_suspect()` exactly as they are; they are read by `try_execute` and `live_switch._blockers()` and re-defining them is a safety change, not a refactor.
6. **Wire `alerts.py`.** No change to the module — it is finished and tested. Add importers and call sites at the kinds its own `KINDS` registry already names: `engine.py` (`feed_loss_live`, `db_integrity_live`, `kill_switch_fired`, `bad_ticket`, `clock_skew`), `storage.py` (`db_integrity_live` from `_mark_suspect`), `server.py` (`command_expired` from `next_command`, and designate this process the single lease-holding relay owner), `live_switch.py` (`trading_mode`). Days, not weeks.
7. **`trading-bot/engine.py`** — `healthchecks.io` ping at the **end** of a successful cycle, after `manage_open_trades` returns. Not at the top, not on the stand-aside path, not conditional on the feed. ~10 lines, new setting `watchdog_ping_url`. `keel-health-probe.ps1` is local and dies with the box; this is the one that does not.
8. **`trading-bot/engine.py:894-902`** — split the feed guard. `feed_age > 60` suspends **entries**; `manage_open_trades(p, derisk_only=True)` still runs. In de-risk-only mode, breakeven moves and stop-tightening are permitted; trail recomputation, new TP levels and any action that widens risk are not. Raise `feed_loss_live` (P1) when this fires with open live positions.
9. **`trading-bot/notifier.py:85`** — replace `except queue.Full: pass` with a `dropped_frames` counter that is itself observable. `notifier.start()` is per-process and `dashboard_api.py` never calls it, so today a live-switch announcement queued from the dashboard process is enqueued into a process with no consumer; `alerts.py`'s lease-based relay is the fix and item 6 delivers it.

### Increment 2 — credential custody

10. **NEW `trading-bot/secrets_store.py`** — DPAPI on Windows (ctypes to `crypt32.dll`, no new dependency), Keychain on macOS, 0600 file fallback on Linux.
11. **`trading-bot/venues.py:37-41`** — split `_all()` / `_save()`. Non-secret venue config stays in `storage.set_setting("venues", rows)`; the three `_SECRET_FIELDS` move to `secrets_store`, keyed by venue name. `get()`, `redact()` (`:52`), `upsert()`'s masked-secret-keeps-stored semantics (`:75-93`), `adapter()` and `health()` all funnel through those two functions and need no change. ~40 lines. This takes bearer credentials to real money out of a plaintext JSON blob in a SQLite settings row and puts them behind the OS login.
12. **`trading-bot/dashboard_api.py:172-179`** — `/api/settings` becomes an **allow-list**, not a four-key deny-list over `all_settings()`. Today `venues` is a settings key holding cleartext `api_key`/`api_secret`/`password`, and the endpoint returns it. This is a live hazard behind the loopback bind and an absolute blocker for Increment 3.

### Increment 3 — capture (the part that cannot be retrofitted)

13. **NEW `trading-bot/projection.py`** (~200 lines, pure read). Opens its **own** `file:…trading.db?mode=ro` connection with `PRAGMA query_only` — deliberately not through `storage.query`, because `storage.py:14` is one module-level `RLock` taken by every read and a 15–30 KB network-facing snapshot every 10 s would serialise against `try_execute`'s rail reads. WAL makes this safe; it is the one sanctioned exception to "all access via query/execute". Payload is an **allow-list built field by field**, never a redaction over `all_settings()`. Carries `node_id`, `git_sha`, `schema_version`, `cost_model_version`, `snapshot_t`, `dropped_frames`, the cursor `{max_trade_id, max_decision_id, engine_heartbeat_t}`, `analysis.health()`, `analysis.promotion_status()`, `decisions.funnel()`, open/closed trades, and `venues.health()` + `venues.redact()` reduced to name, kind, `read_only`, reachable and the 6-char `venues.fingerprint()`.
14. **NEW `trading-bot/publisher.py`** — modelled on `notifier.py`: `queue.Queue(maxsize=200)`, one worker thread, outbound HTTPS only, drop-oldest, dropped-frame counter published in the payload. `offer()` is never blocking.
15. **`trading-bot/engine.py`** — exactly two lines. One `publisher.offer(projection.snapshot())` beside the heartbeat write at `:880`, at the top of the loop so it ticks while standing aside. One event-driven offer in `manage_open_trades` on state transition.
16. **NEW `supabase/migrations/0001_init.sql`** — `nodes`, `snapshots` (UPSERT on `node_id`, last-writer-wins, **not** append-only), `snapshot_history` (capped 2000 rows/node), `closed_trades` (the pooling table, carrying `cost_model_version`, `git_sha`, `broker_fingerprint`, `symbol_as_quoted`), `intents`, `intent_receipts`. RLS `owner_uid = auth.uid()` on every table; `shared_with uuid[]` default empty.
17. **NEW `trading-bot/tests/test_projection.py`** — the hostile test. For every key in `storage.all_settings()`, assert no substring of any stored secret appears anywhere in the serialised snapshot. This is enforced by a test, not by review.

### Increment 4 — the brake

18. **`trading-bot/params_store.py:53`** — add `"remote": {"halt_new_entries"}` to `WHITELISTS`. Do **not** add `"remote"` to `_AUTOMATED` (`:69`) — the 7-day human pin would block a remote halt for a week after a local resume, which is backwards for a de-risking action. `set_param` already refuses unknown origins and logs the refusal, so this one dict entry is the entire security surface.
19. **NEW `trading-bot/intents.py`** — Ed25519 verify against the pinned `console_pubkey`, monotonic nonce high-water mark stored node-side, 60 s expiry, value must be literally `True`, apply via `params_store`.
20. **NEW `trading-bot/tests/test_intents.py`** — replay a week-old intent, forge a signature, suppress delivery, and attempt a remote write of `trading_mode` and of `halt_new_entries = False`. All four must fail.

### Increment 5 — rendering (after the first closed trade)

21. **NEW `apps/web/`** — Next.js on Vercel, Supabase anon key + RLS, ARCHITECTURE-V2 §8 screens 1–5. The gate badge is per-node, computed only from that node's own closed trades, labelled with `git_sha` and `cost_model_version`, and never aggregated across nodes.
22. **`trading-bot/server.py:223-544`** — the legacy Flask HTML UI is retired once screens 1–5 are live, per ARCHITECTURE-V2 §8 PL-3. The five EA routes (`:84-117`) stay.

### Increment 6 — crypto execution (gated, and NOT part of this decision)

23. **`trading-bot/brokers/__init__.py`** — add `supports_attached_stop: bool` and `spot_only: bool` to the `BrokerAdapter` protocol. `ccxt_venue` returns `False`/`True`; the MT5 path returns `True`/`False`, because `SLCDataBridge.mq5:749` attaches the SL at submission.
24. **`trading-bot/engine.py try_execute`** — one more rail using the existing `skip()` machinery: `skip("venue has no broker-resident stop and host continuity is not guaranteed", track=True, stage="host")`. `track=True` records the refusal as a shadow trade so the ledger measures what the rail costs.
25. **`trading-bot/requirements.txt`** — add pinned `ccxt` and a lockfile. Unpinned `>=` means two nodes installed a week apart run different CCXT against the same exchange.
26. Only then wire `venues.adapter()` into `engine.py`.

### Documentation

27. **`CLAUDE.md`** — four new invariants, §6 below.
28. **`docs/LIVE-EXECUTION-VPS.md`** — delete. Replaced by `docs/DEPLOYMENT-WINDOWS.md`.
29. **`docs/DEPLOYMENT-WINDOWS.md`** — promoted to the canonical node runbook; add the MT5-on-the-same-box section and the power-plan checklist.
30. **`LICENSE.md`** — resolve before anything is handed to anyone. §6.

---

## 6. Credential custody and the liability position

**Stated plainly: the owner never holds anyone else's key, never runs anyone else's engine, and never emits an order on anyone else's account.**

**Custody.** Venue API keys never leave the node. Not in Supabase, not in Supabase Vault, not in AWS Secrets Manager, not in a Docker image layer, not in a projection payload, not in a log, not in a backup. After Increment 2 they sit behind DPAPI on the machine's own Windows account rather than in a plaintext settings row, which is a strict improvement on today. Every key is created with **withdrawals disabled** and **IP-allowlisted to the node's own fixed address** — which is possible precisely because the node has a fixed address, and is the thing Fly.io's shared egress pool structurally cannot do without buying a dedicated IPv4 per app. `venues.upsert` defaults `read_only=True` (`venues.py:93`), so pasting a key grants visibility and never execution; arming a venue is a separate deliberate act on that node's own loopback dashboard.

**Money transmission: not carried, conditionally.** FinCEN's test is acceptance and transmission of value. Funds never leave the friend's own account at their own exchange or broker; the system holds an order-placement credential, not custody, and 31 CFR 1010.100(ff)(5)(ii)(A) separately exempts network-access services. No MSB registration, no state money transmitter licensing. **This is conditional on code**: enable withdrawal permission on any key, or pool funds in any account the owner controls for any reason, and the analysis inverts into 50-state licensing. `read_only=True` and withdrawals-disabled are not conveniences.

**Commodity trading advisor: the real exposure, and the wrong statute is usually cited.** It is not the Investment Advisers Act — spot FX is not a security and the Act needs compensation. It is 17 CFR 5.1(e)(1), which defines a retail-forex CTA as anyone exercising discretionary trading authority over a non-ECP's account, with **no compensation element in the text**. Discretion is the trigger. This design keeps discretion on the friend's own machine, under their own start/stop, with their own key, on their own host, and that is the entire point. 17 CFR 4.14(a)(10) then exempts a person who has advised no more than 15 persons in the preceding 12 months **and** does not hold themselves out generally to the public. Both are design parameters here: a counter in the `nodes` table, and nothing public — private repo, private releases, no listing, no marketing page, no performance figures visible to anyone who signs up.

**No compensation. Ever.** Not a fee, not a revenue share, not "cover my VPS costs". It activates the Advisers Act prong, strengthens "engaged in the business" for CTA purposes, triggers India's PMLA "in the course of business" test for VDA activity, and breaches Vercel Hobby's terms. It buys nothing.

**India.** FEMA closes offshore margin FX to residents; the RBI publishes an Alert List and penalties run to three times the amount involved. SEBI's February 2025 retail algo framework routes exchange-traded retail algos through registered brokers with exchange-assigned Algo-IDs from April 2026 and requires a whitelisted static IP — which a node on the user's own box satisfies trivially and a shared-egress hosted machine fails. Consequence: **an India-resident friend runs crypto spot only, on their own node, with their own static IP.** The MT5 leg may serve the owner only. Separately, and this must be told to any Indian user before they start: VDA gains are taxed flat at 30% + 4% cess with 1% TDS under s.194S, and **VDA losses cannot be set off against anything or carried forward** — a high-turnover bot with genuine positive pre-tax expectancy can be net-negative after tax purely from turnover.

**Crypto is spot only.** No margin, no perpetuals, no futures. Expressed as `spot_only` on the `BrokerAdapter` protocol, so it is a code property and not a policy note. This keeps CEA 2(c)(2)(D) retail-leveraged treatment out of scope. Accept the tension honestly: spot means the stop is client-side, which is the riskier operational configuration, and that is exactly why the `supports_attached_stop` rail in Increment 6 exists.

**Auto-update is a legal act.** Code the owner wrote, on a schedule he chose, changing how a friend's real account is traded, is much closer to operating a service than to distributing software. Updates are therefore **opt-in with an acknowledged changelog, never silent**, the acknowledgement is logged locally, the update never lands with positions open (flat-book gate), and `trading.db` is copied to `trading.db.pre-<version>` first because `storage._MIGRATIONS` is additive-only with no down path and one bad release destroys a gate sample that took months to accumulate.

**The disclaimer is well-drafted and pointed at the wrong people.** `LICENSE.md`'s "you are solely responsible for any trades placed with this software" is substantively right, but it lives in a private repo addressed to "the team", and a friend who double-clicks an installer has assented to nothing. It becomes a **click-through at first run**, accepted and timestamped locally, naming the friend as the account owner and sole operator, confirming they configured their own credentials with withdrawals disabled, and acknowledging their own regulatory and tax position. Privity is what is missing, not wording. Note also that "as is" disclaimers are weakest against known defects, and this repo's defect register, commit history and this document are all discoverable — which is another reason Increment 1 is not optional.

**Two blockers to resolve before anything reaches a second person, both an afternoon's work:**
1. **`LICENSE.md` ownership.** Copyright reads "© 2026 Proaxive / Shakeeb Ahmed" and the terms forbid distribution outside "the team". Every topology considered here distributes. If the owner is Shakeeb Ahmed / Proaxive this is a paperwork fix; if not, distributing to friends is a copyright question no architecture answers.
2. **The INR-base-currency account.** `DEVELOPMENT-HISTORY.md:39` describes an MT5 account with INR base currency trading ~42 FX instruments. Identify the broker and its jurisdiction, because it determines whether the 50-trade sample now being accumulated is against a venue that can ever legally be traded live.

**Not legal advice.** Before the second person's real money touches this system: one hour with US commodities/derivatives counsel on the CTA question, one hour with Indian counsel on FEMA and SEBI. Cheap against a penalty computed as three times the amount involved, and cheaper than a friendship.

---

## 7. Multi-tenancy

**The engine has no tenants. The console does. That asymmetry is deliberate and permanent.**

**Why the engine can never be multi-tenant.** This is settled by the code, not by taste, and proposal #3 verified it correctly. `engine.py`'s module globals — `feed_state`, `_recent_keys`, `_last_info`, `_tv_ctx`, `_open_spread` — are keyed by **symbol**, not by user. Two users on EURUSD in one process would share `feed_state["prices"]["EURUSD"]`: one person's broker's bid would size the other's lots via `calc_lots` and decide whether their stop was hit. `storage._conn` and `storage._lock` are module singletons and ~131 call sites pass no tenant. `venues._cache` is keyed by venue **name**, so two users each naming a venue "binance" collide on credentials. `params()` reads one global `all_settings()`; there is no per-user parameter namespace anywhere. Retrofitting tenancy means a contextvar threaded through the risk path, and a contextvar leak routes one person's order to another person's account silently. That refactor is a month of work on exactly the files `CLAUDE.md` says not to touch casually, and it can never be proven absent. **One process, one person, one SQLite file, one set of keys. Forever.**

**Why the console may have tenants.** The console is a read model. The worst outcome of an RLS bug there is that someone sees a P&L number they should not — not that someone's order goes to the wrong account. That asymmetry is the entire justification, and it is why tenancy is confined to a database that cannot place a trade.

**Console tenancy design.**
- Supabase Auth for identity. RLS `owner_uid = auth.uid()` on every table, with a `CHECK` on writes so a bug becomes a database error rather than a silent cross-tenant write.
- **The owner does not get automatic read of a friend's book.** `shared_with uuid[]`, default empty, opt-in per node. Default-visible friends' P&L is a liability surface, not a feature.
- **Node identity is a keypair, not a password.** At pairing the node generates an Ed25519 keypair, stores the private half in its own DB, registers the public half. The node authenticates to the console; the console never authenticates to the node and has no inbound path at all.
- **The real isolation boundary is one machine per person.** RLS is defence in depth. This must be said plainly because the opposite claim is how hosted designs fool themselves: RLS isolates whatever tenant id it is given, so if the id is wrong it isolates the wrong person perfectly.
- **Hard cap: 15 persons, rolling 12 months**, enforced as a counter over `nodes.first_paired_t`. Above the line the correct architecture is not a bigger database, it is CFTC registration and NFA membership.
- **No cross-node aggregation in v1.** `closed_trades` carries `cost_model_version`, `git_sha`, `broker_fingerprint` and `symbol_as_quoted` so that pooling becomes possible once a common cost model exists. Until then the console badges mismatched nodes and refuses to put two nodes' expectancy on the same axis.

---

## 8. Migration path — shippable increments, working system at the end of each

**Increment 0 — the clock starts. Zero new code. This week.**
Provision an always-on Windows box (mini-PC preferred). Clone to `C:\Keel\slc-trading-bot` — not under any sync root; the installer refuses anyway. Set the power plan to never sleep and never hibernate. `keel-services.ps1 selftest -Python <venv python>`, then `install` elevated. Install MT5 and the EA on the same box, `ServerHost=127.0.0.1`, `AllowTradeExecution=false`. Flip `config.yaml:9` to `127.0.0.1`. Tailscale for phone access to 8767.
*End state:* paper engine running 24/5, survives reboots unattended, reachable from a phone, unauthenticated remote-close primitive gone. **The 50-trade clock is running and every subsequent increment happens while it runs.**

**Increment 1 — someone gets woken up. ~1 week of evenings.**
Items 2–9. Atomic `next_command`, auth on `POST /api/commands`, EA auth token, Windows-native DB recovery, `alerts.py` wired to its own registry with `server.py` as relay owner, `healthchecks.io` deadman at end-of-cycle, feed guard split into entries-blocked/de-risk-only, notifier drop counter.
*End state:* same trading behaviour, plus the failures that matter now page a human. **Do not proceed until this pager has fired at least once in anger and been acted on.**

**Increment 2 — the keys move behind the OS. ~1 weekend.**
Items 10–12. `secrets_store.py`, the `venues.py` two-function split, `/api/settings` becomes an allow-list.
*End state:* no cleartext credential anywhere in the database, and the exfiltration path that would otherwise be opened by Increment 3 is closed before it exists.

**Increment 3 — capture. ~3 evenings plus a Supabase afternoon.**
Items 13–17. `projection.py`, `publisher.py`, two lines in `engine.py`, the Supabase schema with RLS, the hostile secrets test.
*End state:* every closed trade is recorded off-box with its cost model, git SHA and broker fingerprint, from the first one. There is no UI yet and that is fine — a `select *` in the Supabase console is enough. **This is the increment that cannot be done later.**

**Increment 4 — the brake. ~2 evenings.**
Items 18–20. One `WHITELISTS` entry, `intents.py`, the hostile intents test.
*End state:* the owner can stop his own node from a phone in India. Nothing else can be changed remotely, by anyone, including him.

**Gate — the first cell opens.** 50 closed paper trades in one strategy × asset-class cell with the promotion gate green and a human sign-off. Nothing below this line happens before it.

**Increment 5 — rendering. 40–60 hours.**
Items 21–22. Next.js console, screens 1–5, per-node gate badges, legacy Flask UI retired.

**Increment 6 — crypto execution.** Items 23–26, in that order. The capability flags and the host-continuity rail land **before** `venues.adapter()` is wired into the engine, not after.

**Increment 7 — the first friend.** Only after Increments 0–6, the gate opening, `LICENSE.md` resolved, the click-through built, and counsel consulted. Realistically: a 30–60 minute screen share to install MT5 or configure a spot exchange key, compile the EA, and allow-list the WebRequest URL. No packaging removes that step.

---

## 9. What this decision forecloses

1. **Hosted execution, permanently.** There will never be a Keel server that holds a credential and emits an order. Reversing this requires the `storage.py` Postgres port, the engine multi-tenancy refactor described in §7, a KMS and envelope encryption, and a completely different regulatory posture. This is a one-way door and it is being walked through deliberately.
2. **Postgres.** SQLite on local disk is now an architectural commitment, not a stage. `storage.py` keeps its single connection and its module `RLock`. Anything that needs a second writer to the same database is wrong by construction. The only exception is `projection.py`'s read-only connection, and it is named as an exception.
3. **Vercel and Supabase in the trading path.** They are a mirror. If both are down, every node trades normally. Any future feature that requires the console to be up in order to trade is rejected without discussion.
4. **Remote resume, remote sizing, remote `trading_mode`, remote anything except `halt_new_entries = True`.** Arming live capital requires physical or tailnet access to that node's loopback dashboard and both halves of the existing double gate.
5. **Cross-node sample pooling, for now — and possibly forever.** The schema keeps it possible; the console will not do it until a common cost model exists. Judge 3 is right that fleet sample rate is the only thing a fleet is genuinely worth at scale, and deferring rather than deleting it is the most this decision can honestly promise.
6. **Scale past 15 people without registration.** A counter, not an aspiration.
7. **A non-technical friend clicking one installer and being done.** The node needs a Windows host that never sleeps, an MT5 terminal or an exchange key, a compiled EA and an allow-listed URL. Anyone unwilling to do a 45-minute setup call is not a user of this system, and no amount of Tauri, Docker or provisioning API removes that.
8. **Laptops.** The node must be a machine that does not sleep. `keel-services.ps1` should gain a refusal on battery-powered hosts without an explicit override flag, because the duty-cycle contamination argument in proposal #1 is correct: a gate sample accumulated across sleep cycles certifies going live for the wrong reason, and `analysis.py` has no field for host uptime with which to detect it.
9. **Public distribution of this code in any form** — public GHCR image, public GitHub Releases, store listing, marketing page — while `LICENSE.md` says what it says and while the 15-person exemption depends on not holding out to the public.

---

## 10. Disposition of ARCHITECTURE-V2

**Amended, not retired.** V2 is 1,296 lines and the large majority of it is topology-independent and remains in force. Specifically:

| V2 section | Disposition |
|---|---|
| §0 governing sentence, §2 adjudication, §3 verified defect register | **Survive unchanged.** |
| §4 strategy-plugin interface | **Survives unchanged.** |
| §5 BrokerAdapter interface | **Amended**, additively: `supports_attached_stop: bool` and `spot_only: bool` join the Protocol. V2 already called for the first as "a declared capability that changes engine behaviour". |
| §6 risk-budget sizing for N concurrent strategies | **Survives unchanged.** |
| §7 alert taxonomy | **Survives and is promoted.** It is fully implemented in `alerts.py` with a passing suite and called by nothing. It moves from specification to blocking work in Increment 1. |
| §8 "Hosted? No. Not now, not at strategy #5, not at venue #3" | **Amended, narrowly.** The reasoning was correct for a single user and the premise it rested on changed when multi-user became a requirement. The amendment: hosted **read model** yes; hosted **engine** no; hosted **credentials** never. V2's actual arguments — the staleness-distrust spiral, the loopback binding as `live_switch`'s security model, the `trading_mode`-flip kill-switch defect — all survive intact and are honoured by the design. §8's endorsement of a Telegram inbound channel with `/halt` as the single write, on the reasoning that every pocket-reachable control must be monotonically de-risking, is the exact principle §4.3 of this document implements with a different transport. |
| §8 screens 1–5 | **Survive**, now rendered in Next.js against Supabase rather than a local HTML page. Screens 6–7 unchanged. |
| §9 phased plan, §1 "what to do this week" | **Superseded** by §8 of this document. V2's W1–W7 remain prerequisites and are folded into Increment 1 — in particular W1 (32-bit ticket truncation) and W4 (paper TP1 banking at minimum size), because a sample accumulated before those fixes is generated by a fill model live cannot reproduce. |
| §10 kill list, §11 amendments | **Survive.** |

**Other documents.** `docs/LIVE-EXECUTION-VPS.md` is **retired and deleted** — the Mac↔Windows-VPS-over-Tailscale split is gone; the engine now runs on the Windows box MT5 already requires and `ServerHost` becomes `127.0.0.1`, which deletes the tailnet, the remote allow-listed WebRequest URL and the latency on every command poll and ack, and removes the failure mode where the engine is alive, the EA is alive, and the link between them is not. `docs/DEPLOYMENT-WINDOWS.md` is **promoted** to the canonical node runbook. `docs/PLATFORM-REQUIREMENTS-ANALYSIS.md` §3 is **vindicated** — its recommended shape (SQLite authoritative on local disk, one-directional async read model, control does not flow back that way) is precisely this decision, arrived at independently. `docs/ARCHITECTURE.md` and `docs/MULTI-ASSET-ARCHITECTURE.md` are **historical**.

**`CLAUDE.md` gains four invariants:**

> **14.** The engine's state and the loop that manages it share one failure domain. `trading.db` lives on the same disk as the process that reads it. Nothing — no network, no pooler, no volume driver, no sync client — may be introduced between `manage_open_trades` and the rows it reads.
>
> **15.** Control flows outward from a node only. The single exception is `halt_new_entries = True`, origin `remote`, Ed25519-signed, nonce'd, 60-second TTL. Any remote write of any other key, or of the value `False`, is an invariant breach and not a config change.
>
> **16.** A venue credential never leaves the machine that owns it. No projection, log, backup, image, snapshot or API response may contain a value from `venues._SECRET_FIELDS`. Enforced by `tests/test_projection.py`, not by review.
>
> **17.** No new entry on a venue that cannot hold a broker-resident stop while the host cannot guarantee continuity. Refusals are recorded as shadow trades so the ledger measures what the rail costs.

---

## 11. The one-line version

Put the engine on a Windows box that never sleeps, next to the MT5 terminal it already needs, on loopback; wire the pager that is already written; publish a redacted copy of the evidence outward to a console that can do exactly one thing back — stop.
