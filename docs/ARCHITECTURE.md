# Architecture — multi-strategy, multi-venue trading platform

> **Superseded — historical.** This predates the venue/host split, the Robinhood adapter, the strategy hosts, the three-state idempotency probe and the current dashboard. For the platform as it actually is now, read [PLATFORM.md](PLATFORM.md). Kept for the design history and the reasoning that led here.


> Target state and the reasoning behind it. Written August 2026, when the system was
> one strategy (SLC), one venue (MT5), and zero closed trades. Read
> `PLATFORM-REQUIREMENTS-ANALYSIS.md` first for the venue landscape; this document is
> about the shape of the code.
>
> Nothing here recommends what to trade. It specifies how to add strategies without
> each one becoming a new way to lose money.

---

## 0. The governing idea

Everything below follows from one sentence:

> **A strategy decides what it wants. The engine decides what actually happens.**

This is already the repo's best property and it is worth naming, because every future
change will be tempted to break it. `engine.py` is the sole execution choke point: risk
sizing, kill switches, exposure limits, concurrency, news blackouts and session calendars
all live there, and the only way an order reaches a venue is through it.

A strategy that could size its own position, or a venue adapter that could decide to skip
the daily stop, would each turn one rail into N rails. The architecture below exists to
make adding strategies and venues *cheap* while making that particular mistake *impossible*.

---

## 1. Current state — honest assessment

### What is genuinely good

- **Single choke point.** `engine.try_execute` is the only path to a position.
- **Write-layer parameter enforcement.** `params_store.set_param` holds per-origin
  whitelists and hard code ceilings. The agent physically cannot reach `risk_pct`.
- **Promotion gate.** Five checks per strategy × asset class before live is even requestable.
- **Decision audit.** `decisions.py` records the decisions *not* to trade, which is where
  most of the diagnostic value lives.
- **Factor exposure ledger.** EUR longs, DAX longs and gold longs recognised as one bet.

### What blocks multi-strategy today

The registry in `strategies/__init__.py` is a good first move that stops short of being an
architecture. Six concrete gaps:

| # | Gap | Consequence |
|---|---|---|
| **G1** | **New plugins default to ENABLED** — `is_enabled` returns `params.get(..., True)` | Appending a plugin makes it trade immediately, contradicting "each strategy clears its own gate" |
| **G2** | **Shadow routing is by SYMBOL, not by strategy** — the engine checks `if symbol in shadow_only` | There is no way to run a *strategy* in shadow on symbols that trade live |
| **G3** | **Flat parameter namespace** | Two strategies both wanting `min_rr` collide silently |
| **G4** | **No declared asset-class or timeframe scope** | A crypto-only strategy is still invoked for EURUSD, and a strategy needing 1m bars cannot say so |
| **G5** | **Hardcoded `REGISTRY` list** | Every new strategy edits a shared file |
| **G6** | **No per-strategy risk budget** | Three strategies each sized at the global cap is 3× the intended risk |

**G1 and G2 together are a safety defect, not a design preference.** `CLAUDE.md` states
each new strategy clears its own ≥50-trade gate before live. The code cannot currently
express that, because a newly registered plugin is enabled by default and its signals go
straight to `try_execute` on any symbol not in the symbol-level shadow list. The promotion
gate computes per-strategy numbers but nothing consults them at execution time.

---

## 2. Target architecture

```
   discovery: strategies/*.py declare a StrategySpec
                        |
              StrategyRegistry  — validates specs, resolves stage from DB
                        |
        +---------------+---------------+
        |               |               |
      SLC            grid/DCA        <next>          each: own params, own stage,
   (spec+impl)      (spec+impl)    (spec+impl)       own gate, own risk weight
        +---------------+---------------+
                        |
                   candidate signals
                        |
   ============ engine.py — THE choke point ============
     stage routing · risk sizing · loss governor · kill switches
     exposure gate · concurrency · session calendar · news blackout
   =====================================================
                        |
                 BrokerAdapter interface
       place_order · cancel · positions · balance · stream · symbol_meta
        |          |          |          |           |
      MT5       Alpaca      CCXT       IBKR       OANDA
                        |
                 SQLite (WAL, local, authoritative)
                        |
                 read-only projections -> dashboard
```

### 2.1 The strategy contract

A strategy declares what it is; it does not decide what it may do.

```python
class Stage(str, Enum):
    DISABLED = "disabled"   # not invoked at all
    SHADOW   = "shadow"     # signals recorded, never executed, never notified
    PAPER    = "paper"      # executes against the paper broker
    LIVE     = "live"       # executes for real — reachable only via live_switch

@dataclass(frozen=True)
class StrategySpec:
    name: str                        # stable identifier, appears in every DB row
    version: str                     # bump on behaviour change (see 2.3)
    description: str
    asset_classes: frozenset[str]    # engine will not invoke it outside these
    trade_modes: frozenset[str]      # {"intraday", "swing"}
    timeframes: tuple[str, ...]      # bars it requires; engine skips if unavailable
    params: Mapping[str, tuple]      # namespaced key -> (default, lo, hi)
    risk_weight: float = 1.0         # share of the global risk budget
    max_stage: Stage = Stage.PAPER   # CEILING the plugin author may not exceed
```

