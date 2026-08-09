# SLC Bot — Complete Setup Guide (SLCDataBridge, port 8766)

Your existing MT5DataBridge/8765 system is not touched by anything below.

---

## Phase 1 — Start the server (Mac)

1. Open Terminal:
   ```bash
   cd "/Users/shakeebs/Claude/Projects/Price Action Strategy/trading-bot"
   python3 server.py
   ```
2. **Check:** banner shows `Dashboard : http://localhost:8766` and no "Address already in use" error.
3. Open **http://localhost:8766** — dashboard loads, header shows *EA: offline* (correct for now).
4. Get your Mac's IP — you'll need it in Phases 2–3:
   ```bash
   ipconfig getifaddr en0
   ```
   Example result: `192.168.68.104`. (If empty, try `en1`.)
5. Keep this Terminal window open. The server must stay running.
   - Optional: prevent the Mac from sleeping while it runs:
     `caffeinate -i python3 server.py`

---

## Phase 2 — Install and compile the EA (machine running MT5)

1. Copy **`SLCDataBridge.mq5`** to the MT5 machine (AirDrop/USB/network share).
2. In MT5: **File → Open Data Folder** → open `MQL5/Experts/` → paste the file there.
3. Open **MetaEditor** (press **F4** in MT5) → File → Open → `Experts/SLCDataBridge.mq5` → press **F7**.
4. **Check:** the Errors tab at the bottom says **`0 errors, 0 warnings`** (a few warnings are tolerable; errors are not). Result line shows `SLCDataBridge.ex5` produced.
5. Back in MT5, right-click **Navigator → Expert Advisors → Refresh**. **SLCDataBridge** appears as its own EA, separate from MT5DataBridge.

---

## Phase 3 — Allow the connection

1. MT5 → **Tools → Options → Expert Advisors** tab.
2. **Check these boxes:**
   - ☑ Allow algorithmic trading
   - ☑ Allow WebRequest for listed URL
3. In the URL list, **add a new line** (keep the existing `:8765` entry for your old system):
   ```
   http://192.168.68.104:8766
   ```
   (your Mac IP from Phase 1, step 4 — must be `http://`, no trailing slash)
4. Click OK.

---

## Phase 4 — Attach the EA (the checkpoints that matter)

1. Open a **new chart** (e.g. EURUSD, M15) — a *different* chart from the one running your old MT5DataBridge. One EA per chart is an MT5 rule; attaching to the same chart would replace the old EA.
2. Drag **SLCDataBridge** from Navigator onto that chart. The inputs dialog opens.

3. **Common tab — check:**
   - ☑ *Allow Algo Trading* (wording varies by build: "Allow live trading")

4. **Inputs tab — check each:**

   | Input | Set to | Why |
   |---|---|---|
   | `ServerHost` | your Mac IP (e.g. `192.168.68.104`) | where server.py runs; `127.0.0.1` only if MT5 runs on the same Mac |
   | `ServerPort` | `8766` (already default) | must match config.yaml and the WebRequest URL |
   | `AllowTradeExecution` | **false** | leave false for paper trading — flip only when going live |
   | `MaxLotsPerTrade` | e.g. `1.0` | broker-side hard cap for later live use |
   | `UseDashboardPairs` | `true` | EA follows your dashboard Pairs Manager |
   | `SymbolAliases` | adjust if needed | e.g. `USOIL=USOUSD` if broker names differ |
   | `VerboseLog` | `true` (first day) | easier debugging; set false later |

5. Click OK.

6. **Check the chart corner:** the EA name shows with a ⌃/smiley icon. A struck-through/sad icon = algo trading is off (see step 3 or the global button next).
7. **Check the toolbar:** the global **Algo Trading** button is green/active.
8. **Check the Experts tab** (Toolbox/Terminal panel at the bottom) for these lines within ~10 s:
   ```
   SLCDataBridge v2.30: started.
     Server : http://192.168.68.104:8766
     Push   : .../api/mt5_feed  every 5s
     Bars   : .../api/mt5_bars  every 60s
   ```
9. **Failure signatures and fixes:**
   - `push failed. HTTP=-1 | error=4014` → URL not in the WebRequest allow-list, or typo in it (must match host *and* port exactly).
   - `push failed. HTTP=-1 | error=5203/5200` → server unreachable: server.py not running, wrong IP, firewall on the Mac (System Settings → Network → Firewall → allow Python), or different Wi-Fi networks.
   - `skipping unavailable symbol 'X'` → broker doesn't offer it or needs an alias; harmless.

---

## Phase 5 — Verify on the dashboard

1. Refresh http://localhost:8766. Within ~30 s the header pill turns **EA: connected** (green) and Balance/Equity show your MT5 account numbers.
2. Within ~60–90 s (first bars push) the **Chart** panel fills with candles. Pick symbols/timeframes from the dropdowns.
3. **Engine analysis** panel populates with one row per symbol per speed (intraday/swing) showing bias and what it's waiting for. This proves the strategy loop is running.

---

## Phase 6 — Configure

1. **Pairs Manager:** click pairs on/off. The EA picks up changes within 30 s (watch the Experts tab: `watching N dashboard-enabled symbol(s)`).
2. **Settings panel:** defaults match the playbook — Risk 1%, Min RR 2.0, ATR buffer 0.35, daily stop −2%, weekly −5%, intraday + swing on. Change → **Save settings**.
3. **Telegram:**
   1. Telegram → search **@BotFather** → `/newbot` → name it → copy the **token**.
   2. Open your new bot's chat and send it any message (required once, so it may reply).
   3. Search **@userinfobot** → it replies with your numeric **chat ID**.
   4. Dashboard → Telegram panel → paste both → toggle **Enabled** → **Save** → **Send test message** → check Telegram for "✅ Keel — Telegram connected."
4. Trading mode in the header is **PAPER** already — leave it.

---

## Phase 7 — Running and monitoring

- Trades fire only when the full 6-point checklist aligns — **zero trades for days is normal**, especially on few pairs. The Engine Analysis panel always tells you why it's waiting.
- You'll get Telegram alerts: 🟢 trade opened, 🔵 TP1/breakeven, ✅/🔴 closed, 🤖 agent changes.
- The agent self-evaluates every 4 h once 15+ trades have closed; its actions appear in the **Agent Log** panel.
- If the Mac reboots: just rerun `python3 server.py`. All history is in `trading-bot/data/trading.db`; the EA reconnects automatically.

---

## Phase 8 — Going live (only after 50+ paper trades with positive expectancy)

1. MT5: right-click the SLCDataBridge chart → Expert Advisors → Properties → set `AllowTradeExecution` = **true**. Set `MaxLotsPerTrade` deliberately.
2. Dashboard header → mode dropdown → **LIVE** → confirm the prompt.
3. **Important:** if your old system's news-agent also manages SL on this same account, run live on only one system at a time — they can fight over the same positions.
4. First live trade: verify the Telegram alert matches what MT5 shows (entry, SL, TP, lots).

---

## Quick troubleshooting

| Symptom | Fix |
|---|---|
| EA: offline on dashboard | server running? IP right? WebRequest URL exact? firewall? same network? |
| Connected but no candles | wait for the 60 s bars push; check Experts tab for `bars push OK` |
| No trades ever | check Engine Analysis notes; check pairs are enabled; mode ≠ OFF |
| `open_trade REFUSED` in Experts tab | `AllowTradeExecution` still false (that's the safety working) |
| Port conflict on restart | `lsof -ti :8766 \| xargs kill -9` |
