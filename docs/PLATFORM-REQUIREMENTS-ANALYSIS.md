# Platform Requirements Analysis — multi-venue, multi-strategy, UI

> Status: **draft for decision**, written August 2026. Venue API facts verified against
> vendor documentation on 2026-08-08; re-verify before committing engineering time, this
> layer changes fast. Nothing here is trading advice — which strategies earn money is an
> empirical question that this repo already has the right instrument for (the promotion
> gate in `analysis.py`). This document is about what to *build*, not what to *trade*.

---

## 1. Venue connectivity — what is actually possible

The single most important finding: **the venue list splits cleanly into "real API", "crypto
only", and "not possible"**. Several things people assume are connectable are not.

| Venue | Official API | Paper / demo | Asset classes | Verdict |
|---|---|---|---|---|
| **MetaTrader 5** | EA bridge (built, `SLCDataBridge.mq5` v2.30) | demo accounts | FX, metals, indices, some crypto | **Keep.** Already integrated and working |
| **Alpaca** | REST + WebSocket, first-class SDKs | **unlimited paper**, no minimum | US equities, crypto | **Add first.** Lowest-friction real paper trading |
| **Interactive Brokers** | TWS/Gateway + Web API | paper account | equities, options, futures, FX, bonds; 150+ markets, 33 countries | **Add for breadth.** Heaviest integration, widest reach |
| **OANDA** | REST + streaming prices | practice accounts | FX, CFD | Add if you want FX off MT5 |
| **Binance** | full REST + WebSocket | **testnet** | crypto | Add via CCXT, not directly |
| **Webull** | OpenAPI — HTTP + MQTT + gRPC | check at signup | stocks, options, futures, crypto, event contracts | Viable. **Requires application, ~1–2 business day review** |
| **Robinhood** | **Crypto Trading API only** (`trading.robinhood.com`) | none | **crypto only** | Low priority. **No official equities/options API** |
| **TradingView** | **outbound alert webhooks only** | n/a | n/a | **Cannot pull data.** See below |
| **3Commas / Cryptohopper / Bitsgap / Altrady** | they are bot platforms themselves | varies | crypto | **Competitors, not dependencies** |

### Three corrections worth internalising

**TradingView cannot be used as a data source.** There is no supported way to pull chart
data or account data from a TradingView account. The only official integration is *outbound*:
an alert fires and TradingView POSTs to a URL you configure. It requires a **paid plan** and
**2FA enabled**. This repo already implements exactly that (`tv_webhook.py`, `/api/tv_webhook`),
so the correct mental model is *TradingView is a signal publisher, not a data feed*. Anything
claiming to pull TradingView data is scraping, which breaks their terms and will break.

**Robinhood is crypto-only for automation.** The endpoints their own app uses for stocks and
options are undocumented and unsupported. Libraries that wrap them exist, are unofficial, and
put the account at risk. If equities automation matters, Alpaca / IBKR / Webull are the
supported routes.

**3Commas is a peer, not a building block.** Integrating it would mean running a bot on top of
a bot — two risk engines disagreeing about the same position. Connect to exchanges directly.

---

## 2. The architectural decision that matters most

Do **not** write a Binance integration, then an Alpaca integration, then an IBKR integration.
Seven venues written seven times is seven places for a rail to be missed — and the rails are
the whole point of this codebase.

The repo is already shaped correctly for the alternative. `engine.py` is a single execution
choke point, and live orders leave through one narrow door: `storage.enqueue_command`. That
door becomes the adapter interface.

```
                       strategies/ (SLC, + others)
                                 |
                        engine.py  — ALL rails live here
             (risk %, kill switches, exposure gate, loss governor,
              news blackout, session calendar, promotion gate)
                                 |
                     broker adapter interface
      place_order · cancel · positions · balance · stream_prices · symbol_meta
        |          |          |          |           |            |
      MT5 EA    Alpaca      IBKR      OANDA       CCXT        Webull
     (built)                                   (100+ crypto)
```

Rules that keep this safe:

1. **Rails never move into an adapter.** An adapter translates and transports; it never decides.
2. **Every adapter implements the same interface**, including a `paper`/`demo` flag, so the
   promotion gate applies per venue as well as per strategy × asset class.
3. **CCXT for all crypto.** One library covers 100+ exchanges with normalised order books,
   OHLCV, balances and orders. Writing Binance by hand and then Coinbase by hand is wasted work.
4. **Each new venue clears its own promotion gate.** Execution semantics differ — partial fills,
   min notional, fee models, settlement. A strategy proven on MT5 FX is unproven on Binance spot.

---