Two properties make this safe:

**`max_stage` is a ceiling, not a setting.** A plugin cannot declare itself LIVE. The
*effective* stage is stored in the DB and moved only by `params_store` (up to PAPER) or
`live_switch` (to LIVE, behind the two-step gate). `effective = min(db_stage, spec.max_stage)`.

**Default stage is `SHADOW`, not enabled.** Closing G1. A newly discovered strategy
observes and records; it executes nothing until a human moves it, and reaches LIVE only
through the promotion gate. The default for an unvalidated strategy should be the safe
state, and today it is the opposite.

### 2.2 Stage routing in the engine

Closing G2. The engine routes on the strategy's effective stage first, and only then
applies the existing symbol-level shadow list:

```python
stage = registry.effective_stage(sname)
if stage is Stage.DISABLED:               continue
if stage is Stage.SHADOW or symbol in shadow_only:
    try_execute_shadow(sig, p)            # recorded, silent, never notified
else:
    try_execute(sig, p)                   # all rails, as today
```

`try_execute` gains one assertion at the top: refuse any signal whose strategy is not in
PAPER or LIVE. Defence in depth — the router should never pass one, and if it does, the
choke point still refuses.

### 2.3 Version bumps void sign-offs

`StrategySpec.version` is not documentation. A sign-off attests that *this behaviour*
produced *this expectancy*. Change the behaviour and the attestation is void.

This composes with the sign-off invalidation already implemented in `analysis.py`: a
sign-off is void if any accepted parameter change postdates it. Extend the same rule to
strategy version — same principle, same failure-closed direction.

### 2.4 Per-strategy risk budget

Closing G6. Today three strategies each size at `risk_pct` means 3× intended risk. The
global cap must be divided, not replicated:

```
effective_risk_pct = risk_pct * (spec.risk_weight / sum(active weights)) * governor * session
```

The existing guard stays absolute: **factors may only reduce risk, never raise it.**
`RISK_PCT_CEILING` remains the ceiling no combination of weights can exceed.

### 2.5 Parameter namespacing

Closing G3. Every strategy parameter is `strategy.<name>.<key>`, with bounds declared in
the spec and enforced by `params_store` exactly as global parameters are today. A strategy
can only write its own namespace. Global risk parameters remain unreachable from a plugin.

### 2.6 Discovery and conformance

Closing G4 and G5. Strategies are discovered by importing `strategies/*.py` and collecting
module-level `SPEC` objects. No shared file to edit.

Discovery alone is not enough — a conformance suite runs against every registered spec:

- the spec validates (bounds sane, weight positive, timeframes known)
- `generate()` returns the documented `{signal, info}` shape
- `generate()` is **pure**: no DB writes, no order placement, no network
- given identical bars it returns an identical signal (determinism, so backtests mean something)
- it never emits a signal for an asset class outside its declared set
- **`max_stage` is not LIVE** unless explicitly reviewed

A strategy that fails conformance is not registered. This is the mechanism that keeps
"rails live in the engine" true as the plugin count grows.

---

## 3. Broker adapter layer

Detailed in `PLATFORM-REQUIREMENTS-ANALYSIS.md` §2. In summary: extract the interface from
the existing MT5 command queue, prove it with Alpaca as the second implementation, use CCXT
for all crypto rather than per-exchange code.

```python
class BrokerAdapter(Protocol):
    name: str
    is_paper: bool
    def symbol_meta(self, symbol) -> SymbolMeta:      ...   # tick size, min lot, fees
    def balance(self) -> Balance:                     ...
    def positions(self) -> list[Position]:            ...
    def place_order(self, order: Order) -> OrderId:   ...   # MUST be idempotent
    def cancel(self, order_id: OrderId) -> None:      ...
    def stream_prices(self, symbols) -> Iterator[Tick]: ...
```

Two non-negotiables:

**Idempotent order placement.** Every order carries a client-generated ID. A dropped
response must never produce a double fill — the single most expensive bug class in
automated trading, and it is an adapter concern that no rail can catch after the fact.

**Reconciliation.** Local position state versus venue truth, on a timer. Any drift halts
new entries and alerts. This is how you find out the adapter is lying before the drawdown does.

---

## 4. State and data

**SQLite (WAL) stays the authoritative store, on local disk, and remains the only thing
the engine reads in the hot path.** The engine must keep managing stops through loss of
internet to every non-venue service. A hosted database in that path converts a network
blip into an inability to read your own positions.

Two operational rules that follow:

- **Never run the DB inside a cloud-synced folder** (OneDrive, Dropbox, iCloud). WAL-mode
  SQLite plus a background sync agent is a corruption mechanism aimed at the file your kill
  switches read. Keep the runtime checkout on plain local disk.
- **Remote dashboards get a read-only projection**, pushed best-effort. Publication failing
  must never block trading, and control actions never flow back along that path.

