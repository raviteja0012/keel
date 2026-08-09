# Setup guide

Getting Keel from a clone to a connected venue. There are two independent paths and you do not
need both:

- **Path A — MetaTrader 5.** Venue #1, and the one strategy #1 (SLC) is written against. This is
  the path if you want live FX, metals or index data flowing into the engine.
- **Path B — an exchange.** CCXT or 3Commas, added through the control dashboard. Read-only
  until explicitly armed.

Part 0 applies to both. Everything below assumes the runtime checkout is on plain local disk —
**not** OneDrive, Dropbox or iCloud. `data/trading.db` is SQLite in WAL mode and a background
sync agent writing underneath the engine corrupts the exact file the kill switches read.

---

## Part 0 — Start the processes

```bash
cd trading-bot
pip install -r requirements.txt
python3 server.py
```

**Check:** the banner prints

```
============================================================
 Keel
 Dashboard : http://localhost:8766
 EA target : http://<this-machine-ip>:8766
============================================================
```

and no `Address already in use`. Open **http://localhost:8766** — the legacy dashboard loads and
the header shows *EA: offline*, which is correct at this point.

In a second terminal, start the control dashboard. It binds `127.0.0.1` only, because it can
control live trading:

```bash
cd trading-bot
python3 dashboard_api.py       # http://127.0.0.1:8767
```

Two more things you will want:

- **The machine's LAN IP.** The banner prints the literal placeholder `<this-machine-ip>`, not
  your address; Path A needs the real one twice. Get it with:
  ```bash
  ipconfig getifaddr en0        # macOS; try en1 if empty
  hostname -I                   # Linux
  ```
  Example result: `192.168.68.104`.
- **The control token.** Everything on 8767 that *changes* something requires the
  `X-Dashboard-Token` header. It comes from the `DASHBOARD_TOKEN` environment variable if set,
  otherwise it is generated on first run and stored at `trading-bot/state/dashboard_token`
  (mode 0600). Paste it into the **Controls → Control token** field; the browser keeps it in
  local storage, it is never stored in the DB or the repo.

Keep both processes running. On macOS, `./watchdog-install.sh` from the **repo root** installs
launchd jobs for `server.py` and `news_agent.py`, keeps the Mac awake, and restarts them on
crash; `./watchdog-install.sh status` reports on them and `./watchdog-install.sh remove`
uninstalls.

---

## Path A — MetaTrader 5 (venue #1)

### A1. Install and compile the EA

1. Copy **`SLCDataBridge.mq5`** to the MT5 machine.
2. MT5 → **File → Open Data Folder** → `MQL5/Experts/` → paste it there.
3. Open **MetaEditor** (**F4** in MT5) → File → Open → `Experts/SLCDataBridge.mq5` → **F7**.
4. **Check:** the Errors tab says **`0 errors`** and the result line shows `SLCDataBridge.ex5`
   produced. A few warnings are tolerable; errors are not.
5. In MT5, right-click **Navigator → Expert Advisors → Refresh**. **SLCDataBridge** appears.

> **If you are holding an older copy of the EA, recompile it — this is not optional.** The
> current source casts MT5 tickets to `long` when it serialises them
> (`IntegerToString((long)ticket)` at `SLCDataBridge.mq5:989`, and `(long)dTicket` /
> `(long)…DEAL_POSITION_ID` at `:1031`–`:1032`). The baseline in
> `SLCDataBridge.original.mq5:953/995/996` cast them to `int`. MQL5 `int` is 32-bit signed and
> live MT5 tickets routinely exceed 2³¹, so they wrap negative. Every ticket-keyed mechanism —
> position matching, `close_trade`, TP1 partials, P&L attribution — then fails silently on a
> live account while working perfectly on a demo account, where ticket numbers are small. A
> stale `.ex5` compiled from the old source looks identical in Navigator and behaves correctly
> right up until it costs you money.

### A2. Allow the connection

1. MT5 → **Tools → Options → Expert Advisors**.
2. Tick **Allow algorithmic trading** and **Allow WebRequest for listed URL**.
3. Add a line to the URL list — host and port must match exactly, `http://`, no trailing slash:
   ```
   http://192.168.68.104:8766
   ```
4. OK.

### A3. Attach the EA

1. Open a **new chart** (e.g. EURUSD M15). One EA per chart is an MT5 rule; attaching to a chart
   that already runs an EA replaces it.
2. Drag **SLCDataBridge** onto the chart. The inputs dialog opens.
3. **Common tab:** tick *Allow Algo Trading* (some builds word it "Allow live trading").
4. **Inputs tab:**

   | Input | Set to | Why |
   |---|---|---|
   | `ServerHost` | the LAN IP from Part 0 | where `server.py` runs; `127.0.0.1` only if MT5 is on the same machine |
   | `ServerPort` | `8766` (default) | must match `config.yaml` and the WebRequest URL |
   | `AllowTradeExecution` | **false** | half of the double gate. Leave false. Flipping it is a going-live step, not a setup step |
   | `MaxLotsPerTrade` | e.g. `1.0` | broker-side hard cap for later live use |
   | `MaxOpenPositions` | deliberate value | broker-side hard cap; independent of the engine's `max_concurrent` |
   | `UseDashboardPairs` | `true` | the EA follows the dashboard Pairs Manager |
   | `SymbolAliases` | adjust if needed | e.g. `USOIL=USOUSD` when broker names differ |
   | `VerboseLog` | `true` for the first day | easier debugging; turn it off later |

