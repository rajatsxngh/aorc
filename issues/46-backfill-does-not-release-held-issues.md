# S46 — backfill doesn't release held issues after their collision clears

Live multi-issue testing: issue A merged to main while issue B sat held
(`aorc-held`) because of a collision with A. After A's PR merged, running
`python -m aorc backfill` printed `dispatched=[] held=[] queued=[]` and left
B held — the only way to get B moving again was a manual
`python -m aorc run-issue <B>`.

Cause is visible in the code: `backfill()` (`src/aorc/wake.py:298`) never
sweeps the held queue at all. Its `_already_in_flow` filter
(`src/aorc/wake.py:355`) explicitly skips any issue carrying `HELD_LABEL`,
so backfill neither re-triages nor re-evaluates held issues — it only adds
*new* work. The only caller of `_sweep_held` is `wake()`
(`src/aorc/wake.py:295`). The designed release path is the merge webhook
(`merge.MergeTimeHandler.on_pr_merged` → `loop.wake()`), but with no `serve`
process running (the manual/live-testing loop) nothing ever fires it, and
the command documented as "re-sync" (PRD B7) silently ignores the held
queue. `python -m aorc wake` would probably have released B, but a re-sync
that skips half the re-sync is a trap either way.

## What to build

1. Make `backfill()` run the same held-queue sweep `wake()` runs — simplest
   shape: perform the sweep (post-`_adopt_in_flight_claims`, so the
   collision picture is current) before or after the triage pass, and carry
   the released issue numbers on `BackfillReport`.
2. Print the released list in the `backfill` CLI output alongside
   `dispatched`/`held`/`queued`.
3. While in there, verify `wake()` itself releases in this exact scenario
   (blocker merged + closed, held issue's collision cleared) — if the sweep
   also fails there, the bug is deeper (`is_declared_blocked` or the S43
   claim rebuild) and this ticket's scope widens.

## Acceptance criteria

- [ ] Test: issue held for a collision/blocker; the blocking issue's PR
      merges and the issue closes; `backfill()` releases the held issue
      (removes `HELD_LABEL`, dispatches it) and reports it as released
      (fails before the fix)
- [ ] `python -m aorc backfill` output includes the released issues
- [ ] Existing backfill semantics unchanged for non-held issues
      (triage-only-what's-missing, concurrency cap)
- [ ] Full suite green

## Blocked by

Nothing.