## 3. Should you use Vercel and Supabase?

**Short answer: not for the engine. Maybe later for the UI. Neither is needed now.**

### Supabase — no, not for trading state

Supabase is hosted Postgres. Moving `trading.db` there puts a network hop in the path where
money is. Ask what happens when your home internet drops for ninety seconds while three
positions are open: today, SQLite in WAL mode keeps working and the engine keeps managing
stops. With Supabase as the source of truth, the engine cannot read its own positions.

That is a new failure mode in the exact place you least want one, bought for remote access you
can get in other ways. `CLAUDE.md` invariant 7 already says never act on numbers from a DB you
cannot trust — unreachable is a stronger form of untrusted.

**If you later want remote dashboards**, the safe shape is one-directional:

```
engine → SQLite (source of truth, local, authoritative)
             ↓  async publisher, best-effort, never blocking
        Supabase (read model: redacted snapshot for the UI)
             ↓
        Vercel-hosted UI (read-only)
```

The publisher failing must never stop trading. Control actions do **not** flow back this way.

### Vercel — fine for a read-only UI, wrong for control

Vercel hosts frontends well. But your dashboard is not a content site: it can halt trading,
change risk parameters, and flip to live. Exposing that to the public internet turns a
localhost-only control plane into an internet-facing one.

Note what `live_switch.py` assumes today — the FastAPI control dashboard binds **localhost only**
on 8767, and that binding is part of the safety argument. Publishing it to Vercel removes an
assumption the design depends on.

### What to do instead, right now

For remote access to the existing dashboards, use **Tailscale** or a **Cloudflare Tunnel**. Both
give you your dashboard on your phone in about ten minutes, with device-level auth, no code
changes, no new database, and no new public attack surface. This is a fraction of the work of
Vercel + Supabase and strictly safer.

Revisit hosted infrastructure when you have a second user or need dashboards while the trading
machine is off. Neither is true today.

---

## 4. UI — what exists and what to build

You currently have **two** UIs, which is one too many:

| Surface | Port | Role |
|---|---|---|
| Flask dashboard (`dashboard/index.html`) | 8766 | original operator view, served by the engine process |
| FastAPI control dashboard (`dashboard/multiasset.html`) | 8767 | newer, localhost-only, token-gated, 20 endpoints |

**Recommendation: converge on the FastAPI one and retire the Flask view.** It already has the
token gate, the promotion-gate status, the decision audit, and the param-change queue. Two
dashboards means two places to add every future control and two places to forget one.

Screens worth having, roughly in order of value:

1. **Positions & P&L** — open, closed, per strategy × venue × asset class
2. **Promotion gate** — the five checks per cell, and *why* a gate is closed (this is the
   screen that decides when you go live, so it should be the best one)
3. **Decision audit** — including decisions *not* to trade, which is where the interesting
   information is; `decisions.py` already records these and nothing surfaces them well
4. **Risk state** — daily/weekly kill-switch headroom, loss-governor status, exposure buckets
5. **Venue health** — feed age, adapter connectivity, per-venue error rates
6. **Parameter change queue** — what the agent wants to change, what was refused and why

---

## 5. Competitive landscape

Reviewed: **3Commas, Cryptohopper, Bitsgap, Altrady**. All four are crypto-only, subscription
(roughly $20–$100+/month), non-custodial, and connect to exchanges via your API keys.

> Caveat on sources: the most-cited head-to-head comparison of these four is **published by
> Altrady**, one of the four. Treat the rankings as marketing, and the *feature lists* as the
> useful part.

**What they have that this repo does not:**

- **Grid and DCA bots** — mechanical, well-understood, easy to backtest. The most commoditised
  feature in the category and a reasonable strategy family to add
- **Backtesting in the UI** — Bitsgap advertises 365-day backtesting; this repo has
  `backtest.py` but no interface to it
- **Copy trading / strategy marketplaces** — Cryptohopper's differentiator
- **Multi-exchange portfolio view** — 18+ exchanges typical
- **Mobile apps**

**What this repo has that they do not:**

- Hard risk rails enforced in code with ceilings that runtime settings cannot exceed
- A promotion gate requiring 50 closed trades, positive expectancy, a data-trust verdict,
  demonstrably-fired rails, and a human sign-off before live
- A decision audit that records the decisions *not* to trade
- Cross-asset factor-bucket exposure limits — EUR longs, DAX longs and gold longs recognised
  as one stacked bet
- Bespoke price-action logic rather than parameterised grid/DCA

The honest read: **the commercial bots are better at convenience, this repo is better at not
blowing up.** Do not chase feature parity. Take the two ideas worth having — grid/DCA as an
additional strategy family, and backtesting surfaced in the UI — and leave the rest.

