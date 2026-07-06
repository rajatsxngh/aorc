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
  path (`set_board_column` / `get_board_column`) against a real configured
  project.
- `DockerContainerRuntime`: `start` a container from a base image + `teardown`,
  against a real Docker daemon.
- `ClaudeLLMClient` and `OpenAICompatibleLLMClient`: one real `complete()` call
  each (the OpenAI adapter also via a local `base_url` runtime).

Tests skip cleanly (not fail) when the SDK extra, credentials, or daemon are
absent, so the zero-dep unit suite is unaffected. CI runs them in a separate
job where those are provisioned.

## Acceptance criteria

- [ ] Integration tests live in a separate path/marker from the unit suite and
      do not run in the zero-dep unit run
- [ ] Each of the four adapters has at least one test hitting its real call path
- [ ] `SdkGitHubClient` Projects v2 board read/write covered against a real project
- [ ] Tests skip (not error) when the SDK extra / credentials / daemon are missing
- [ ] CI job provisions the extras + secrets and runs the integration suite

## Blocked by

- S1 (the adapters exist). Board coverage assumes the Projects v2 path shipped
  in the board-column fix.
