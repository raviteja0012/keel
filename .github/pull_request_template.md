## What and why

<!-- One or two sentences. Why this change exists, not a restatement of the diff. -->

## Which files, and which invariant

<!-- CLAUDE.md asks that behaviour changes name the file and the invariant checked against.
     Delete this section only if the change touches no runtime code (docs, comments). -->

| File | Change | Invariant checked |
|---|---|---|
|  |  |  |

## Safety checklist

- [ ] No rail moved out of `engine.py` into a strategy or an adapter
- [ ] No risk multiplier that can raise risk (every factor guarded `if factor < 1.0`)
- [ ] No new path to `trading_mode = live` beside `live_switch`
- [ ] Uncertainty fails **closed** (stale data, unreachable venue, suspect DB → stand aside)
- [ ] No secret in source, `config.yaml`, or any committed file
- [ ] `data/` and `state/` still gitignored
- [ ] De-escalation is no harder than escalation

## Tests

- [ ] Full suite green locally (`for t in tests/test_*.py; do python3 "$t"; done`)
- [ ] A fix includes the test that would have caught the bug

**Did an existing test have to change?**

<!-- If yes, say which and why the old expectation was wrong. A test relaxed to let new
     behaviour through is how a rail gets weakened quietly. If no, write "no". -->

## Paper validation

<!-- For anything affecting signals, sizing or execution: what did paper or shadow show?
     "Not yet validated" is an acceptable answer; silently skipping the question is not. -->
