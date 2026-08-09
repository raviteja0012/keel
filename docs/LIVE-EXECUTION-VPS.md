# Live execution topology — Mac + Windows VPS (Phase 5 runbook)

The MetaTrader5 Python package is Windows-only, but this system never needed
it: live execution is the **EA command queue** — the server enqueues typed
commands in SQLite, the `SLCDataBridge` EA polls `/api/commands/next` and
executes inside the MT5 terminal, then acks. The EA **is** the thin execution
client. Going live on MT5-routed instruments (forex/metals/indices/energies —
and crypto CFDs, per the Vantage-routed decision) therefore means running an
MT5 terminal + EA on a Windows VPS pointed at the Mac's server, nothing more.

```
┌────────────────── Mac (always on) ───────────────────────────┐
│ server.py  (Flask :8766, LAN/VPN)  ← EA feed & command poll  │
│ engine + strategies + risk rails + paper router              │
│ news_agent.py · news calendar · self-tuning agent            │
│ dashboard_api.py (FastAPI, 127.0.0.1:8767 ONLY)              │
│ data/trading.db  = source of truth + command queue           │
└────────────▲─────────────────────────────────────────────────┘
             │  HTTPS/HTTP over Tailscale (or WireGuard) — never the
             │  open internet; the EA URL allow-list pins the host
┌────────────┴────────────────┐
│ Windows VPS (live MT5 only) │
│ MT5 terminal (Vantage) +    │
│ SLCDataBridge EA v2.30      │
│   AllowTradeExecution=false │  ← flip manually, on the VPS, only after
│   MaxLotsPerTrade cap       │    the dashboard promotion gate is green
│   MaxOpenPositions cap      │
└─────────────────────────────┘
```

## Why this shape

- **Paper trading and research never depend on the VPS.** The Mac's paper
  router consumes the same EA data feed (which any MT5 terminal — including
  the current setup — can push). The VPS is needed only when real orders
  must execute 24/5 without the Mac's MT5 running.
- **The DB is the sync mechanism** (as scoped): commands are rows with
  `pending → sent → acked | expired` states and a TTL, so a VPS outage can
  never execute stale stops or duplicate an order when it reconnects
  (`storage.py`: `COMMAND_TTL_S`, `RESEND_GRACE_S`).
- **The double gate survives the split.** Software gate: two-step,
  promotion-gated switch (`live_switch.py`, dashboard-only, token + confirm
  phrase). Hardware gate: `AllowTradeExecution` in the EA inputs on the VPS,
  default `false`, with `MaxLotsPerTrade` / `MaxOpenPositions` backstops.
  Neither side alone can place a live order.

## Setup checklist (when the promotion gate is actually green)

1. **Network**: install Tailscale on Mac + VPS; note the Mac's tailnet IP.
   Do not port-forward 8766 on any public interface.
2. **VPS**: install the Vantage MT5 terminal; log into the **live** account
   (paper validation stays on demo/any terminal).
3. **EA**: copy `SLCDataBridge.mq5` (v2.30) to the VPS terminal, compile,
   attach to one chart. Inputs:
   - `ServerHost` = Mac tailnet IP, `ServerPort` = 8766
   - `AllowTradeExecution` = **false** until the final flip
   - `MaxLotsPerTrade` = minimum viable size for the first live weeks
   - allow-list `http://<mac-tailnet-ip>:8766` in MT5 WebRequest settings.
4. **Only one stop manager per account** (CLAUDE.md gate): confirm no other
   EA/system manages stops on that account.
5. **Rehearse the loop in paper**: with the VPS EA connected, run paper for
   at least a week — the EA feed/ack cycle over the VPN is itself part of
   what must be validated (latency, reconnects, weekend behavior).
6. **Go-live sequence** (in this order):
   a. dashboard → Promotion gate → confirm the cell is GREEN and signed off;
   b. dashboard → Controls → Live trading → Request → type `GO LIVE`;
   c. VPS → EA inputs → `AllowTradeExecution = true`;
   d. watch the first trade end-to-end; sizes start at minimum (playbook §12
      — live continues forward-testing).
7. **Kill order** (reverse): EA `AllowTradeExecution=false` is the fastest
   hard stop; dashboard HALT stops new entries; `/api/live/paper` drops the
   mode back.

## Crypto note

Per the Phase 1 sign-off, crypto routes through Vantage MT5 CFDs — same EA,
same queue, same gates; the instrument registry carries its 24/7 calendar,
UTC day boundary, and weekend half-risk. If a native exchange is ever added,
it enters as a second `OrderRouter`/`DataSource` adapter behind the same
rails, with keys in the macOS Keychain — never in the repo or DB.
