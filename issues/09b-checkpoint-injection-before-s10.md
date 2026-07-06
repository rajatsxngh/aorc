# S20 — Inject Checkpoint into ContainerHarness + give it collision visibility

**BLOCKING S10.** S10 (real collision logic) cannot plug in cleanly until this
is done.

## What to build

S4 shipped the checkpoint spine with the right verdict signature
(`verdict(report) -> "proceed" | "hold"`) but two structural gaps make S10 a
rework rather than a drop-in:

1. `ContainerHarness` constructs its own `Checkpoint()` internally, while every
   other collaborator (runtime, worktrees, github) is injected. So swapping in
   the real collision-aware checkpoint means cracking open the harness
   constructor.
2. The current `Checkpoint` is built with no collaborators, and `CheckpointReport`
   carries only *this* issue's file list. Collision detection needs to compare
   against **other in-flight issues' files** and **open PRs** — neither of which
   the checkpoint can see, and nothing tracks the former at all.

This slice is the plumbing so S10 only has to write the collision rule:

- Inject the `Checkpoint` into `ContainerHarness` (constructor arg), same pattern
  as runtime/worktrees/github. Default to the trivial `Checkpoint` so existing
  behaviour is unchanged.
- Give `Checkpoint` the collaborators collision logic will need: the
  `GitHubClient` (to read open PRs) and a registry of other in-flight issues'
  claimed file lists.
- Add the in-flight file-claim registry: as each dispatched issue reports its
  file list at the checkpoint, record it keyed by issue; expose a lookup of
  "files claimed by *other* in-flight issues." Cleared on teardown.

The verdict stays trivially `proceed` in this slice — S10 fills in the real rule
using the collaborators now available.

## Acceptance criteria

- [ ] `Checkpoint` is injected into `ContainerHarness`, not constructed inside it
- [ ] Default wiring reproduces current behaviour (trivial `proceed`); existing
      harness tests unchanged
- [ ] `Checkpoint` has access to the `GitHubClient` (open PRs) and to other
      in-flight issues' claimed file lists
- [ ] In-flight file-claim registry records per-issue file lists on checkpoint,
      exposes "files claimed by other issues", and clears them on teardown
- [ ] Seam is shaped so S10 adds only the collision rule — no further harness
      surgery required

## Blocked by

- S4 (harness + checkpoint spine)
