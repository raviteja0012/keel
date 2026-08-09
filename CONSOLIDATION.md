# Repository consolidation — two builds, one project

This repo holds **two implementations of the same system** (the SLC price-action bot, once
called "Pattern Strategy"). They share zero source files — same strategy, different code — so
they are **co-located, not file-merged**.

| Location | Build | Status |
|---|---|---|
| repo root (`/`, `trading-bot/`) | **SLC** — Flask + SQLite, port **8766**, EA `SLCDataBridge` v2.30 | **canonical / current** |
| `legacy/pattern-strategy-fastapi/` | Pattern-Strategy — FastAPI + JSON ledger, port 8765, EA `MT5DataBridge` | superseded; kept for reference |

**Rules**
- The canonical bot is the one at the repo root. Run, operate, and develop **that**.
- Do **not** deploy or merge the `legacy/` code. It's here for its reference value only:
  the labeled dataset (`strategy-study/setups_dataset.json`), the validation-PNG gallery,
  `strategy_knowledge_base.md`, `parameter_tuning.md`, and `docs/PROJECT_CONTEXT_HANDOFF.md`.
- Note the builds use **different risk numbers** (SLC ships `min_rr 2.0`, 1%/A+; legacy `min_rr 2.0`,
  2%→1%). When in doubt, the root build's `config.yaml` is authoritative (the runtime DB, which wins
  after first run, is not committed).

**Roadmap:** refactor the root build into a shared engine + the global risk rails, with
strategies as isolated plug-ins (SLC = strategy #1). New strategies plug in behind that
interface; rails stay global. See `docs/` for the consolidation/GitHub/project kit.
