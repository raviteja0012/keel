# SLC Trading System — Consolidation Plan + GitHub & Project Kit

> **Historical planning note.** This is the original consolidation/GitHub planning doc. The
> consolidation it describes has since happened (single repo, `legacy/` retained for reference,
> secrets removed, `data/`/`state/` gitignored). Some figures here predate the alignment of the
> docs to `config.yaml` — the current shipped defaults are **min RR 2.0** and **8 enabled pairs**
> (see [`../README.md`](../README.md)); the 2.5 / 22-pair values below reflect an earlier live
> deployment's runtime DB, which is not committed.

Covers four things you asked: (1) are the two zips the same, (2) the best way to combine them
and expand with new strategies, (3) exactly what to post on GitHub, (4) a review of the Claude
Project name / description / instructions.

---

## 1. Are the two zips the same? — No (same project, different code)

| | `Pattern-Strategy-Bot_Team-Share` (zip 1) | `SLC-Trading-Bot-team-archive` (zip 2) |
|---|---|---|
| Status | earlier / parallel build | **canonical (current)** |
| Web framework | FastAPI + asyncio | Flask + threads |
| Storage | JSON ledger + `trade_journal/*.json` | SQLite (`data/trading.db`, WAL) |
| Port / EA | 8765 / `MT5DataBridge` | **8766 / `SLCDataBridge` v2.30** |
| Code shape | modular `strategy/` package + `tests/` | flat modules + `agent.py` + TradingView ctx |
| Risk knobs | `min_rr 2.0`, risk lowered 2%→1% | `min_rr 2.5`, 1% per A+ |
| Extra docs | strategy-study corpus + 100+ validation PNGs | LICENSE, SECURITY, ONBOARDING, dev-history |

They share **zero identical files**. The dev-history confirms the bot was rebranded
"SLC Bot → Pattern Strategy" — same strategy, two independent implementations. **Do not
file-merge the two `trading-bot/` trees** (Flask+SQLite vs FastAPI+JSON would break both).

---

## 2. Best way to combine + expand with new strategies

