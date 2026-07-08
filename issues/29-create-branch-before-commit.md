# S29 — Create the per-issue branch before the first `commit_file` to it

A live end-to-end run (`python -m aorc run-issue` against real GitHub)
surfaced a bug the mock suite could not see: `DesignStage._commit` calls
`github.commit_file(branch="aorc/issue-<n>", ...)` to persist the design
doc, but **nothing ever creates that branch on the remote first**. The
GitHub contents API does not auto-create branches, so the very first commit
of every fresh issue fails with `404 Branch aorc/issue-N not found`
(PyGithub `UnknownObjectException`) and the pipeline dies at its first
stage.

Why every test passed anyway: `MockGitHubClient.commit_file` records the
call and stores the content **regardless of whether the branch exists** —
it is more permissive than real GitHub, so the whole class of
"commit to a branch nobody created" bugs is invisible to the unit suite.
`WorktreeManager.ensure` does create the branch, but only **locally** in
the orchestrator's clone (`git worktree add -b ...`) — nothing pushes it,
and the API commits land on the remote, not the clone.

The same latent bug exists in `install._ensure_config_pr`, which
`commit_file`s the config template to a fresh `CONFIG_PR_BRANCH` that
nothing creates either.

## What to build

1. Add `create_branch(branch, from_ref=None)` to the `GitHubClient`
   interface: create `branch` pointing at `from_ref`'s current HEAD
   (repository default branch when `from_ref` is `None`); **no-op when the
   branch already exists**, so re-dispatch stays idempotent.
   Implement in `SdkGitHubClient` (git-refs API: resolve base sha, create
   `refs/heads/<branch>`, swallow only already-exists), `MockGitHubClient`,
   and the two pass-through wrappers (`ScrubbingGitHubClient`,
   `RateLimitedGitHubClient`).
2. Call it at the start of pipeline dispatch — `PipelineDriver.run`, right
   after `WorktreeManager.ensure` — so `aorc/issue-<n>` exists remotely
   (off the default branch's current HEAD) before any stage's
   `commit_file` targets it. Same one call in `install._ensure_config_pr`
   before the config-PR commits.
3. Harden the mock so this bug class can't hide again:
   `MockGitHubClient` tracks known branches (default branch pre-seeded;
   `create_branch` registers; the `add_file` test helper registers its
   ref; `delete_branch` unregisters) and `commit_file` to a branch that
   doesn't exist raises `UnknownBranchError` — mirroring the real 404.

## Acceptance criteria

- [x] Real-git-remote-style driver test (same throwaway bare-remote setup
      as the S26/S27 tests in `tests/test_driver.py`): dispatching a fresh
      issue commits the design doc successfully because the branch is
      created first — under the hardened mock this fails with
      `UnknownBranchError` before the fix, exactly reproducing the live 404
- [x] `MockGitHubClient.commit_file` to a non-existent branch raises;
      after `create_branch` it succeeds — mock matches real GitHub
- [x] `SdkGitHubClient.create_branch` creates `refs/heads/<branch>` from
      the default branch's HEAD sha and no-ops when the ref already exists
- [x] The config-PR path (`install._ensure_config_pr`) creates
      `CONFIG_PR_BRANCH` before committing to it
- [x] Full suite green with the hardened mock — every test that drives a
      stage directly now declares its branch precondition explicitly

## Blocked by

Nothing — independent of S25's open scope.

## Progress notes

Implemented as specified: `create_branch` added to `interfaces.GitHubClient`
and all four implementers; `PipelineDriver.run` creates `aorc/issue-<n>`
right after `WorktreeManager.ensure` and before any stage runs;
`install._ensure_config_pr` creates `CONFIG_PR_BRANCH` before its two
commits. `MockGitHubClient` now models branch existence (`self.branches`,
default branch `"main"` pre-seeded) and its `commit_file` raises
`UnknownBranchError` on an unknown branch. Unit tests that drive
`DesignStage`/`TesterStage`/`CoderStage` directly (without the driver) were
updated to create the issue branch in setup — the explicit precondition the
mock now enforces. Honesty caveat, same as S23–S25: verified against the
hardened mock and a fake PyGithub repo object, not re-run live against real
GitHub this iteration.