5. **Check the chart corner:** the EA name shows with a ⌃/smiley icon. A struck-through icon
   means algo trading is off.
6. **Check the toolbar:** the global **Algo Trading** button is green.
7. **Check the Experts tab** for these lines within ~10 s:
   ```
   SLCDataBridge v2.30: started.
     Server : http://192.168.68.104:8766
     Push   : .../api/mt5_feed  every 5s
     Bars   : .../api/mt5_bars  every 60s
   ```

### A4. Failure signatures

| In the Experts tab | Cause |
|---|---|
| `push failed. HTTP=-1 \| error=4014` | URL not in the WebRequest allow-list, or a typo — host *and* port must match |
| `push failed. HTTP=-1 \| error=5203/5200` | server unreachable: `server.py` not running, wrong IP, host firewall blocking Python, or different networks |
| `skipping unavailable symbol 'X'` | the broker does not offer it, or it needs an alias. Harmless |
| `open_trade REFUSED` | `AllowTradeExecution` is still false. That is the safety working |

### A5. Verify on the dashboard

1. Refresh http://localhost:8766. Within ~30 s the header pill turns **EA: connected** and
   Balance/Equity show the MT5 account numbers.
2. Within ~60–90 s (the first bars push) the **Chart** panel fills.
3. The **Engine analysis** panel populates with one row per symbol per speed showing bias and
   what it is waiting for. That is the proof the strategy loop is running.

---

## Path B — Connect an exchange

The venue layer is credential storage, health, balances and a position feed today. `engine.py`
does not import `venues` or `brokers`, so connecting an exchange does not yet route orders
through it — it gives you an authenticated, observable account and the reconciliation feed.
Arming a venue is still a real act with a logged audit trail, so treat it as one.

### B1. Install the adapter dependency

`ccxt` is deliberately not in `trading-bot/requirements.txt`: a missing dependency should
disable a venue kind, not the engine.

```bash
pip install ccxt
cd trading-bot
python3 -c "import brokers; print(brokers.kinds())"    # ['3commas', 'ccxt']
```

Without it you get `['3commas']` and the engine still starts.

### B2. Know before you type a key: reachability is about where the bot runs

The dashboard offers every exchange ccxt supports (103 on ccxt 4.5.71) and makes no promise
about which of them answer from your host. Verified behaviour:

| Exchange | Result |
|---|---|
| `binance` (binance.com) | **HTTP 451** from the US — refused for legal reasons, not a credential problem. Answers normally from India |
| `binanceus` | answers |
| `kucoin` | answers |
| `okx` | answers |
| `kraken` | answers |
| `coinbase` | answers |

This is a property of the host's location, not of configuration. No API key, VPN setting or
market type changes a 451. If you are moving the bot between regions, re-test before assuming a
venue still works.

### B3. Add the venue read-only

1. Open **http://127.0.0.1:8767** → **Venues**.
2. Paste the control token into **Controls → Control token** first; venue writes are token-gated.
3. **Test reachability only** with no key, to confirm the host can reach the exchange at all.
4. Fill the form:
   - **Kind** — `ccxt exchange` or `3Commas router`.
   - **Name** — how you will refer to it, e.g. `binanceus-main`. It is the key: saving the same
     name again updates that venue.
   - **Exchange** and **market type** (`spot` / `swap` / `future`) for ccxt.
   - **API key**, **API secret**, and **passphrase** where the venue needs one (okx, kucoin).
   - For 3Commas: the **account_id**, plus the **"3Commas bots also manage this account"**
     checkbox — see B5.
5. Click **Save read-only**. Saving never arms execution.
6. **Check the row:** *Reachable* yes, *Auth* yes, a plausible latency, and your balances. The
   stored secret is shown as a mask plus a six-character fingerprint, so you can tell which key
   is loaded without revealing it.

**Failure signatures:**

| Row shows | Cause |
|---|---|
| Reachable **no**, detail quotes the venue's refusal | a 451 there means geo-blocked from this host (see B2) — no key will fix it |
| Reachable yes, Auth **failed** | key/secret/passphrase wrong, or the key lacks read permission |
| Reachable yes, Auth `no key` | saved without credentials — public data only |
| `400 unknown kind 'ccxt' (have: 3commas)` on save | `ccxt` is not installed in the interpreter running `dashboard_api.py` |

Re-saving a venue with the key and secret fields left blank keeps the stored ones, so you can
change the market type without retyping a secret.

### B4. Arm it, separately and deliberately

The **Execution** column on the venue row is the switch. `read-only` → **Arm** flips it, and it
is written to the agent log either way. Until it is armed, every adapter write raises
`VenueReadOnly`. Adding credentials lets you see an account; arming lets an order leave.

