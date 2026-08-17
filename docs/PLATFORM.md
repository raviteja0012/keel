# Keel — platform reference

Keel is a daemon trading engine: it consumes prices from every configured source, generates signals, executes them on the paper broker or via live EA commands, and manages open trades (TP1 -> break-even -> structure trail) through one execution choke point (`engine.py:1-3`). This is the canonical platform reference, written for an engineer who will operate Keel with real money; every claim below is grounded in the shipped code and cited to file:line.

## Contents

1. [What Keel is, and the model it enforces](#what-keel-is-and-the-model-it-enforces)
2. [The safety rails, and why each exists](#the-safety-rails-and-why-each-exists)
3. [Venue and host reference](#venue-and-host-reference)
4. [Running Keel](#running-keel)
5. [Document map and what this supersedes](#document-map-and-what-this-supersedes)

---

## What Keel is, and the model it enforces

Keel is a daemon trading engine. Its own module docstring states the job plainly: it "consumes prices from every configured source, generates signals, executes them on the paper broker or via live EA commands, and manages open trades (TP1 -> break-even -> structure trail)" (`engine.py:1-3`). It runs as a daemon thread started by `server.py`, and every mutable runtime parameter comes from `storage.settings` rather than source or static config (`engine.py:12-14`).

The model below is not advisory. It is enforced in the type contracts of the two adapter protocols, and the docstrings record the specific defects each rule exists to prevent.

### 1. engine.py is the sole execution choke point

Orders leave through exactly one door. `brokers/__init__.py` titles itself "Venue adapters — the one door orders leave through" (`brokers/__init__.py:1`) and draws the line without ambiguity:

> "The engine decides WHAT to do. An adapter only knows HOW to say it to a particular venue. Nothing in here may size a position, widen a stop, or decide whether a trade is allowed: those are engine rails and they stay in engine.py. An adapter that makes a decision is a bug." (`brokers/__init__.py:3-6`)

The `Order` dataclass encodes the same split: it is "An instruction the engine has already approved. The adapter transmits it." (`brokers/__init__.py:136`). The strategy-host module restates the venue side of the contract from the other direction: "a venue Keel trades through: Keel decides, sizes, places and manages, and the adapter only transmits." (`strategy_host.py:3-4`).

Two consequences follow in code. Adapters are constructed only from credentials held in the runtime DB, "never from source or config.yaml" (`brokers/__init__.py:8-9`). And the `BrokerAdapter` protocol is "Kept deliberately small: each method added here is a method every future venue has to get right." (`brokers/__init__.py:181-183`) — the surface an adapter is allowed to touch is minimised on purpose.

### 2. The two mandatory adapter properties

The module names two properties as "mandatory for anything that touches real money" (`brokers/__init__.py:11`). Quoting the contract:

> "idempotency   place_order() carries a caller-generated client_order_id. A dropped response must be safe to retry. Double fills are the most expensive bug class in automated trading and no downstream rail can undo one." (`brokers/__init__.py:13-16`)

> "read_only     A venue is read-only until trading is explicitly enabled for it. Adding credentials lets you SEE the account; it does not let the engine trade it." (`brokers/__init__.py:18-21`)

Both are enforced in the protocol, not left to convention. `read_only: bool` is a required attribute of `BrokerAdapter` (`brokers/__init__.py:185`). `place_order` carries the obligation in its signature docstring: "MUST be idempotent on order.client_order_id. Resubmitting the same id must return the existing order, never create a second one." (`brokers/__init__.py:197-199`). The idempotency key is the caller-generated `client_order_id` field on `Order` (`brokers/__init__.py:137`).

### 3. The three-state Probe, and why `Optional[Dict]` was the trap

Idempotency requires an adapter to check whether an order already exists before or after submitting. That probe has three outcomes, not two: `PROBE_FOUND`, `PROBE_ABSENT`, `PROBE_INDETERMINATE` (`brokers/__init__.py:80-82`). The reason it must be three and never a nullable dictionary is recorded verbatim:

> "The idempotency probe has exactly three answers, and 'indeterminate' is not a spelling of 'absent'. Every adapter that probes before or after submitting MUST use this vocabulary rather than Optional[Dict], because Optional collapses 'the venue says no such order' and 'I could not find out' into one value — and an adapter reading the second as the first is the single most expensive bug this codebase has produced. It shipped in webull (disarmed) and again in ccxt_venue (fixed), both times undetected by a full green test suite." (`brokers/__init__.py:73-79`)

The trap: `Optional[Dict]` gives you a dict when the order is found and `None` otherwise. `None` then means both "the venue confirmed no such order" and "I could not find out" — and a retry keyed on `not order` fires in both cases. The retry on the second case is what creates the second position.

The `Probe` type closes this. Its `order` field is "only ever set for PROBE_FOUND" (`brokers/__init__.py:87`), and the `is_absent` property is documented as the thing to read instead of a truthiness check: "True only for a definite venue-confirmed absence. Read this rather than `not probe.order`, which is true for indeterminate too." (`brokers/__init__.py:91-95`).

Indeterminacy has its own exception. `VenueIndeterminate` means "We do not know whether the order exists, and therefore must not act." (`brokers/__init__.py:51-53`). It is "deliberately NOT retryable: a retry is exactly the action that turns 'might have landed' into two positions." (`brokers/__init__.py:57-62`). The only sanctioned responses are to reconcile against the venue or ask a human — "Both are slower than a retry. Both are cheaper than a double fill." (`brokers/__init__.py:64-70`). The related `VenueError` carries `http_status`, where a status code is the venue saying something and `None` is the venue saying nothing; the two "must never be handled alike." (`brokers/__init__.py:29-35`).

### 4. Venue vs strategy host, and one-position-one-owner

A venue and a host are opposite integration shapes, and confusing them is a documented failure mode.

A **venue** is traded through: Keel places and manages orders, the adapter only transmits (`strategy_host.py:3-4`; `brokers/__init__.py:3-6`).

A **strategy host** runs its own bots. Cryptohopper, Bitsgap, Altrady and 3Commas each run grid bots, DCA bots and signal bots that Keel does not write, "which is the entire reason to integrate them." (`strategy_host.py:5-7`). Keel only starts, stops and watches these; it never steers them.

Why the distinction is load-bearing, quoted: "modelling a bot platform as a broker is how you get two systems managing one position." Keel sizes to a risk budget and manages a stop; a grid bot ladders in and out on its own schedule. Point both at one position "and neither is in control: Keel's stop moves under the grid's feet, the grid's rungs fire against Keel's sizing, and the loss is bounded by nothing." (`strategy_host.py:9-14`).

The rule the module exists to enforce:

> "ONE POSITION HAS EXACTLY ONE OWNER, AND FOR A HOSTED BOT THAT OWNER IS THE HOST. Keel starts it, stops it, and watches it. Keel never manages it." (`strategy_host.py:16-20`)

Hosted positions are "REPORTED, never MANAGED." They still count toward exposure, drawdown and the kill switches — "money at risk is money at risk, whoever is steering" — but no engine rail may adjust, hedge or close them. "The only controls Keel has over a hosted position are start and stop." (`strategy_host.py:22-24`).

This is enforced structurally, in several places:

- **The `StrategyHost` protocol omits `place_order` and `symbol_meta` on purpose:** "A host that grows those is a broker, and belongs behind BrokerAdapter with the idempotency obligations that carries." (`strategy_host.py:130-133`).
- **Two separate registries.** `register_host` / `build_host` (`strategy_host.py:167-182`) are kept deliberately apart from the broker `register` / `build` (`brokers/__init__.py:207-223`) so that "`build_host("cryptohopper")` and `build("cryptohopper")` cannot be confused for one another, and a host can never be handed to code expecting something it can place an order through." (`strategy_host.py:160-166`).
- **`read_only` still gates control.** Reading a host's bots and positions is always allowed once credentials exist; starting or stopping a bot "is a state change on real money and requires the same explicit arming a venue does." (`strategy_host.py:57-63`).

Two properties make this shape safer than order routing rather than merely different. Bot control is "naturally idempotent": starting a running bot is a no-op, stopping a stopped one likewise, so "A dropped response to start_bot can be safely retried" — there is no double-fill equivalent (`strategy_host.py:29-36`). And there is "no sizing surrender": a host that lets Keel submit an order without a size and then picks the size itself "is not a host being helpful — it is a second decision-maker," and adapters must not use those endpoints (`strategy_host.py:38-44`).

One trap the host side guards against explicitly is what `stop_bot` does to open positions. Vendors differ, "the difference is expensive, and nothing in an API response announces it." (`strategy_host.py:66-73`). So each adapter must declare a `stop_disposition` — one of `STOP_UNKNOWN`, `STOP_CLOSES_POSITIONS`, `STOP_ORPHANS_POSITIONS` (`strategy_host.py:74-77`) — "Declared, not guessed." (`strategy_host.py:138`). An orphaned position after a stop is named the worst state in the whole design: no bot steering it and no Keel rail permitted to touch it (`strategy_host.py:68-73`).

Finally, hosted P&L is never silently coerced. `BotState.valued` is False when the host did not report P&L, and "None must not be summed as zero. An unvalued hosted position is exactly as dangerous as an unvalued local one — the difference is only that we cannot close it." (`strategy_host.py:106-114`). `hosted_exposure` returns an explicit `unvalued_bots` count rather than a reassuring total, because `sum(s.unrealized_pnl or 0)` "would report a host we cannot read as a host that is flat, which is the same mistake this codebase has now made four times in different clothes." (`strategy_host.py:185-200`).

---

## The safety rails, and why each exists

Every rail here is built on one principle stated in the code: when the honest answer to a safety question is "I cannot tell," the system stops opening risk rather than assuming benign. From `price_state`: "the honest answer to unknown provenance is to stand aside" (`engine.py:457`). From `trade_upnl`: valuing the book against a dead feed is a question where "the honest answer on a dead feed is 'I cannot'" (`engine.py:949`). Each rail below is that principle applied to one specific failure.

### Price freshness, bounded at both ends

`price_state` decides how much of a quote to believe (`engine.py:436-489`). Freshness is `-MAX_CLOCK_SKEW_S <= age <= max_age` (`engine.py:470`), where `age = now - src_t` (`engine.py:462`), `max_age` is the per-source `max_age_s` or the default `MT5_PRICE_MAX_AGE_S = 60` (`engine.py:463`, `engine.py:71`), and `MAX_CLOCK_SKEW_S = 1.0` (`engine.py:75`).

- Upper bound: a quote older than its limit is stale and not fresh (`engine.py:479-481`).
- Lower bound: an `age <= max_age` test alone reads a **negative** age as fresh, so a quote stamped in the future would be "fresh forever and never age out" (`engine.py:464-469`). A future stamp is treated as a clock problem — fast venue clock, NTP step, VM resume — tolerated for one second, then stood aside with the reason reported (`engine.py:476-478`).
- Unknowable age: a quote with no source timestamp (`src_t <= 0`) returns `fresh=False, reachable=False` rather than the old behaviour of `fresh=True` / permanently tradable (`engine.py:450-461`).
- Reachability: a source with no health row reads as unknown, not reachable — it previously assumed `True` when `rec is None` (`engine.py:471-474`).

Failure prevented: trading or valuing against a dead, future-dated, or unprovenanced quote. The fail-closed default is `fresh=False`.

Two callers split on this. `price_if_fresh` requires only recency, because a stop breached at the last real print was genuinely breached (`engine.py:492-497`). `price_for_entry` requires fresh **and** reachable, because "a venue we cannot reach is a venue we cannot trade, whatever its last print said" (`engine.py:500-507`). Managing an existing position and opening a new one are deliberately not held to the same test.

### An unvaluable open position halts new entries

`trade_upnl` values one open trade. For paper trades it now requires a fresh quote and returns `None` when the feed is stale (`engine.py:950-952`); it also returns `None` if tick value or size are non-positive (`engine.py:958-959`). Previously it read the book raw, so an hour-old print could value a position and feed that value into the kill switches (`engine.py:939-944`).

`_open_pnl` carries the count of positions that could **not** be valued instead of collapsing unknown to zero (`engine.py:1048-1068`). The prior `sum(trade_upnl(t) or 0)` turned an unknown into a zero, and "zero reads as 'that position is flat'" — the most reassuring possible answer to a question that could not be answered (`engine.py:1051-1057`). `open_pnl_total` is the same fix applied to the equity-sample path, where a falsely reassuring sample is worse than a missing one (`engine.py:971-985`).

`loss_limits_hit` acts on that count: if any open trade is unvalued, open drawdown is understated by an unknown amount, so it refuses new entries and says why (`engine.py:1093-1096`). This does not touch existing positions — their stops are still managed on the last confirmed price (`engine.py:1091-1092`).

Failure prevented: a position moving hard against the account while the daily/weekly stops see it as flat because its quote died.

### Hosted-bot drawdown gates the LIVE switches only

In `loss_limits_hit`, exposure from hosted bots (3Commas / Cryptohopper / Altrady) is folded into open drawdown only under `if mode == "live" and _HOSTS_AVAILABLE` (`engine.py:1108`). The reasoning in code: hosted bots are real money whichever mode Keel is in, but a paper entry risks nothing, so a hosted drawdown has no business halting it (`engine.py:1100-1107`).

Within live mode it fails closed the same way as the local book:
- If hosted exposure is not trustworthy — hosts unreadable or bots unvalued — total drawdown cannot be computed, so new entries stand aside (`engine.py:1110-1118`).
- `hosted_pnl` is read as `hx["unrealized_pnl"]` with no `or 0.0`, deliberately, so a broken trustworthy=True promise raises a loud `TypeError` rather than a silent zero — "the zero is how the last four fail-open defects hid" (`engine.py:1119-1123`). A trustworthy report that still carries no P&L value is refused rather than treated as zero (`engine.py:1124-1127`).

Failure prevented: a live account opening fresh risk while hosted bots bleed, or while their state is unknown.

### Profit never offsets realized loss

Open P&L is clamped before it meets the stops: `open_dd = min(0.0, open_pnl)` (`engine.py:1098`). Hosted P&L is clamped the same way: `open_dd += min(0.0, float(hosted_pnl))` (`engine.py:1128`). The stop tests then compare realized P&L since the period start plus this non-positive `open_dd` against the threshold (`engine.py:1129-1132`). Open gains cannot lift the account back above a stop it has breached. The docstring names this "conservative by construction" (`engine.py:1076-1081`).

The loss "day" and "week" follow the broker clock, not UTC midnight, because statements cut there (`engine.py:1077`, `engine.py:1084`). Both open and realized drawdown count against the same limit — being -2% underwater on open positions halts entries as surely as -2% realized (`engine.py:1078-1080`).

### The loss governor halves risk after three losses

`loss_governor` implements playbook §10.3: after 3 consecutive losses, halve risk until 2 consecutive wins (`engine.py:1136-1158`). It returns `0.5` when halved, `1.0` otherwise (`engine.py:1158`), a multiplier derived from the trades table so it is restart-safe with no counter to lose (`engine.py:1138-1139`).

It reads the last 50 closed trades, ordered oldest to newest, incrementing the loss streak (halving at 3) and clearing it on a win, un-halving only after 2 consecutive wins (`engine.py:1146-1157`). The SQL filters `pnl IS NOT NULL` (`engine.py:1145`): a closed row whose P&L was never written would otherwise read as a win, reset the loss streak, and silently un-halve risk (`engine.py:1140-1142`).

Failure prevented: continuing at full size through a losing run, and bad data (a NULL P&L) quietly disarming the throttle.

### The two-step live switch

`live_switch.py` is the only sanctioned path to `trading_mode = live`; `params_store` rejects `trading_mode` for every origin and the legacy Flask route is de-fanged (`live_switch.py:1-8`). Every endpoint on the router requires the dashboard control token via `require_token` (`live_switch.py:41`).

Going live takes two calls:
- `POST /request` is refused if any fail-safe is active — DB integrity suspect or a manual halt (`live_switch.py:61-67`, `109-117`) — or if no strategy×class promotion gate is open (`live_switch.py:118-125`). An open gate requires ≥50 closed paper trades, positive expectancy, a GROUNDED data-trust verdict, rails demonstrably exercised, and a manual sign-off row that is never automatic (`live_switch.py:14-18`, `122-125`). On success it issues a one-time `confirm_token` and the phrase `GO LIVE`, valid for `CONFIRM_TTL_S = 60` seconds (`live_switch.py:43-44`, `126-134`).
- `POST /confirm` requires the token (checked with `secrets.compare_digest`) and the phrase typed back exactly (`live_switch.py:140-149`). It then **re-validates** blockers and open gates, because the world may have changed in 60 seconds (`live_switch.py:150-157`). Only then does `trading_mode` flip to `live` (`live_switch.py:159`).

Even after the flip, this switch cannot place a live order by itself: the EA's `AllowTradeExecution` input (default false, EA-side) is the independent second half of the double gate (`live_switch.py:20-23`, `162-167`).

De-escalation is asymmetric on purpose. `POST /paper` flips back with no ceremony and is never gated (`live_switch.py:25`, `170-177`). `POST /signoff` records the human promotion sign-off for a cell but changes nothing itself (`live_switch.py:80-106`). Every action lands in `agent_log` and notifications (`live_switch.py:29`).

Failure prevented: a single compromised call, a fat-fingered toggle, or a stale approval promoting real-money trading; and a code path that could trade without the human-held EA gate also being true.

---

## Venue and host reference

Keel talks to two kinds of external system through two separate registries. A **venue** (`BrokerAdapter`, `brokers/__init__.py:180`) is something Keel places orders through: the engine decides and sizes, the adapter only transmits. A **host** (`StrategyHost`, `brokers/strategy_host.py:127`) runs its own bots; Keel only starts, stops and reads them. The two registries are deliberately kept apart so a host can never be handed to code that would try to place an order through it (`brokers/strategy_host.py:160-166`).

### Venues

Kind strings below are the exact first argument to `register()` in each file.

| Kind | Covers | Client-order-id idempotency | Caveats for a real-money operator |
|---|---|---|---|
| `ccxt` (`ccxt_venue.py:325`) | Binance plus ~100 other CCXT exchanges through one adapter (`ccxt_venue.py:1-2,45-47`). Spot by default; `market_type` selectable (`:43`). Asset class reported as crypto (`:148`). | **Native** on the ten venues in `_TRUSTED_CLIENT_ID`: binance, binanceusdm, binancecoinm, bybit, okx, kucoin, gate, bitget, kraken, coinbase (`:29-30`). **Probe-verified** on every other exchange — `clientOrderId` is still sent (`:246`) but not trusted; `_find_by_client_id` runs before placing and the adapter refuses on an INDETERMINATE probe (`:231-244`). After a retryable transport error *every* venue probes before concluding, and only a venue-confirmed absence is called retryable (`:257-280`). | Sandbox exists but is OFF by default — production is the default path (`:60-62`). Prices are polled ticker snapshots; no WebSocket (ccxt.pro is not used) (`:306-308`). `positions()` is empty on spot venues lacking `fetchPositions` (`:168-169`). |
| `robinhood` (`robinhood.py:629`) | Robinhood **Crypto** only (`robinhood.py:1,119-120`). | **Probe-verified.** `client_order_id` is required and must be a UUID (`:16-17,423-431`); documented as an idempotency key, but the vendor never states what a duplicate does, so the adapter probes before submitting and refuses on INDETERMINATE (`:19-25,436-444`). There is no `client_order_id` filter on `GET /orders/`, so the probe narrows by symbol + `created_at_start` and returns INDETERMINATE — never ABSENT — if pagination is not exhausted (`:26-37,383-413`). | **No sandbox, paper, or test environment exists — the first order this adapter ever sends is real money** (`:39-41`); ships read-only until armed (`:41,128`). **No WebSocket**; `stream_prices` polls (`:43-45,486-491`). `stop_loss`/`take_profit` and `reduce_only` are rejected loudly, never silently dropped — Robinhood models protection as separate order types (`:559-577`). v2 (fee-tier) is default, v1 selectable (`:91-93,129`). |
| `3commas` (`threecommas.py:201`) | 3Commas wired as an **order router / execution destination**: the engine decides, 3Commas transmits to the underlying exchange via smart_trades (`threecommas.py:1-20`). | **Probe-verified.** The platform has no `clientOrderId`, so the key is written into the `note` field and the adapter scans existing smart trades for it before placing (`:160-167`). | The idempotency scan reads a **single page** (`status:all, per_page:100`, no pagination) (`:162-163`) — the same one-page pattern `robinhood.py:33-34` names as the "3Commas defect" that can miss an order and double-submit. `symbol_meta` refuses: 3Commas exposes no usable instrument filters, so size against the underlying exchange adapter (`:106-113`). `manages_positions` one-owner guard: if 3Commas' own bots also trade the account, the engine will not open positions there (`:45-47,152-156`). `stream_prices` is empty — prices come from the exchange, not the router (`:197-198`). |
| `webull` — **NOT registered / DISARMED** (`webull.py:944-971`; the `register("webull", …)` call is commented out at `:970`) | As written: US equities, options, futures, crypto (`webull.py:1`). Written from published docs, **never run against a live account** (`:3-9`). | Intended three-state probe, but the adapter is disarmed because `place_order` is still not idempotent (defect W1) (`:949-954`). | `venues.build()` cannot construct it (`:964-965`). Disarmed for three live defects: W1 (not idempotent), W4 (`cancel()` confirms cancels that did not happen), and `reduce_only` waiving the short-sale safety gate then being dropped on the wire (`:948-975`; `WEBULL_DISARMED_REASON` at `:972-975`). Re-arming requires fixing all three and an independent re-review before the `register()` call is restored. |

### Hosts

Kind strings below are the exact first argument to `register_host()` in each file.

| Kind | Bot control exposed | `stop_disposition` | How "see but never manage" is enforced |
|---|---|---|---|
| `3commas-bots` (`threecommas_hosts.py:562`) | Read + **start/stop** (enable/disable) for DCA bots and grid bots. Ids are namespaced `dca:<n>` / `grid:<n>`; a bare number is refused as ambiguous (`:32-58,84-87,443-449,498-510`). | `STOP_ORPHANS_POSITIONS` (`:71-82,148`). Disabling halts new deals; deals already open keep running under 3Commas' own management until they close. | `BOTS_WRITE` — the scope start/stop needs — is the *same* scope that authorizes `panic_sell_all_deals`, `cancel_all_deals`, deal editing and bot create/update/delete (`:60-69`). The platform cannot narrow it, so a hardcoded 10-endpoint `_ALLOWED` frozenset is checked before any network I/O (`:109-120,189-193`). |
| `cryptohopper` (`cryptohopper.py:572`) | Read + **start/stop**, where start/stop is an `enabled` **field write**, not a dedicated verb (`:30-46,438-506`). Two mutually contradictory official API contracts; `api_contract` config selects one (default `openapi`, alt `legacy`) and the adapter never auto-switches on a 401/404 (`:3-28,177-184`). | `STOP_UNKNOWN` (`:47-53,164`). Neither source states what disabling does to open positions, so callers treat a stopped hopper's book as orphans. | The `manage` scope also grants `/hopper/delete`, `/hopper/create`, every config write, and `/hopper/buy`,`/sell`,`/panic`; scopes cannot express "flip `enabled` only" (`:30-46`). A **per-contract** hardcoded allowlist frozenset gates every call before I/O, and path arguments that could reshape the URL are refused (`:123-128,145-150,239-262`). P&L is not reported anywhere, so `BotState` carries `None`, never 0 (`:58-64`). |
| `altrady` (`altrady.py:507`) | Read (one positions endpoint) + **start/stop** via `/v2/signal_bot/start_stop`. Credentials are **per-bot**, and Keel's config `bots` list *is* the directory — no list-bots endpoint exists on the platform (`:1-24,433-469`). | `STOP_ORPHANS_POSITIONS` (`:51-58,125`). Documented verbatim: stop makes the bot stop accepting new signals and "Any open positions will remain open." | No credential scopes exist at all: the same `api_key`/`api_secret` that reads P&L will open/close/reverse positions if POSTed to the same path, one verb apart (`:26-38`). A 2-entry `_ALLOWED` frozenset is the only wall, checked before I/O (`:107-110,195-200`). Secrets ride in the **query string** (documented landmine) and are scrubbed from every string that could escape (`:39-49,173-261`). "Is the bot running?" is unqueryable — reported as last-Keel-command, presumed live before any command (`:60-68,275-290`). |

**No host platform can scope a credential to read + start/stop only.** Each vendor's control scope also grants order placement, position management, or bot deletion. Keel therefore does not rely on the vendor's scoping: every host routes all traffic through one private `_request` method that checks a hardcoded `(METHOD, path)` allowlist *before any network I/O* — 3Commas bots (`threecommas_hosts.py:64-66,189-193`), Cryptohopper (`cryptohopper.py:234,239-245`), Altrady (`altrady.py:26-38,195-200`). The allowlist is the actual enforcement boundary; the vendor scope is not.

### Two things that are deliberately absent

- **Bitsgap is not a host.** It is named as a strategy platform in the module overview (`strategy_host.py:5`) but there is no adapter and no `register_host("bitsgap", …)`. The reason is grounded in code: its current official API (`open.bitsgap.com`, topic-based) "has no bot concept at all — no list, start, stop or bot-state — so there is nothing to build. See docs: it is not a documentation gap" (`strategy_host.py:217-219`).
- **Robinhood equities is not built.** The only Robinhood adapter shipped is Robinhood Crypto (`robinhood.py:1`); there is no equities adapter file, no equities code path, and no `register()` for one anywhere in `brokers/`. Note on grounding: the code does **not** state a rationale for the omission, so the commonly-cited "the equities REST API has no client-order-id idempotency key" reason is not something confirmable from the shipped source. What is verifiable is only the absence: an operator gets crypto through this venue and nothing else.

### How credentials are stored

Credentials for both venues and hosts live in the **runtime DB**, entered through the dashboard — never in source, `config.yaml`, or a committed env file (`venues.py:1-6`; `brokers/__init__.py:8-9`; `hosts.py:1-8`).

- **Secret fields.** Venues mask `api_key`, `api_secret`, `password` (`venues.py:28`). Hosts additionally mask `access_token` and `app_key` (`hosts.py:36-38`).
- **Masked at the edge.** Any config leaving over the API is passed through `redact()`, which replaces each secret with the marker `••••set••••` plus a short fingerprint — the first 6 hex characters of the secret's SHA-256 — so the UI can show *which* key is loaded without exposing it (`venues.py:47-67`; `hosts.py:67-96`). (The `fingerprint` docstring says "last-4"; the code takes `hexdigest()[:6]` — trust the code.) Host redaction also walks the nested per-bot `bots` list so Altrady's per-bot pairs are masked too (`hosts.py:84-92`).
- **Full config never crosses HTTP.** `get()` returns the unmasked config and is explicitly marked "Never return this over HTTP"; only `list_venues()` / `list_hosts()` (redacted) are API-safe (`venues.py:65-72`; `hosts.py:94-101`).
- **Read-only by default.** A new venue or host defaults `read_only=True` (`venues.py:93`; `hosts.py:154`). Arming execution (`set_trading_enabled`) or bot control (`set_control_enabled`) is a separate, explicitly logged act (`venues.py:114-125`; `hosts.py:175-187`).
- **Re-save without retyping.** On update, a blank or masked secret keeps the stored value, so the dashboard can re-save a venue/host without the operator re-entering keys (`venues.py:75-101`; `hosts.py:104-139`).
- **Adapter-side redaction.** The host adapters also scrub credentials out of any error text, response body, or `repr` before it can reach a log — belt-and-braces because a proxy or chatty server can echo a credential back (`altrady.py:39-49,474-492`; `cryptohopper.py:202-211`; `threecommas_hosts.py:167-179`). Webull redacts its key/secret from error bodies for the same reason (`webull.py:375-383`).

---

## Running Keel

Keel runs as three containers from one image (`Dockerfile:1-2`), all defined in `docker-compose.yml` under the compose project name `keel` (`docker-compose.yml:27`). Build and start with `docker compose build` then `docker compose up -d` (`docker-compose.yml:3-5`).

### The three containers and their ports

| Container | Role | Process | Published port |
|---|---|---|---|
| `keel-engine` | engine + EA/webhook HTTP API + agent | `keel-run-engine.py` → `server.py` (Flask) | `127.0.0.1:8766` → 8766 |
| `keel-dashboard` | control plane (FastAPI) | `keel-run-dashboard.py` → `dashboard_api.py` | `127.0.0.1:8767` → 8767 |
| `keel-newsagent` | news/RSS agent | `news_agent.py` | none (no listener) |

- Engine: container name `keel-engine`, binds `0.0.0.0:8766` inside its namespace (`SLC_HOST`/`SLC_PORT`, `docker-compose.yml:75-76`), published only on host loopback `127.0.0.1:8766` (`docker-compose.yml:82-83`). `server.py` is Flask and hosts `engine_loop` and `agent_loop` as daemon threads in the same process (`docker-compose.yml:63-66`, `server.py:1-9`).
- Dashboard: container name `keel-dashboard` (`docker-compose.yml:120`), entrypoint `keel-supervise.py --role dashboard`, command `keel-run-dashboard.py` (`docker-compose.yml:121-122`). Binds `0.0.0.0:8767` inside its namespace (`KEEL_DASHBOARD_HOST`/`KEEL_DASHBOARD_PORT`, `docker-compose.yml:128-129`), published only on `127.0.0.1:8767` (`docker-compose.yml:130-131`). It is a separate process and container from the engine by design so a wedged dashboard cannot take the engine down (`dashboard_api.py:3-7`, `docker-compose.yml:114-118`).
- News agent: container name `keel-newsagent` (`docker-compose.yml:157`), runs `news_agent.py` (`docker-compose.yml:158`), reaches the engine over HTTP at `http://engine:8766` (`SLC_SERVER_URL`, `docker-compose.yml:161`). It has no listener and no database volume; it does not get `keel_data` (`docker-compose.yml:149-163`). It waits for the engine to be healthy before starting (`docker-compose.yml:164-166`).

Every published port is bound to `127.0.0.1`, not `0.0.0.0`; reach them over an SSH tunnel or tailnet (`docker-compose.yml:12-24`). Two of these ports place trades (8766 and 8767), so do not change the mappings to `0.0.0.0`.

### The dashboard control token

Source of truth is `dash_auth.get_token()` (`dash_auth.py:18-34`):
1. the `DASHBOARD_TOKEN` environment variable if set; else
2. a token read from the file `state/dashboard_token`; else
3. a freshly generated `secrets.token_urlsafe(24)`, written to `state/dashboard_token` and chmod'd `0600` (`dash_auth.py:29-33`).

In the containers that file resolves to `/app/trading-bot/state/dashboard_token` on the `keel_state` volume. All three containers mount `keel_state` (`docker-compose.yml:85, 133, 163`), so they share one token. The token is never stored in the repo, `config.yaml`, or the database (`dash_auth.py:3-6`, `dashboard_api.py:11-14`). The news agent reads the same file (`news_agent.py:398`) and sends it as `X-Dashboard-Token` (`news_agent.py:403`); if the token is missing it tells you to check `state/dashboard_token` or set `DASHBOARD_TOKEN` for both processes (`news_agent.py:409-410`).

### Which endpoints require `X-Dashboard-Token`

The token authorizes mutating endpoints only; GET read views are open because the analysis layer never returns credentials, and `/api/settings` masks anything whose key looks like a secret (`dashboard_api.py:9-14`, `380-400`). `require_token` rejects a missing or non-matching header with 401, compared with `secrets.compare_digest` (`dash_auth.py:37-40`).

Token-gated endpoints on the dashboard (8767):
- `POST /api/alerts/{alert_id}/ack` (`dashboard_api.py:333`)
- `POST /api/venues`, `POST /api/venues/{name}/test`, `POST /api/venues/{name}/trading`, `POST /api/venues/{name}/delete` (`dashboard_api.py:431, 441, 447, 459`)
- `POST /api/hosts`, `POST /api/hosts/{name}/test`, `POST /api/hosts/{name}/control`, `POST /api/hosts/{name}/delete` (`dashboard_api.py:493, 503, 509, 521`)
- `POST /api/hosts/{name}/bots/{bot_id}/start`, `POST /api/hosts/{name}/bots/{bot_id}/stop` (`dashboard_api.py:527, 540`)
- `POST /api/params`, `POST /api/toggle`, `POST /api/halt`, `POST /api/resume`, `POST /api/param_changes/ack/{change_id}` (`dashboard_api.py:554, 569, 593, 600, 607`)
- The entire live-trading router mounted under `/api/live` — its `require_token` dependency is set at the router level, so every `/api/live/*` route needs the token, including `GET /api/live/status` (`dashboard_api.py:613-618`, `live_switch.py:41`).

On the engine (8766), `POST /api/commands` also requires the token: it checks `X-Dashboard-Token` with `secrets.compare_digest(get_token())` before doing anything, because it can close a live position and rewrite a live stop and the route is reachable on `0.0.0.0` (`server.py:150-166`). Note: the header comment in `docker-compose.yml:18-21` claims this route "authenticates NOTHING." That comment is stale — the shipped `server.py:163-166` does require the token. Trust the code.

### Adding a venue and arming it (two separate acts)

Adding a venue and enabling live orders on it are deliberately two calls; pasting an API key must never be the same act as arming execution (`dashboard_api.py:449-450`).

1. Add/update the venue: `POST /api/venues` with the venue body (`dashboard_api.py:431-438`). The read view `GET /api/venues` lists available ccxt exchanges and broker kinds to build the body from (`dashboard_api.py:406-421`).
2. Test it: `POST /api/venues/{name}/test` returns the health check (`dashboard_api.py:441-444`).
3. Arm it: `POST /api/venues/{name}/trading` with `{"enabled": true}` calls `venues.set_trading_enabled` (`dashboard_api.py:447-456`). Send `{"enabled": false}` to disarm.

### Adding a strategy host and arming it (two separate acts)

Hosts follow the same shape. A host is not a venue: Keel never places orders through it, it starts/stops the host's own bots and counts their positions as money at risk (`dashboard_api.py:465-469`). Starting someone's grid bot commits real money, so adding credentials and enabling control stay two acts (`dashboard_api.py:511-512`).

1. Add/update the host: `POST /api/hosts` (`dashboard_api.py:493-500`). `GET /api/hosts` lists configured hosts and available host kinds (`dashboard_api.py:470-475`).
2. Test it: `POST /api/hosts/{name}/test` (`dashboard_api.py:503-506`).
3. Arm bot control: `POST /api/hosts/{name}/control` with `{"enabled": true}` → `hosts.set_control_enabled` (`dashboard_api.py:509-518`).
4. Once armed, start/stop individual bots: `POST /api/hosts/{name}/bots/{bot_id}/start` and `.../stop` (`dashboard_api.py:527-550`). A read-only host returns 403 (`dashboard_api.py:534-535`).

### The path to live

This is the operational runbook for going live; for why the switch is shaped this way, see [The two-step live switch](#the-safety-rails-and-why-each-exists) under The safety rails.

`live_switch.py` is the only sanctioned path to `trading_mode = live`; `params_store` rejects `trading_mode` for every origin and the legacy Flask `/api/settings` route refuses the live value (`live_switch.py:1-7`). The switch is code-isolated and mounted under `/api/live` (`dashboard_api.py:613-618`).

Promotion gate (per strategy × asset-class cell). A cell's `gate_open` is true only when all of these pass (`analysis.py:308-347`), computed over closed **paper** trades (`analysis.py:280`):
- `sample_size`: at least 50 closed paper trades — `GATE_MIN_TRADES = 50` (`analysis.py:215, 314-315`).
- `positive_expectancy`: expectancy in R > 0 **and** n ≥ 50, so expectancy over a handful of trades cannot certify the cell (`analysis.py:320-326`).
- `data_trust`: latest integrity verdict is `GROUNDED` and less than 24h old — `DATA_TRUST_MAX_AGE_H = 24` (`analysis.py:233, 329-338`).
- `rails_exercised`: at least one risk rail (`loss_limit`, `exposure`, or `concurrency`) has demonstrably fired in paper (`analysis.py:339-342`).
- `manual_signoff`: a human sign-off row exists for the cell and has not been voided by a later behavioural parameter change (`analysis.py:343`, `291-306`). Record it with `POST /api/live/signoff` (`strategy`, `asset_class`, `signed_by`), which does not flip anything (`live_switch.py:80-106`).

Inspect the gate read-only at `GET /api/studies/promotion` (`dashboard_api.py:255-257`) or `GET /api/live/status` (`live_switch.py:70-77`).

The two-step live switch (both steps token-gated by the router dependency):
- Step 1 — `POST /api/live/request`. Refused if a blocker is active (DB integrity suspect, or `halt_new_entries` set — resume first) or if no promotion cell has an open gate. On success it returns a one-time `confirm_token`, the phrase `GO LIVE`, and a 60-second TTL (`live_switch.py:43-44, 109-134, 61-67`).
- Step 2 — `POST /api/live/confirm` with `{token, phrase}` within 60s, phrase typed back exactly as `GO LIVE`. It re-validates blockers and open cells at confirm time before flipping `trading_mode` to `live` (`live_switch.py:137-167`).

Even after the flip, the EA's `AllowTradeExecution` input (default false, EA-side) is the independent second half of the double gate; this switch cannot place a live order by itself (`live_switch.py:21-23, 162-167`). De-escalation is one call and is never promotion-gated: `POST /api/live/paper` (`live_switch.py:170-177`).

---

## Document map and what this supersedes

This is the canonical platform reference. It supersedes the architecture and deployment documents written before the multi-venue, strategy-host, and dashboard work landed. Nothing below is deleted — the older documents are kept for history. Where a document is superseded, read this file instead.

Three facts to hold before reading any of the older docs:

- **The three `ARCHITECTURE*.md` files are successive versions of the same design argument, all dated 2026-08-08, all written before the venue/host code existed.** `ARCHITECTURE-V2.md:2` states it "Supersedes `docs/ARCHITECTURE.md`"; `ARCHITECTURE-V3.md:3` states it "Supersedes the deployment sections of ARCHITECTURE-V2; retires `docs/LIVE-EXECUTION-VPS.md`." They describe the system when it was one strategy (SLC), one venue (MT5), and zero closed trades (`ARCHITECTURE-V2.md:4-6`).
- **None of the three mention Robinhood, the strategy hosts (Cryptohopper / Bitsgap / Altrady / 3Commas), the three-state venue Probe, or the rebuilt dashboard (Hosts view).** Verified by search: zero hits for any of those terms in `ARCHITECTURE.md`; in `ARCHITECTURE-V2.md` the only near-match is "three-state checks" at `:1024`, which is the promotion-gate screen, not the venue Probe; in `ARCHITECTURE-V3.md` every "probe" is `keel-health-probe.ps1`, the Windows watchdog, not the venue reachability Probe.
- **Two canonical docs referenced by the repo do not exist in the tree.** `CONTRIBUTING.md:5-6` points readers to `docs/ARCHITECTURE.md` as "target design" and to `docs/STATUS.md` for current state; `STATUS.md` is absent. `PLATFORM.md` (this document) did not exist when those pointers were written. Treat `CONTRIBUTING.md`'s and `README.md`'s doc pointers as stale even though their standards are current.

### `docs/` files

| File (date) | Status | Note |
|---|---|---|
| `ARCHITECTURE.md` (2026-08-08) | SUPERSEDED | Original "target state." `ARCHITECTURE-V2.md:2` calls it "a good design for a problem this system does not yet have." Read V2/V3 and this doc instead. |
| `ARCHITECTURE-V2.md` (2026-08-08) | SUPERSEDED | "The decisive version," grounded in the 2026-08-08 code (one strategy, one venue, zero trades, `AllowTradeExecution=false`). Its risk-invariant analysis is still worth reading for history; its deployment sections are retired by V3. |
| `ARCHITECTURE-V3.md` (2026-08-08) | SUPERSEDED | Freshest of the three and the source of the shipped deployment topology (fleet of single-tenant Windows nodes, read-only console). Still predates the venue/host/Probe/dashboard work. Its verified-defect table (`:13-27`) remains a useful ground-truth snapshot. |
| `CONSOLIDATION-AND-KIT.md` (2026-08-08) | SUPERSEDED | Self-labeled "Historical planning note" (`:3`); the consolidation it plans has already happened. It even flags its own figures as outdated (min RR / pair count). History only. |
| `DEPLOYMENT-LINUX.md` (2026-08-12) | CURRENT | Newest doc. Containers-on-one-Linux-host deployment path; retires MT5 as the *primary* path but keeps the adapter/EA in-tree (`:38-40`). Explicitly touches no risk rail (`:25-27`). Deployment runbook, not architecture. |
| `DEPLOYMENT-WINDOWS.md` (2026-08-08) | CURRENT | Windows Scheduled-Tasks autostart runbook for `scripts/windows/keel-services.ps1`. `ARCHITECTURE-V3.md:20` names it "the runbook" for the shipped Windows node. This is the live path for the paper clock host. |
| `LIVE-EXECUTION-VPS.md` (2026-08-08) | STALE | Explicitly retired by `ARCHITECTURE-V3.md:3`. Describes a Mac-always-on server with a Windows VPS for MT5 — a topology the fleet-of-nodes decision replaced. Do not follow it. |
| `MULTI-ASSET-ARCHITECTURE.md` (2026-08-08; body dated 2026-07-05) | SUPERSEDED | "PROPOSAL — awaiting sign-off" (`:3`). Inventories two separate bots (SLC + a legacy FastAPI pattern bot) before consolidation. Historical proposal; the single-repo, single-engine outcome is what shipped. |
| `PLATFORM-REQUIREMENTS-ANALYSIS.md` (2026-08-08) | CURRENT | Venue-landscape reference, still pointed to by `README.md` and `CONTRIBUTING.md:5`. Correctly scopes Robinhood as crypto-only and TradingView as webhook-only (`:24-25`). It is the requirements analysis, not the as-built adapter guide; its own header (`:2-3`) says re-verify venue API facts before committing time. |

### Notable root files

| File (date) | Status | Note |
|---|---|---|
| `README.md` (2026-08-08) | CURRENT | Accurate platform framing (Keel = platform, SLC = strategy #1, MT5 = venue #1; paper mode, no data flowing). Its inline doc pointers predate this file. |
| `CONTRIBUTING.md` (2026-08-08) | CURRENT (stale pointers) | Standards and the "one engine, sole choke point" rule are current. But `:5-6` point to the superseded `ARCHITECTURE.md` as "target design" and to a nonexistent `docs/STATUS.md`. |
| `SECURITY.md` (2026-08-08) | CURRENT | Secrets-handling policy: credentials live only in the gitignored runtime DB/logs, re-entered via the dashboard. Consistent with the shipped config. |
| `SETUP-GUIDE.md` (2026-08-08) | CURRENT | Clone-to-connected-venue walkthrough (Path A MT5, Path B exchange), consistent with the current process layout (`server.py` :8766, `dashboard_api.py` :8767, `news_agent.py`). |