# S19 — Integration tests for the four real adapters

## What to build

The unit suite runs entirely against in-memory mocks, so the four real
adapters — `DockerContainerRuntime`, `ClaudeLLMClient`, `OpenAICompatibleLLMClient`,
and `SdkGitHubClient` (incl. its Projects v2 GraphQL board path) — have **zero**
automated coverage. Every one of them could be broken (wrong method name, wrong
argument shape, wrong GraphQL query) and the full unit suite would still pass.
Several commit messages already claim these are "exercised by integration tests";
those tests do not exist. This slice makes that claim true.

Smoke-level integration tests, run **separately** from the unit suite (marker /
separate path, gated on the relevant SDK extra + credentials/daemon being
present), that exercise each adapter's real calls at least once:

- `SdkGitHubClient` (PyGithub): read an issue, add/remove a label, open a PR,
  `get_file` (present + 404→None), `delete_branch`, and the Projects v2 board
  path (`set_board_column` / `get_board_column`, plus S18's `create_board` —
  project creation + status-option update, GraphQL never exercised) against a
  real configured project.
- `DockerContainerRuntime`: `start` a container from a base image + `teardown`,
  against a real Docker daemon.
- `ClaudeLLMClient` and `OpenAICompatibleLLMClient`: one real `complete()` call
  each (the OpenAI adapter also via a local `base_url` runtime).

Tests skip cleanly (not fail) when the SDK extra, credentials, or daemon are
absent, so the zero-dep unit suite is unaffected. CI runs them in a separate
job where those are provisioned.

## Acceptance criteria

- [x] Integration tests live in a separate path/marker from the unit suite and
      do not run in the zero-dep unit run
- [x] Each of the four adapters has at least one test hitting its real call path
- [x] `SdkGitHubClient` Projects v2 board read/write covered against a real project
- [x] Tests skip (not error) when the SDK extra / credentials / daemon are missing
- [x] CI job provisions the extras + secrets and runs the integration suite

## Known limitation inherited from S15/S16 (S18 did not address it)

A container holding `GITHUB_TOKEN` can push or hit the API **directly**,
bypassing the orchestrator-side `ScrubbingGitHubClient` entirely — layer-2
scrubbing only covers orchestrator-mediated writes. S18 landed no real
container plumbing, so this lands here with the real-adapter work: route agent
pushes through an orchestrator-mediated path or a scrubbing egress proxy, and
stop passing secrets as `docker run -e KEY=value` (visible in host `ps`;
`DockerContainerRuntime.start` does this today).

## Notes from S18

- `LocalGitOps` (`gitops.py`) is a real adapter but is already exercised
  against real throwaway git repos by `tests/test_gitops.py` in the unit
  suite (subprocess `git`, no SDK/daemon needed) — it does **not** need a
  separate integration test here.
- The generated `aorc-rollback.yml` workflow and the App-manifest registration
  are declared-but-unexecuted surfaces; if CI provisioning allows, a smoke
  check that the workflow YAML is accepted by `actionlint`/GitHub would fit
  this slice.

## Blocked by

- S1 (the adapters exist). Board coverage assumes the Projects v2 path shipped
  in the board-column fix.

## Outcome (S19 close-out — honesty record)

- `tests/integration/` (marker `integration`, deselected from the unit run via
  pyproject `addopts`) covers all four adapters; `.github/workflows/ci.yml`
  runs unit and integration as separate jobs, integration provisioned from
  repo secrets/vars (model names via vars, never code).
- **Actually executed against a live service in this iteration:**
  `ClaudeLLMClient.complete()` (real Anthropic API call, passed). Everything
  else (`OpenAICompatibleLLMClient` hosted + local, `SdkGitHubClient` incl.
  both Projects v2 board paths, `DockerContainerRuntime`) was verified only
  down its skip path here — no OpenAI key, GitHub token, or Docker daemon on
  this box. Those tests are real code awaiting CI credentials, not proven
  green against live services yet.
- The S14 error contract (real SDK 429/5xx → `ProviderError`) is still
  asserted against mocks only; the smoke suite can't provoke those
  deterministically.
- Inherited S15/S16 limitation, first half **fixed**: `DockerContainerRuntime`
  no longer passes secrets as `docker run -e KEY=value` (host-`ps`-visible);
  env now goes through a 0600 temp `--env-file` removed as soon as `docker
  run` returns (unit-pinned, incl. the failure path).
- Second half **not addressed and now recorded in `issues/README.md` as a
  known open limitation**: a container holding `GITHUB_TOKEN` can still push
  or hit the API directly, bypassing `ScrubbingGitHubClient`. Mediating agent
  pushes (orchestrator-side push from the shared worktree, or a scrubbing
  egress proxy) needs the real in-container agent execution path, which no
  v1 slice builds — there is nothing real to plumb it into yet, and S15's
  audit already established that shelf code nothing composes is a defect,
  not progress.
- The optional actionlint smoke check on the generated rollback workflow was
  not added (the YAML remains parse-tested only, per S18).
