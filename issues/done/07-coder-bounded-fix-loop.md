# S7 — Coder bounded fix loop

## What to build

The implementation stage. A coder agent writes minimal code to pass the tester's tests, driven by the spec rather than its own assumptions.

- Coder input: design doc + `task_list` + failing-test output as **pass/fail + error messages only** (never test source, so it cannot game the tests by exploiting test internals) + the repo files it must modify. It executes the `task_list` in order.
- Runs `setup`, `test`, and `lint` from `.aorc.yml` — the project's real toolchain, never guessed.
- **Tests are locked during the fix loop.** Once written and committed, unit tests (and smoke tests) are read-only; the fix-loop container mounts test files read-only. The loop may modify application code only — never tests. Prevents turning a red test green by weakening/deleting/skipping it.
- **Bounded loop:** iterate until green or the escalation cap is reached. (Provider errors are handled separately with backoff in S14 and do not consume the fix-loop counter.) At the cap → hand off to failure handling (`agent-blocked` + summary, detailed in S14).

## Acceptance criteria

- [ ] Coder receives pass/fail + errors only, never test source
- [ ] Executes `task_list` in order
- [ ] Runs setup/test/lint from `.aorc.yml`
- [ ] Test files mounted read-only during the loop; only app code is modified
- [ ] Loop is bounded — terminates on green or at the attempt cap
- [ ] Provider errors do not consume the fix-loop attempt counter

## Blocked by

- S6 (committed failing tests)
