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
