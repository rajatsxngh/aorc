# S8 — Reviewer + coverage + smoke + PR open

## What to build

The final pipeline stage. This slice completes the spine: one actionable issue now goes all the way to a review-approved PR.

- **Reviewer agent** evaluates the branch diff against the design doc **and** the original issue requirements. It runs on a **distinct model slot** from the coder (the escalation-tier slot), so it doesn't share the coder's blind spots. If it finds deviations from the design doc, it blocks the PR — the coder must fix (re-enters S7's fix loop) before the PR is surfaced. Reviewer feedback is posted as PR comments so the human reads its reasoning at final merge.
- **Line-coverage floor — BEFORE merge.** Once tests pass and code exists, run a coverage tool and assert the PR's changed lines meet the floor (default 80%, config).
- **End-to-end smoke test.** Before any merge the full suite **plus** `.aorc.yml`'s `smoke:` examples (input X → output Y) must pass. Smoke verifies real outputs, not merely that the app started; smoke tests are locked during the fix loop like unit tests. If no `smoke:` block: run, skip this gate, and hard-disqualify the repo from auto-merge (full handling in S18).
- On reviewer approval, open the PR automatically.

## Acceptance criteria

- [ ] Reviewer runs on a distinct (escalation-tier) model slot from the coder
- [ ] Reviewer evaluates diff vs design doc + original issue; blocks PR on deviation
- [ ] Reviewer feedback posted as PR comments
- [ ] Line-coverage floor asserted on changed lines before merge (default 80%, config)
- [ ] Full suite + smoke examples must pass before PR is surfaced (skip smoke gate if no `smoke:` block)
- [ ] PR opened automatically on approval

## Blocked by

- S7 (green code)