For remote access today, a WireGuard-style overlay (Tailscale) or an authenticated tunnel
beats hosting: device-level auth, no new public surface, no code change.

---

## 5. Lifecycle — the path a strategy walks

```
  written -> conformance suite -> DISCOVERED
                                      |
                                   SHADOW      signals recorded, nothing executed,
                                      |        no notifications, no risk consumed
                        human review of shadow decisions
                                      |
                                   PAPER       executes on the paper broker,
                                      |        full rails, accumulating the sample
                        promotion gate: >=50 closed trades, positive
                        expectancy, GROUNDED data trust, rails fired,
                        human sign-off aged >=1h and not superseded
                                      |
                                    LIVE       two-step confirm + EA AllowTradeExecution
```

Per strategy × asset class × venue. A strategy proven on MT5 FX is unproven on Binance
spot: different fills, fees, minimum notionals and settlement. The gate cell must include
the venue once adapters land.

De-escalation is always one step and never gated by sample size.

---

## 6. Requirements

### Strategy framework

| ID | Requirement | Priority |
|---|---|---|
| **SF-1** | `StrategySpec` dataclass with the fields in §2.1 | must |
| **SF-2** | Default stage for a newly discovered strategy is `SHADOW` | **must (closes G1)** |
| **SF-3** | Engine routes execution on effective stage before symbol shadow list | **must (closes G2)** |
| **SF-4** | `try_execute` refuses any signal whose strategy is not PAPER/LIVE | must |
| **SF-5** | Parameters namespaced `strategy.<name>.<key>`, bounds via `params_store` | must (G3) |
| **SF-6** | Engine skips a strategy outside its declared asset classes / timeframes | must (G4) |
| **SF-7** | Auto-discovery of `strategies/*.py`; no shared registry file | should (G5) |
| **SF-8** | Risk budget divided by `risk_weight`, never multiplied | **must (G6)** |
| **SF-9** | Conformance suite gates registration; purity and determinism included | must |
| **SF-10** | Strategy version bump voids an existing sign-off | must |
| **SF-11** | `max_stage` is a ceiling a plugin cannot raise | must |

### Broker layer

| ID | Requirement | Priority |
|---|---|---|
| **BL-1** | `BrokerAdapter` protocol per §3 | must |
| **BL-2** | Conformance suite every adapter passes before use | must |
| **BL-3** | Idempotent order placement with client-generated IDs | **must** |
| **BL-4** | Position reconciliation on a timer; drift halts entries and alerts | must |
| **BL-5** | MT5 refactored behind the interface with no behaviour change | must |
| **BL-6** | Alpaca adapter as the second implementation (validates the design) | should |
| **BL-7** | CCXT adapter covering crypto venues generically | should |
| **BL-8** | Promotion gate cell extended to strategy × class × **venue** | must |

### Platform

| ID | Requirement | Priority |
|---|---|---|
| **PL-1** | SQLite local remains authoritative; no hosted DB in the hot path | must |
| **PL-2** | Runtime checkout never inside a cloud-synced folder | must |
| **PL-3** | Single consolidated UI; retire the Flask dashboard | should |
| **PL-4** | Remote access via overlay network, not public hosting | should |
| **PL-5** | Venue credentials in the runtime DB only, never in source | must |
| **PL-6** | Backtesting surfaced in the UI, reusing `backtest.py` | could |

### Invariants that must survive all of the above

Every rule in `CLAUDE.md` §"Safety invariants" holds unchanged. Specifically, no change
below may:

- move a rail out of `engine.py` into a strategy or an adapter
- allow any combination of weights or factors to exceed `RISK_PCT_CEILING`
- create a second path to `trading_mode = live` beside `live_switch`
- let a strategy write outside its own parameter namespace
- make de-escalation harder than escalation

---

## 7. Sequence

Each phase is independently valuable and leaves the system working.

| Phase | Work | Exit criterion |
|---|---|---|
| **0** | Run the current bot on an MT5 demo | closed paper trades exist and are non-zero |
| **1** | SF-1…SF-4, SF-11 — spec, stages, engine routing | a second strategy can be added in SHADOW and observed safely |
| **2** | SF-5, SF-6, SF-8, SF-9 — namespacing, scoping, budget, conformance | two strategies coexist without competing for the same risk |
| **3** | BL-1…BL-5 — adapter interface, MT5 behind it | MT5 behaviour unchanged, interface proven |
| **4** | BL-6 — Alpaca | the design is validated by a genuinely different venue |
| **5** | Second strategy family through the full gate | the lifecycle is proven end to end |
| **6** | BL-7, PL-3, PL-4 — CCXT, UI consolidation, remote access | breadth |

**Phase 1 is the one that matters most**, because G1 and G2 are open today. Until stage
routing exists, "add a strategy" and "put money behind an unvalidated strategy" are the
same action.

**Phase 0 gates everything.** The promotion gate needs 50 closed trades per cell and there
are currently zero. Architecture cannot substitute for a sample.