Leave every venue read-only until the promotion gate for that cell is actually open. There is no
reason to arm a venue the engine is not yet routing through.

### B5. 3Commas: one owner per position

3Commas is a router, not an exchange. Two things follow:

- **It publishes no usable instrument filters.** `symbol_meta` raises rather than guessing a tick
  or lot size, because a guess produces rejected or wrongly-sized orders. Size against the
  exchange adapter and route execution through 3Commas.
- **If 3Commas' own DCA/grid bots trade the account, tick `manages_positions`.** With it set the
  adapter refuses to open a position at all. Two risk engines managing one stop is not a
  supported configuration, and this flag is how the operator's intent is recorded and made
  visible in the health detail.

---

## Configure

1. **Pairs Manager** (8766): click pairs on and off. The EA picks up changes within 30 s — watch
   for `watching N dashboard-enabled symbol(s)` in the Experts tab.
2. **Settings:** the shipped defaults match the playbook — risk 1%, min RR 2.0, ATR buffer 0.35,
   daily stop −2%, weekly −5%, max 2 concurrent, intraday + swing on. Change → **Save settings**.
   Risk-relevant keys are written through `params_store`, which enforces per-origin whitelists,
   bounds and hard code ceilings, and records an audit row. Nothing writes settings directly.
3. **Telegram:**
   1. Telegram → **@BotFather** → `/newbot` → copy the **token**.
   2. Open your new bot's chat and send it any message once.
   3. **@userinfobot** replies with your numeric **chat ID**.
   4. Dashboard → Telegram panel → paste both → **Enabled** → **Save** → **Send test message**.
4. **Discord:** Server Settings → Integrations → Webhooks → New Webhook → copy the URL → paste it
   into the dashboard. Restart `news_agent.py` after changing Discord: it builds its notifier
   once at startup, while the main process reads notification settings live.
5. Trading mode in the header is **PAPER**. Leave it.

---

## Running and monitoring

- Trades fire only when the full checklist aligns and every rail passes. **Zero trades for days
  is normal**, especially on eight pairs at `max_concurrent: 2`. The Engine Analysis panel and
  the decisions log always say what it is waiting for.
- Telegram alerts: 🟢 opened, 🔵 TP1/breakeven, ✅/🔴 closed, 🤖 agent changes. Shadow trades are
  silent by design.
- The self-tuning agent evaluates every 4 h once 15+ trades have closed, and may only move a
  whitelisted set of parameters. It can never touch `risk_pct`, stops, `max_concurrent` or
  `trading_mode`.
- After a reboot, rerun `server.py`. History is in `trading-bot/data/trading.db` and the EA
  reconnects on its own.
- If you ever doubt the numbers, run `python3 hallucination_check.py` from the repo root. It is
  read-only, verifies DB integrity, feed freshness and that the agent stayed inside its
  authority, and appends a verdict line to `hallucination_check.jsonl`. If integrity is actually
  suspect, stop the bot and use `./recover-db.sh`.

---

## Going live

Setup does not end at live, and neither does this guide — going live is gated in code, not by
documentation. The short version:

1. The promotion gate must be open for the specific strategy × asset-class cell: ≥50 closed paper
   trades, positive expectancy over those trades, a `GROUNDED` data-trust verdict less than 24 h
   old, risk rails demonstrably fired, and a manual sign-off that has settled for at least an
   hour and has not been superseded by a behavioural parameter change.
2. `POST /api/live/request` on 8767, then `POST /api/live/confirm` within 60 s with the returned
   one-time token and the phrase `GO LIVE` typed back exactly.
3. **And** `AllowTradeExecution = true` in the EA inputs. Both halves are required. Neither one
   alone places a live order.
4. Start at minimum size and treat the first live trades as a continuation of forward-testing.
5. If another system manages stops on the same MT5 account, only one runs live at a time.

`POST /api/live/paper` de-escalates with no ceremony and is never gated. Read
[`TEAM-ONBOARDING.md`](TEAM-ONBOARDING.md) and `CLAUDE.md` before you get near step 1.

---

## Quick troubleshooting

| Symptom | Fix |
|---|---|
| EA: offline on the dashboard | server running? LAN IP right? WebRequest URL exact? host firewall? same network? |
| Connected but no candles | wait for the 60 s bars push; check the Experts tab for `bars push OK` |
| No trades ever | check Engine Analysis and the decisions log; check pairs are enabled; mode ≠ OFF |
| `open_trade REFUSED` in the Experts tab | `AllowTradeExecution` is still false — that is the safety working |
| 401 from the control dashboard | control token missing or wrong; re-read `trading-bot/state/dashboard_token` |
| `unknown kind 'ccxt'` when saving a venue | `pip install ccxt` into the interpreter running `dashboard_api.py` |
| Venue unreachable with 451 | geo-block, not credentials. See Path B step B2 |
| Port conflict on restart | `lsof -ti :8766 \| xargs kill -9` (and `:8767` for the control dashboard) |
