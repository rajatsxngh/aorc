# S47 — stale state labels: pipeline/queue labels not cleaned up on transitions

Live multi-issue testing surfaced two label-hygiene failures:

- A released (formerly held) issue shows **both** `aorc-held` and
  `in-review` at once.
- A merged issue still shows `in-review` after its PR merged and the issue
  closed — no done/terminal state visible on the issue itself.

Code confirms both:

1. `MergeTimeHandler._close_merged_issue` (`src/aorc/merge.py:351`) closes
   the issue, moves the board card to Done, and cleans the branch — but
   never removes the pipeline label the issue is carrying (`in-review`).
2. Only `_sweep_held` (`src/aorc/wake.py:439`) removes `HELD_LABEL` before
   dispatch. Every other dispatch path — `run-issue` from the CLI,
   `dispatch_issue` generally — leaves `aorc-held` in place while the
   driver stacks stage labels (`in-design` → … → `in-review`) on top.

This is worse than cosmetic: `rebuild_state` (`src/aorc/wake.py:157`)
classifies `HELD_LABEL` **first**, so an actively-in-pipeline issue still
wearing a stale `aorc-held` is treated as held on every subsequent wake and
is eligible for the sweep to dispatch again. Closed-but-labeled issues at
least drop out of `list_issues("open")`, so their stale label only misleads
humans reading the repo.

## What to build

1. A single label-transition helper (the state machine already exists in
   `pipeline.py`) that enforces the invariant: an issue carries **at most
   one** AORC state label at a time — entering any state removes the
   previous pipeline/queue label.
2. `dispatch_issue` removes `HELD_LABEL` when dispatching a held issue,
   regardless of which caller (sweep, backfill, run-issue CLI) got it there.
3. Merge-time close removes the pipeline label (Done column on the board is
   the terminal marker; decide whether a terminal label is wanted at all).
4. Audit every `add_label` call site for the same invariant
   (`escalation`, `clarification`, `decomposition`, awaiting-config, S45's
   `BLOCKED_LABEL` path).

## Acceptance criteria

- [ ] Test: held issue dispatched via `dispatch_issue` directly (not the
      sweep) ends up without `aorc-held` (fails before the fix)
- [ ] Test: merged issue ends up with no pipeline label after
      `_close_merged_issue` (fails before the fix)
- [ ] Invariant test: after each transition the state machine exercises, the
      issue carries at most one AORC state label
- [ ] Full suite green

## Blocked by

Nothing, but S46 touches the same sweep/dispatch code — coordinate.
