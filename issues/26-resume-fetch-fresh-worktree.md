# S26 — Fetch remote branch on resume from a fresh worktree

The S22 split-brain fix mirrors every `GitHubClient.commit_file` write into
the local worktree at the commit call site (`harness.write_worktree_file`),
which keeps the toolchain's view in sync **within a single orchestrator
process on a single machine**. But every commit still travels only through
the GitHub contents API — nothing ever runs `git fetch`. `WorktreeManager.
ensure` rebuilds a missing worktree from the **local clone's** branch state,
so on resume from a fresh or deleted worktree, everything committed via the
API since the local clone last saw the branch simply isn't there:

- the generated test file (`in-test`'s artifact) — and the driver *skips*
  `TesterStage` on resume because the marker exists API-side, so nothing
  ever re-mirrors it to disk. The coder's toolchain then runs **without the
  tests it is supposed to satisfy** and can go green vacuously;
- prior coder output from earlier attempts on the same issue;
- an advanced remote default branch (e.g. the merged `.aorc.yml` config PR),
  so the worktree is cut from a stale base.

Trigger conditions today: cross-machine resume (orchestrator restarted on a
different host than the one that made the API commits) or deletion of the
per-issue worktree directory. Same-process/same-machine resume is unaffected
— the worktree dir persists and the mirrored files survive, which is why the
whole S22 suite passes without a fetch.

## What to build

On resume — before any stage runs against the worktree — bring the worktree
up to date with the remote branch:

1. In `WorktreeManager.ensure`, when the branch already exists remotely (or
   the worktree is being (re)created), `git fetch` the remote and reset the
   worktree's branch to `origin/aorc/issue-<n>` before returning the path.
   Base the worktree on the fetched remote default branch, not the local
   clone's possibly-stale HEAD.
2. Keep the S22 mirror-on-commit path exactly as is — fetch-on-resume and
   mirror-on-commit are complementary (one heals cold starts, the other
   keeps a hot worktree in sync without remote round-trips).
3. `MockGitHubClient` has no remote to fetch from — the real-git behavior
   needs its own test in `tests/test_gitops.py` / `tests/test_driver.py`
   style: a throwaway "remote" repo, commits landed on the remote only, then
   `ensure()` on a machine-B clone must materialize them in the worktree.

## Acceptance criteria

- [ ] Real-git test: commit a file to the remote branch only (not the local
      clone), delete/never-create the worktree, `ensure()` — the file is
      present in the returned worktree path
- [ ] Real-git test: driver resume at `in-code` with a fresh worktree runs
      the toolchain with the previously committed generated test file on
      disk (the vacuous-green path above is closed)
- [ ] Fresh worktrees are cut from the fetched remote base, not the local
      clone's stale HEAD
- [ ] S22 mirror-on-commit ordering tests remain green and unchanged
- [ ] No orchestrator-core module gains a git/SDK dependency (invariant #1
      — this stays inside `harness.py`'s existing subprocess-git surface)

## Blocked by

Nothing — independent of S23–S25.