---

## 6. Strategy families worth implementing

Framing: these are *engineering targets*, each implemented behind the existing strategy registry
and the shared rails, each clearing its own promotion gate. Which of them actually make money on
your venues and timeframes is exactly what the gate is for — nothing below is a recommendation
to trade.

| Family | Fits | Notes |
|---|---|---|
| **Price action / structure** (SLC) | built | strategy #1 |
| **Trend following** | FX, indices, crypto | partially present via SLC's structure logic |
| **Mean reversion / range** | FX crosses, equities | needs an explicit regime filter; `regime_max` exists |
| **Breakout / volatility expansion** | all | natural pair with the existing ATR machinery |
| **Grid / DCA** | crypto, ranging markets | what the commercial bots sell; mechanical, testable |
| **Cross-sectional momentum** | equities, crypto baskets | needs ranking across a universe — new shape for this engine |
| **Pairs / statistical arbitrage** | equities, crypto | needs two-legged positions; the exposure ledger already thinks in factors |
| **Carry** | FX, perp funding rates | funding-rate carry is crypto-native and CCXT exposes it |

**Sequencing:** add **one** family, take it through the full gate, and only then add the next.
Adding six strategies before any of them has 50 closed trades produces six unvalidated systems
and no information.

---

## 7. Requirements

### Functional

- **FR-1** Broker adapter interface with a conformance test suite every adapter must pass
- **FR-2** Adapters: Alpaca (first), CCXT/Binance, IBKR, OANDA, Webull — MT5 stays as-is
- **FR-3** Per-venue paper/demo mode, with the promotion gate applied per strategy × class × venue
- **FR-4** Strategy registry extended with at least one non-SLC family, fully gated
- **FR-5** Single consolidated UI; retire the Flask dashboard
- **FR-6** Backtesting surfaced in the UI, reusing `backtest.py`
- **FR-7** Remote access via Tailscale or Cloudflare Tunnel — not public hosting
- **FR-8** Per-venue health and reconciliation: local position state vs venue truth, alert on drift

### Non-functional

- **NFR-1** The engine must keep trading through loss of internet to any non-venue service
- **NFR-2** No rail may be implemented inside an adapter — `engine.py` stays the only choke point
- **NFR-3** Secrets (venue API keys) never in source; runtime DB only, entered via the dashboard —
  this extends the existing rule to every new venue
- **NFR-4** Every adapter's order path is idempotent under retry — a dropped response must never
  double-fill
- **NFR-5** All existing `CLAUDE.md` invariants hold unchanged across every venue

### Open questions

1. Which venue is the live-with-demo-money target first? That decides adapter order.
2. Is Webull's application approval worth starting now, given the 1–2 day review?
3. Are equities in scope at all, or is this FX + crypto? Equities pull in IBKR/Alpaca and
   market-hours complexity that `sessions.py` only partly models.
4. Single-user forever, or eventually multi-user? Only the second justifies hosted infrastructure.

---

## 8. Suggested sequence

**Phase 0 — before any new venue.** Get the current bot running against an MT5 demo and
accumulate closed trades. The promotion gate needs 50 per cell and you have none. Every phase
below is worth less until this number is non-zero.

**Phase 1 — adapter interface + Alpaca.** Extract the interface from the existing MT5 command
queue, prove it with a second implementation. Alpaca because unlimited paper and the least
friction. This is where the design gets validated.

**Phase 2 — CCXT for crypto.** One adapter, many exchanges. Binance testnet first.

**Phase 3 — UI consolidation.** Retire Flask, add the promotion-gate and decision-audit screens,
surface backtesting. Tailscale for remote access.

**Phase 4 — second strategy family.** Grid/DCA is the natural candidate: mechanical, easy to
validate, and it is what the commercial platforms are built on.

**Phase 5 — breadth.** IBKR and/or Webull, if equities are genuinely in scope.

---

## Sources

Venue API status verified 2026-08-08:

- Robinhood Crypto Trading API — https://robinhood.com/us/en/newsroom/robinhood-crypto-trading-api/
- Webull OpenAPI — https://developer.webull.com/apis/docs/
- TradingView webhook alerts — https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/
- CCXT — https://docs.ccxt.com/
- Broker API comparison — https://brokerchooser.com/best-brokers/best-brokers-for-algo-trading
- Crypto bot comparison (published by Altrady — biased source, features useful, rankings not) —
  https://www.altrady.com/blog/crypto-bots/altrady-vs-cryptohopper-vs-3commas-vs-bitsgap-2026
