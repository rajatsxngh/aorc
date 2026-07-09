# S48 — backfill-dispatched "winner" issues stall at in-design

Live multi-issue testing: issues dispatched by `python -m aorc backfill`
(the collision "winners" that were supposed to proceed) stall at
`in-design` and never advance; running `python -m aorc run-issue <n>` on the
same issue completes the pipeline.

What's known so far:

- The driver **is** wired for backfill: `compose()` sets `loop.driver`
  unconditionally (`src/aorc/__main__.py:300`), and `backfill()` dispatches
  through the same `dispatch_issue` as `run-issue`.
- Backfill discards every dispatch's `DriverResult`: the dispatch loop
  (`src/aorc/wake.py:325`) ignores `dispatch_issue`'s return value, and the
  CLI prints only issue numbers. S31 added stage/status/reason surfacing to
  `run-issue` **only** — so under backfill a design stage that blocked,
  held, or threw looks identical to a clean run. The stall may have had a
  printed-nowhere reason.
- Root cause of the stall itself is not yet diagnosed. Suspects, in order:
  the S43 post-design checkpoint holding the winner against a claim from
  the same backfill batch (its own or the loser's), a driver result of
  `held`/`blocked` at design being silently swallowed per the previous
  point, or an exception in a later stage aborting the loop between issues.

## What to build

1. Surface per-issue `DriverResult` (stage/status/reason) in
   `BackfillReport` and the `backfill` CLI output — same information
   `run-issue` prints since S31. This alone turns the next repro from a
   mystery into a message.
2. Reproduce the stall in a multi-issue test (two actionable issues, one
   collision, backfill dispatches the winner), diagnose with the new
   reporting, and fix so a backfill-dispatched winner reaches the same
   terminal state `run-issue` reaches.

## Acceptance criteria

- [ ] Test: multi-issue backfill where the collision winner runs the full
      pipeline to the same terminal state as a `run-issue` dispatch (fails
      before the fix, or the diagnosis documents why it can't)
- [ ] `python -m aorc backfill` prints stage/status/reason per dispatched
      issue
- [ ] Full suite green

## Blocked by

Nothing hard; shares diagnosis surface with S46 (both are backfill paths).
