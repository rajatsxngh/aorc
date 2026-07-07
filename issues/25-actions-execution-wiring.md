# S25 — Real GitHub Actions execution wiring

The readiness review found the project's entire GitHub Actions surface is
declared-but-unexecuted: the generated `aorc-rollback.yml` (S17/S18) is a
YAML string that has never run on real Actions (S19 skipped even the
actionlint smoke check), and the "Docker/Actions" `ContainerRuntime` seam
promised by the S4 harness has only a Docker implementation. This ticket
makes the Actions half real.

## What to build

**1. Prove the rollback loop live.** On a sandbox repo with a real merged-in
`.aorc.yml` and the generated workflow:

- Static gate first: `actionlint` (or GitHub's workflow-parse API) accepts
  the generated `ROLLBACK_WORKFLOW` — added to CI so template edits can't
  silently break the YAML again.
- Live exercise: merge a PR that reddens main → the workflow runs the
  repo's own `setup`/`test` from `.aorc.yml`, reverts the offending merge,
  pushes, and fires the `aorc-main-broken` `repository_dispatch` → the S24
  receiver routes it to `MergeTimeHandler.on_main_broken`, which labels and
  parks the offending issue per S17.
- Known-fragile pieces to verify or fix while live: the PR-number extraction
  (`git log … grep '#[0-9]+'` assumes merge-commit subjects — squash merges
  differ), the `git revert -m 1 || git revert` fallback, and whether the
  default `github.token` push even triggers protected-branch rules on the
  sandbox.

**2. `ActionsContainerRuntime` — the second `ContainerRuntime`.** An
implementation of the existing seam that dispatches a per-issue build as a
GitHub Actions run instead of a local Docker container:

- `start` fires a `workflow_dispatch` (via the `GitHubClient`-adjacent
  adapter surface, App token minted by S23 — the App manifest already
  requests `actions: write`) with the issue number/branch as inputs, and
  returns a `ContainerHandle` identifying the run.
- `teardown` cancels the run.
- The per-issue env (S15 broker output) must reach the job without widening
  the credential surface: passed as encrypted repo/environment secrets or
  minted inside the job — decided and recorded here; secrets never appear in
  workflow inputs (which are visible in run logs/API).
- Selection between Docker and Actions runtimes is an `.aorc.yml` concern
  wired at the S21 composition root.

Out of scope: moving the S22 driver itself into the Actions job (v1 keeps the
driver orchestrator-side; this runtime gives it a remote place to execute
when local Docker is not it).

## Acceptance criteria

- [ ] `actionlint` (or equivalent parse check) on the generated rollback
      workflow runs in CI and passes
- [ ] Live sandbox run recorded: red main → auto-revert → main green again →
      `repository_dispatch` received and routed to `on_main_broken`; PR-number
      extraction verified against the sandbox's actual merge method (fixed if
      it doesn't survive squash merges)
- [ ] `ActionsContainerRuntime` implements the `ContainerRuntime` ABC; unit
      tests pin the dispatch/cancel request shapes against a mock transport
- [ ] No secret ever rides in `workflow_dispatch` inputs, argv, or logs; the
      chosen secret-delivery path is documented and leak-checked the same way
      `DockerContainerRuntime`'s env-file path is
- [ ] Runtime choice comes from `.aorc.yml` (invariant #2), composed only at
      the S21 root (invariant #1 — orchestrator core stays SDK-free)
- [ ] Credential-gated integration test (S19 pattern) dispatches and cancels
      one real workflow run, skipping cleanly without credentials

## Blocked by

- S21 + S22 (a startable AORC with a real pipeline is what Actions executes),
  S23 (App token with `actions: write` for `workflow_dispatch`), and S24
  (the `repository_dispatch` round-trip needs a receiver to land on).

## Progress notes

Done this iteration:

- `ActionsContainerRuntime` (`src/aorc/github/actions_runtime.py`, new): the
  second `ContainerRuntime`. `start` seals each per-issue env value
  (libsodium sealed-box, lazily-imported PyNaCl -- same lazy-import
  discipline as `app_token.py`'s PyJWT) against the repo's Actions public
  key and writes it as a repo secret (`AORC_ISSUE_<n>_<KEY>`), fires
  `workflow_dispatch`, then resolves the run id via the runs-list API
  (`workflow_dispatch` itself returns no body). `teardown` cancels the run
  and deletes the secrets it wrote. Unit-tested against a fake
  transport/seal (`tests/test_actions_runtime.py`, 10 tests) -- pins the
  exact dispatch/secret-write/cancel/delete request shapes and asserts the
  plaintext env value never appears in any dispatch/inputs/query body.
- `.aorc.yml` gains a `container:` block (`config.py`):
  `container.runtime: docker|actions` (default `docker`),
  `container.workflow_file` (required when `runtime: actions`). Parsed and
  validated fail-closed (`tests/test_config.py`, 4 new tests).
- `compose()` (`__main__.py`) builds `ActionsContainerRuntime` when
  configured, minting its own orchestrator-side App token scoped to
  `{"actions": "write"}` -- a second, wider-than-`MINIMAL_PERMISSIONS` mint,
  separate from the per-issue container token, done once at composition
  (`tests/test_main.py`, 4 new wiring tests). `--dev-pat-minter` reuses the
  fixed `GITHUB_TOKEN` PAT for this token too.
- `pyproject.toml`: new `actions = ["PyNaCl>=1.5"]` extra.
- `.github/workflows/ci.yml`: new `actionlint` job renders
  `install.ROLLBACK_WORKFLOW` to a file and runs it through the official
  `docker://rhysd/actionlint:latest` image; `integration` job installs the
  `actions`/`apptoken` extras and gets a new `AORC_IT_GITHUB_WORKFLOW_FILE`
  var for the new integration test below.
- `tests/integration/test_actions_runtime_integration.py` (new,
  credential-gated: `AORC_IT_GITHUB_TOKEN`/`AORC_IT_GITHUB_REPO`/
  `AORC_IT_GITHUB_WORKFLOW_FILE`, `importorskip("nacl")`): dispatches and
  cancels one real workflow run. **Confirmed only that it skips cleanly
  here** -- not run against a real repo/workflow this iteration.
- CLAUDE.md documents the `container:` block, the secret-delivery design
  decision, and the App's extra `Actions: Read & write` permission.

Explicitly NOT done -- the two biggest items in the ticket's "what to build"
section remain open, and this issue is staying in `issues/` (not moved to
`done/`) because of them, not just as an unverified nice-to-have:

- **The live sandbox exercise** (red main → auto-revert → main green →
  `repository_dispatch` → `on_main_broken`) has not been run at all -- no
  sandbox repo, GitHub App, or network access in this environment. The
  fragile pieces the ticket calls out (PR-number extraction assuming
  merge-commit subjects, the `git revert -m 1 || git revert` fallback,
  whether `github.token` pushes trip branch protection) are therefore
  **unverified and unfixed** -- `install.ROLLBACK_WORKFLOW`'s revert step is
  untouched from S17/S18.
- The new `actionlint` CI job has never actually run -- no `actionlint`
  binary or Docker available in this dev sandbox to test it locally, and no
  live GitHub Actions run has been observed to confirm the
  `docker://rhysd/actionlint:latest` step even executes as written. It is
  wired, not proven.
- The new integration test has never run against a real token/repo/workflow
  (same caveat as S23's and S24's integration tests when they were written).

Next iteration (or a human with sandbox access) should: register/use a
throwaway repo with `.aorc.yml` + the generated rollback workflow merged in,
exercise the live loop, fix the PR-number extraction if squash merges break
it, and confirm the `actionlint` job and the new integration test both
actually run and pass in real CI.
