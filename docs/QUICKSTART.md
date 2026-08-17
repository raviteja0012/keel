# Quickstart — connecting your first account

This is the operator's walkthrough for taking Keel from "running and idle" to
"accumulating the sample that decides whether it ever goes live." It matches
the shipped code exactly; where a step is deliberately awkward, that is the
design, and the reason is stated.

For the full reference — every rail, every venue, every endpoint — read
[PLATFORM.md](PLATFORM.md). This file is just the path.

---

## 0. What you are about to do, honestly

Adding an account here does **not** start live trading. It gives Keel prices
and a place to *see*. The engine then trades **paper** against real prices,
and the promotion gate requires **50 closed paper trades per strategy × asset
class, with positive expectancy**, before live is even *requestable* — and
live still needs a two-step human switch after that. There is no shortcut in
the code, on purpose. The 50-trade sample is the long pole: start it today and
let it run.

## 1. Make sure the stack is up

```bash
docker compose ps
```

You want three containers, all `(healthy)`: `keel-engine`, `keel-dashboard`,
`keel-newsagent`. If they are not up:

```bash
docker compose up -d
```

Dashboard: **http://127.0.0.1:8767** — loopback only, by design. It is not
reachable from other machines unless you deliberately change the compose file.

> **After every restart, entries are HALTED.** The mode bar will say so. This
> is fail-closed behaviour, not a bug: the engine never assumes that whatever
> stopped it was harmless. Resuming is a human act — Controls → Resume, and it
> asks you to type `RESUME`.

## 2. Paste the control token (once per browser)

Every button that *changes* anything — add venue, arm, halt, resume — needs
the control token. Reads work without it.

```bash
docker compose exec engine cat /app/trading-bot/state/dashboard_token
```

Paste the output into **Controls → Control token**. It is stored in your
browser only. If the file does not exist yet, the engine prints the token on
startup (`docker compose logs engine | grep -i token`).

## 3. Add the account — read-only first, always

Open **Venues**. What you fill in depends on the account:

### A crypto exchange (Coinbase, Kraken, Binance.US, KuCoin, OKX, …)

- **Kind:** `ccxt` — one adapter covers 100+ exchanges; pick yours in the
  dropdown.
- **Where you are matters, not where the code is:** `binance` (binance.com)
  answers HTTP 451 from US addresses and works from India. From the US, use
  `binanceus`, `coinbase`, or `kraken`.
- Create the API key **on the exchange's site** with *read* and *trade*
  permissions and **no withdrawal permission — ever**. Keel never needs it,
  and a key that cannot withdraw is a key whose worst day is smaller.
- Type the key and secret into the form yourself. They go straight into the
  runtime database; they are never in the repo, the image, or any log.

### Robinhood Crypto

- **Kind:** `robinhood`. US customers only; this is their official Crypto
  Trading API.
- It signs requests with an Ed25519 keypair **you** generate. Run:

```bash
docker compose exec engine python -c "import nacl.signing,base64; k=nacl.signing.SigningKey.generate(); print('PRIVATE (paste into Keel):', base64.b64encode(bytes(k)).decode()); print('PUBLIC  (paste into Robinhood):', base64.b64encode(bytes(k.verify_key)).decode())"
```

- Give the **public** key to Robinhood (web app → crypto account settings →
  API credentials); they give you back an API key (`rh-api-…`). The
  **private** key and that API key go into the Keel form.
- Know two quirks before you debug at midnight: requests are only valid for
  **30 seconds** after signing, so a badly skewed clock shows up as an *auth*
  error; and there is **no sandbox** — the first order ever sent is real, which
  is one more reason the promotion gate exists.

### 3Commas (as an order router)

- **Kind:** `3commas`, with an API key scoped `SMART_TRADES` read+write.
- If the connected exchange account has 3Commas **bots** running on it, say so
  in the form (`manages_positions`) — Keel will then refuse to trade it. One
  position, one owner.

## 4. Test before you arm

Press **Test** on the venue row. You want:

- `reachable: yes`, `auth: yes`, and your real balances listed.
- If reachable fails: it is almost always geography (see 451 above) or a typo'd
  key. The error shown is the venue's own answer, redacted.

A venue that fails Test cannot tell Keel prices, and a venue you never arm can
never trade. There is no state where "it silently half-works."

## 5. Arm it — a separate, deliberate act

**Arm** flips the venue out of read-only. It asks you to type `ARM`, and the
act is logged. Until you do this, the adapter will refuse every order at the
door (`VenueReadOnly`), no matter what any other part of the system asks for.

Pasting a key and enabling trading are two different decisions, made at two
different moments. That is intentional.

## 6. What happens next (and what does not)

- The engine starts pulling prices and running strategies against them in
  **paper** mode. Watch **Decisions** — what it *declines* to trade, and why,
  is usually more informative than what it takes.
- **Progress to live** (on Health) fills toward 50 closed trades per cell.
  Come back to it; nothing you click accelerates it.
- If the mode bar says **ENTRIES HALTED**, read the reason in Controls before
  resuming. Something closed that gate.

## 7. Optional: strategy hosts (3Commas bots, Cryptohopper, Altrady)

The **Hosts** tab connects platforms whose *own* bots trade for you. Keel
starts them, stops them, and counts their positions as money at risk — it
never manages them. Same shape as venues: add read-only → Test → arm
*bot control* separately. Their unrealized losses gate Keel's live entries;
their profits never widen anything.

Altrady note: it has no account-level key — each bot carries its own
key/secret pair, so you add bots individually.

## Troubleshooting, briefly

| Symptom | Usual cause |
|---|---|
| Hero says "Cannot see the whole picture" | An API call failed; the page refuses to guess. Reload; check `docker compose logs dashboard`. |
| Venue Test: reachable no | Geography (451), firewall, or the exchange is down. Try from where the bot actually runs. |
| Robinhood: auth errors that come and go | Clock skew vs the 30-second signing window. Sync the host clock. |
| Everything healthy, no trades for hours | Normal. Look at Decisions — standing aside is a decision, and most hours the correct one. |
| Entries halted after restart | By design. Controls → Resume, type `RESUME`, after reading why. |

---

*One sentence to keep: adding a key lets Keel see; arming lets it act; the
50-trade gate decides whether it ever acts with real money. Each of those is a
separate door, and you hold all three keys.*
