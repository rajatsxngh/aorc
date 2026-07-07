# S22 — Build-pipeline driver + worktree/API split-brain fix

**UNBLOCKS EVERYTHING LIVE (with S21).** The readiness review found that
`DesignStage`, `TesterStage`, `CoderStage`, and `ReviewerStage` are never
instantiated anywhere in `src/` — orphan classes. `WakeLoop.dispatch_issue`
mints a token and starts a container that runs no command (the base image's
CMD is a detached non-interactive `/bin/bash`, which exits immediately).
Nothing sequences design → tests → code → review for an issue. Triage,
clarification, and decomposition are wired; the build pipeline is not.

## What to build

A per-issue pipeline driver: given one actionable, dispatched issue, run

`DesignStage.run` → `TesterStage.run` → `CoderStage.run` → `ReviewerStage.run`

threading the shared worktree (`WorktreeManager.ensure(issue)`) as the one
`cwd` every stage's `TestRunner` executes in, with:

- label transitions through the existing S2 state machine at each stage
  boundary,
- the S16 idempotency discipline on entry to each stage (`should_run_stage` /
  artifact-presence check — a resumed or double-dispatched issue skips
  completed stages),
- stage failure routing through the existing outcomes (`agent-blocked`
  labeling and branch-preserving teardown), never a new failure channel,
- invoked from S21's `run-issue` subcommand and from `dispatch_issue`'s path
  (v1 decision recorded here: the driver runs orchestrator-side; the
  container remains the isolation boundary of record for a future in-agent
  execution path, per the known open limitation in `issues/README.md`).

### The split-brain bug (MUST fix — not optional)

Today the stages commit files via the GitHub API (`github.commit_file` → the
remote `aorc/issue-<n>` branch) but run setup/test/lint via
`TestRunner.run(cwd, ...)` against the **local worktree** — and nothing pulls
the API commits into that worktree. Concretely, in `CoderStage.run`: each
attempt commits every task's file remotely, then immediately runs the
toolchain against a `cwd` that never received those files. Under
`MockTestRunner` this is invisible; live, tests run against stale code —
the coder can "pass" without its code ever being executed, or fail forever
on code it already fixed.

Required fix, one of (driver's choice, recorded in the ticket close-out):

1. sync the worktree from the remote branch (fetch + reset/pull) after every
   API commit and before every `TestRunner.run`, or
2. write files locally in the worktree and commit/push from there
   (orchestrator-mediated), making the local tree the source of truth.

Either way: **at every stage boundary and every coder fix-loop iteration, the
tree the toolchain runs against contains exactly the commits the pipeline
believes exist.**

## Acceptance criteria

- [ ] Driver sequences the four stages for one issue, one shared worktree
      `cwd` end to end; commands come from `.aorc.yml`, never guessed
- [ ] Stage boundaries move labels via the S2 state machine; artifact-presence
      /`should_run_stage` checks make re-entry idempotent
- [ ] Failure at any stage routes through existing `agent-blocked` /
      branch-preserving teardown paths
- [ ] **Split-brain fix, pinned by test:** a test proves that code written by
      one step is seen by the toolchain run in the next — e.g. coder commits
      file X, and the very next `TestRunner.run` observes X's new content in
      `cwd` (real throwaway git repo à la `tests/test_gitops.py`, plus a unit
      test pinning the sync call ordering: commit → sync → run, per attempt)
- [ ] Reviewer approval ends with the PR open on the real branch
      (`ReviewerStage`'s existing PR-open path), issue labeled `in-review`
- [ ] Whole driver runs green against the in-memory mocks with zero
      third-party deps (unit suite), and is invocable live via S21's
      `run-issue`

## Blocked by

- S21 (the driver needs an entry point and a composition root to be invoked
  from; the stages themselves shipped in S5–S8). This ticket, with S21,
  unblocks S23, S24, and S25.
