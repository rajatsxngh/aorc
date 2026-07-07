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

## Outcome

Implemented `src/aorc/driver.py` (`PipelineDriver`), wired into
`WakeLoop.dispatch_issue` (an optional `self.driver` attribute, `None` by
default so every existing/hand-assembled loop is unaffected) and into
`__main__.compose()` (built only once `config.setup`/`config.test` are both
present — mirrors `config.build_blockers`'s gate — and attached to the loop
before `run-issue`/`dispatch_issue` can ever call it). `run-issue` needed no
changes: it already calls `dispatch_issue`, which now runs the driver.

- **Sequencing:** `PipelineDriver.run` walks `in-design -> in-test ->
  in-code -> in-review`, one shared `WorktreeManager.ensure(issue)` `cwd`
  for every stage's `TestRunner` call, moving labels via
  `PipelineStateMachine.advance` at each boundary.
- **Idempotency:** per-stage `ArtifactChecker` checks decide skip-vs-run for
  in-design/in-test/in-review. `in-code` is deliberately never skipped on
  resume: `ArtifactChecker.artifact_exists("in-code", ...)` always returns
  `True` by original S2 design (no static artifact for that stage), so
  trusting it here would skip the coder entirely on any resume landing on
  that label — pinned by
  `test_in_code_always_reruns_coder_even_when_resumed_there`. `in-review`
  resumes by re-reviewing an already-open PR via `ReviewerStage`'s existing
  `pr=` parameter (S17's stale-PR seam) rather than opening a second one.
- **Failure routing:** any stage's `agent-blocked` labels
  `guards.BLOCKED_LABEL` + the derived column — the same mechanism every
  other guard/stage in this codebase already uses, no new failure channel.
  Nothing here ever calls `delete_branch`, so branch-preservation falls out
  of simply not touching branches (the driver doesn't own a
  `ContainerHandle` to tear down; that stays the dispatcher's job).
- **Split-brain fix, chosen approach:** neither of the ticket's two literal
  options, exactly — a variant of option 1 that avoids needing a real git
  fetch. `harness.write_worktree_file(cwd, path, content)` mirrors every
  `CoderStage`/`TesterStage` commit straight onto disk at `cwd`, using the
  exact bytes already in hand at the commit call site, immediately before
  the next `TestRunner.run`. A real `git fetch`/`reset` was rejected because
  `GitHubClient` has no such surface (it's a content API) and a fetch would
  only work against a real remote anyway — not `MockGitHubClient`'s
  in-memory store, which the entire zero-dep unit suite depends on. Pinned
  by `tests/test_driver.py::test_real_worktree_and_subprocess_toolchain_see_coders_committed_code`
  (real git worktree + real `SubprocessTestRunner` + real Python subprocess
  actually importing the coder's freshly-committed file) and by ordering
  tests in `test_coder.py`/`test_tester.py` using a `TestRunner` that reads
  the file's content at the moment it runs.
- **Safety fix caught mid-implementation:** the first version of
  `write_worktree_file` wrote unconditionally, and every existing
  `CoderStage`/`TesterStage` test that doesn't pass `cwd` explicitly (most
  of them) defaults to `cwd="."` — which resolved to the actual pytest
  process's working directory (this repo checkout) and started writing
  stray files (`a.py`, `src/aorc/add.py`, etc.) into the real tree on every
  test run. Caught by `git status` before committing; fixed by having
  `write_worktree_file` no-op on `cwd == "."` (the stages' own sentinel
  default for "no real worktree given") and cleaned up the stray files.
- **LLM wiring for the four stages (a decision this ticket had to make,
  config only defines `primary`/`escalation`):** `primary` (already
  `BackoffLLMClient`-wrapped) drives design/tester/coder/reviewer's main
  calls; `escalation`, if configured, is built as a second real client and
  used for the tester's critic and the reviewer's LLM (the "distinct
  instance" S6/S8 call for); falls back to reusing `primary` when no
  `escalation` slot is configured. `EscalationLadder` (primary xN ->
  escalation xM retry-with-backoff) is NOT wired here — it's a pre-existing
  orphan class with no wiring anywhere in the repo, and wiring it is a
  larger, separate concern than this ticket's driver/split-brain scope.
- **Known gap surfaced, not fixed here:** `ReviewerStage`'s `smoke_command`
  template parameter has no corresponding field in `.aorc.yml`'s schema
  (`config.py` only parses the `smoke:` examples list, matching the PRD and
  `install.py`'s config-PR template) — live composition passes
  `smoke_examples=config.smoke` but `smoke_command=None`, which correctly
  skips the smoke gate exactly as `install.py` already documents for a
  missing/incomplete smoke config. Adding that field is a `config.py`
  schema gap, out of scope here.
- Honesty: all of the above is unit-tested against
  `MockGitHubClient`/`MockLLMClient`/`MockTestRunner`, plus the one
  real-git + real-subprocess test named above. No real container, real
  GitHub API, or real LLM provider was exercised this iteration.

Files changed: `src/aorc/driver.py` (new), `src/aorc/harness.py`
(`write_worktree_file`), `src/aorc/coder.py` + `src/aorc/tester.py` (call
it at the exact split-brain site), `src/aorc/wake.py` (`WakeLoop.driver` +
`dispatch_issue` wiring), `src/aorc/__main__.py` (`compose()` builds +
attaches the driver), `tests/test_driver.py` (new), `tests/test_coder.py` +
`tests/test_tester.py` (split-brain ordering pins), `tests/test_wake.py` +
`tests/test_main.py` (wiring tests).

Tests: 410 passed (393 prior + 17 new), 9 deselected (integration,
unchanged). Next: S23 (real token minter), S24 (webhook receiver), S25
(Actions execution) are all now unblocked.