**Yes — one repo / one Claude Project is the right call.** Both are the same system, share the
playbook, the risk rails, the team, and the infra. The catch is *how* you combine: keep
**one engine, many strategies behind a clean interface** — not one giant tangle. This is the
consensus pattern (e.g. freqtrade-style strategy plugins; OpenAlgo runs *"multiple strategies
in parallel with full process isolation"*). The most-cited failure mode is **strategy drift** —
*"the bot starts doing things you didn't design because conditions across strategies interact
unexpectedly. Keep strategies modular… use an orchestration layer to run multiple in parallel."*

### Target architecture (layered)

```
                 ┌─────────────── ENGINE / PLATFORM (shared, strategy-agnostic) ───────────────┐
MT5 ──SLCDataBridge──► data ingest → │ execution/broker · GLOBAL risk rails · storage(SQLite) · │
                                     │ notifier(TG/Discord) · dashboard · scheduler/integrity   │
                                     └──────────────────────────┬───────────────────────────────┘
                                                                │  Strategy interface:  signal(bars)->Signal|None
                       ┌────────────────────────────────────────┼────────────────────────────┐
                  strategies/slc/   (SLC = strategy #1)     strategies/<new-1>/   strategies/<new-2>/  …
```

- **Rails are GLOBAL, not per-strategy.** Risk %, stops (never loosen), daily/weekly kill
  switches, paper/live mode, the bounded self-tuner's whitelist — these live in the engine and
  apply to every strategy. A new strategy can pick setups; it can never change the guardrails.
- **Strategies are isolated.** Each gets its own signal logic, its own config block + enable
  flag, its own journal / shadow stream. The self-tuning agent stays bounded *per strategy* and
  never crosses into another or into the rails/mode.
- **One dashboard, per-strategy tagging** so you can see which strategy fired what.

### Phased plan (low-risk, paper-first)

| Phase | Do | Output |
|---|---|---|
| 0 | Stand up the repo from **zip 2 (SLC)**, scrubbed | already done → `slc-trading-bot_github-ready.zip` |
| 1 | Harvest from zip 1 **only framework-agnostic assets** — `setups_dataset.json` (labeled dataset), the validation-PNG gallery, `strategy_knowledge_base.md`, `parameter_tuning.md`, `PROJECT_CONTEXT_HANDOFF.md` — into `reference/legacy-fastapi/`. **No code.** | reference corpus in-repo |
| 2 | Refactor `strategy.py` to expose a clean **Strategy interface**; register SLC as the first plugin under `strategies/slc/` | engine becomes strategy-agnostic |
| 3 | Add each new strategy as a new module implementing that interface, with its own config + enable flag | strategies plug in without touching the engine |
| 4 | Orchestration: run strategies in parallel with **per-strategy state isolation**, aggregate to one dashboard, per-strategy kill switch | multi-strategy platform |

### The rule every new strategy must clear (quant lens)
Each new strategy passes its **own** go-live gate independently: ≥50 closed paper trades with
positive expectancy, validated out-of-sample, before it's allowed anywhere near live. Don't let
a new strategy inherit SLC's "proven" status — overfitting and regime-specific edges are the
norm, not the exception.

### Monorepo vs multi-repo
**Monorepo (one repo).** Small team, shared engine/rails/dashboard, strategies evolve together —
a monorepo keeps them in lockstep and is exactly what your "all combined is good" instinct wants.
Split a strategy into its own repo only if it ever needs a separate release cadence or owner
(not now).

---

## 3. What to post on GitHub (manual creation)

The repo content is the scrubbed **`slc-trading-bot_github-ready.zip`** (already has README,
LICENSE, SECURITY, a clean `.gitignore`, and an initial commit). Set these in the GitHub UI:

- **Repository name:** `slc-trading-bot`  *(alt if you go full multi-strategy platform later: `proaxive-trading-system`)*
- **Visibility:** **Private — required.** `LICENSE.md` is proprietary (© Proaxive / Shakeeb Ahmed) and the original DB held live creds.
- **Description (About):**
  > SLC (Structure · Liquidity · Confirmation) price-action trading bot for FX, metals & crypto — MT5 data bridge, Flask dashboard, Telegram/Discord alerts, bounded self-tuning + news agents. Paper now, built to go live.
- **Topics:** `algorithmic-trading` `trading-bot` `forex` `metatrader5` `mt5` `price-action` `python` `flask` `sqlite` `telegram-bot` `discord` `trading-strategy` `paper-trading`
- **On the create screen:** Add a README → **No**; .gitignore template → **None**; License → **None** (the repo's own `LICENSE.md` governs). Default branch → `main`.
- **After first push:** Settings → Code security → enable **Secret scanning + Push protection**; add **branch protection** on `main`; invite the team.

**Push it** (the zip is already a git repo with a commit):
```bash
unzip slc-trading-bot_github-ready.zip && cd slc-trading-bot
git remote add origin git@github.com:<you-or-org>/slc-trading-bot.git
git branch -M main && git push -u origin main
```
> Note: GitHub's web "Upload files" commits *files*, not a `.zip` (a zip just lands as a binary).
> Push via git, or extract and drag the **contents**. Rotate the Telegram/Discord creds regardless
> — they were live in the bundle the team already has.

---

## 4. Claude Project — name / description / instructions review

### Name — small fix
Your `Pattern Strategy (SLC) — FX Trading Bot` is fine except **"FX" undersells it** (it trades
FX, metals *and* crypto). Recommended:

> **Pattern Strategy (SLC) — Price-Action Trading Bot**

(If the project becomes a true multi-strategy platform, rename later to `Proaxive Trading System`.)

### Description — accurate; just consolidate
Both paragraphs you wrote are accurate against the playbook and rails — they only repeat each
other. Use this single version:

> We're building the **SLC Price Action Trading Bot** toward live trading. It's a Python + MT5
> system trading FX, metals and crypto on a pure price-action playbook (Structure · Liquidity ·
> Confirmation), currently running in **paper mode**. The objective: develop and harden the
> strategy, validate it forward in paper until it clears the go-live gate (**≥50 closed trades
> with positive expectancy**), then promote to live at small size — keeping the same hard risk
> rails in live (never loosen stops, bounded self-tuning agent, daily −2% / weekly −5% kill
> switches). Use this project to develop the code, operate the bot, review performance, expand it
> with new strategies, and make the go-live call. **North star: `SLC-Price-Action-Playbook.md`.**

### Instructions — your current doc is accurate; add three things
The `SLC Trading Bot — Project Instructions` you're using checks out against the canonical SLC
build (port 8766, `SLCDataBridge` v2.30 magic 770001, Flask/engine/strategy/storage/agent, paper
/ swing / 1% / RR 2.5 / ATR 0.35 / −2% / −5% / vol_mult 1.0 / 22 pairs, secrets in DB+state,
clean `config.yaml`, the 7 rails, open items). Keep all of it. Add these to match where you're
taking it:

1. **A Goal line at the top** (so Claude actively supports the go-live work, not just "stay paper"):
   > *Goal: take this bot live once it clears the gate (≥50 paper trades, positive expectancy). Paper is the proving ground, not the destination — support strategy hardening, finishing live MT5 routing, and go-live readiness. Going live is a deliberate, double-gated step (EA `AllowTradeExecution` + dashboard switch), never a side effect.*

2. **A multi-strategy scope note** (so new strategies are added the right way):
   > *Scope is expanding to multiple strategies. SLC is strategy #1. New strategies are added as isolated modules behind the shared engine and the GLOBAL risk rails — never duplicate or fork the rails per strategy, and never let one strategy's tuning affect another. Each new strategy clears its own ≥50-trade positive-expectancy paper gate before live.*

3. **A canonical-source note:**
   > *The `slc-trading-bot` GitHub repo (the SLC/Flask/SQLite build, port 8766) is the source of truth. The older FastAPI/8765 `Pattern-Strategy` snapshot is superseded — don't merge its code; only its labeled dataset + validation gallery are kept as reference.*

---

*Educational software, not financial advice. Paper until proven; live deliberately, at small size,
behind the rails.*
