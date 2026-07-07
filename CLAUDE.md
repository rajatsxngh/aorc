# AORC — developer notes

Autonomous Orchestrated Repo Contributor: an unattended GitHub-labels-driven
pipeline that takes an issue to a review-approved PR. See
`AORC-PRD-v1-FINAL-tagged.md` and `issues/` for the full design and the
tracer-bullet slices (S1–S18).

## Architecture invariants (hard rules)

1. Orchestrator logic depends **only** on the `GitHubClient` and `LLMClient`
   interfaces — never on a provider SDK (anthropic/openai) or the GitHub SDK
   directly. Those SDKs live behind adapters.
2. No hardcoded model names or secrets in code. Config comes from `.aorc.yml`.

## Layout

- `src/aorc/` — the package (orchestrator core + interfaces + adapters).
- `tests/` — pytest suite; runs with zero third-party deps against in-memory mocks.

## Environment

Python is provided via [uv](https://github.com/astral-sh/uv). If `python`/`pytest`
are not on PATH in a fresh shell:

```sh
export PATH="$HOME/.local/bin:$PATH"      # uv
uv venv --python 3.12                      # create .venv (once)
uv pip install -e ".[dev]"                 # install pytest etc. (once)
```

## Running tests

```sh
.venv/bin/pytest -q
```

(equivalently `uv run pytest -q`). All tests must pass before committing.

## Running AORC

`src/aorc/__main__.py` (S21) is the composition root and only entry point —
the one module allowed to construct real SDK-backed adapters
(`SdkGitHubClient`, a real `LLMClient`, `DockerContainerRuntime`). Everything
else in `src/aorc/` still depends only on the `GitHubClient`/`LLMClient`
interfaces (invariant #1).

```sh
python -m aorc install       # board + labels + config PR + immediate backfill
python -m aorc backfill      # re-sync: triage every open issue not in the flow
python -m aorc wake          # one cron tick: held-queue sweep + token-expiry pass
python -m aorc run-issue 42  # dispatch a single actionable issue
python -m aorc serve         # webhook receiver (HMAC-verified GitHub deliveries)
```

(also installed as the `aorc` console script via `[project.scripts]`.)

Required environment, read only at the composition root (invariant #2 — no
hardcoded models or secrets):

- `GITHUB_TOKEN` — a GitHub token for `SdkGitHubClient` (the orchestrator's
  own issue/label/PR operations).
- `AORC_GITHUB_APP_ID` + `AORC_GITHUB_APP_PRIVATE_KEY_PATH` — the registered
  GitHub App's ID and a filesystem path to its PEM private key, consumed only
  by `CredentialBroker`'s real minter (`aorc.github.app_token.
  build_app_token_minter`, S23) to mint per-issue, single-repo, short-lived
  container tokens via the App-JWT → installation-token exchange. Pass
  `--dev-pat-minter` to skip both and fall back to a fixed `GITHUB_TOKEN` PAT
  for every container instead (`aorc.__main__.pat_passthrough_minter`, a dev
  escape hatch — never use it for a live run).
- `AORC_REPO` — `owner/repo` (or pass `--repo`).
- `AORC_BASE_IMAGE` — the Docker image `DockerContainerRuntime` starts
  per-issue containers from. Only required when `.aorc.yml`'s
  `container.runtime` is `docker` (the default) or absent.
- Whatever `.aorc.yml`'s `llm:` block references for provider API keys (e.g.
  `$ANTHROPIC_KEY`), expanded by `config.py`.
- `AORC_WEBHOOK_SECRET` — the GitHub App's webhook secret, needed only by
  `python -m aorc serve` (S24). Read once here and handed straight to
  `aorc.webhook.serve`; never logged, never echoed in a response.

### Webhook receiver (S24)

`python -m aorc serve [--host 0.0.0.0] [--port 8080]` starts a small stdlib
`http.server` (`aorc/webhook.py`) that verifies `X-Hub-Signature-256` on every
POST (`hmac.compare_digest`, constant-time) before parsing the body — a
missing or wrong signature is a 401 and nothing is routed. A verified
delivery is ACKed (200) immediately, then handed to the same
`install.route_webhook(event, payload, handler=, loop=, installer=)` mapping
`compose()` already builds real collaborators for (`MergeTimeHandler` +
`ConfigGatedWakeLoop` + `InstallHandler`) — the receiver adds no routing
logic of its own. Redelivery is already harmless: `wake.claim_event`'s
`(issue, stage, head_sha)` dedup (S16) is the idempotency layer routed calls
land on.

Dev loop with no public URL yet: tunnel a local `serve` with
[smee.io](https://smee.io) (`npx smee-client --url <channel> --target
http://localhost:8080/webhook`) or `ngrok http 8080`, and point the GitHub
App's webhook URL at the tunnel's public address while iterating.

### Container runtime: Docker or Actions (S25)

`ConfigGatedWakeLoop` dispatches every issue's build through one
`ContainerRuntime`, selected by `.aorc.yml`'s `container:` block (invariant
#2 — the choice is config, not a code-level default swap) and constructed
only at the `compose()` root (invariant #1):

```yaml
container:
  runtime: docker            # default; omit the whole block for this
  # runtime: actions
  # workflow_file: aorc-build.yml   # required when runtime: actions
```

`runtime: actions` builds an `ActionsContainerRuntime`
(`aorc.github.actions_runtime`) that fires a real `workflow_dispatch` on
`workflow_file` instead of starting a local Docker container, then resolves
and later cancels the resulting run. It authenticates with its own
orchestrator-side App token, scoped to `actions: write` and minted once at
composition (wider than `credentials.MINIMAL_PERMISSIONS`, which only bounds
per-issue *container* tokens) — under `--dev-pat-minter` it reuses the fixed
`GITHUB_TOKEN` PAT instead, same escape hatch as the broker's.

The per-issue env (the S15 broker's `GITHUB_TOKEN` + LLM key) never rides in
`workflow_dispatch` inputs — those are visible in run logs and the runs-list
API — instead each value is sealed (libsodium sealed-box, via the `actions`
extra: `uv pip install -e ".[actions]"`, PyNaCl) against the target repo's
Actions public key and written as a repository secret
(`AORC_ISSUE_<n>_<KEY>`) immediately before dispatch; `teardown` deletes
those secrets again once the run is cancelled. This mirrors
`DockerContainerRuntime.start`'s env-file discipline (S19): a per-issue
credential never outlives the issue and never touches argv/logs.

**Known gap:** `ActionsContainerRuntime` is unit-tested against a fake
transport (`tests/test_actions_runtime.py`) and has a credential-gated
integration test (`tests/integration/test_actions_runtime_integration.py`,
`AORC_IT_GITHUB_TOKEN`/`AORC_IT_GITHUB_REPO`/`AORC_IT_GITHUB_WORKFLOW_FILE`)
that dispatches and cancels one real run — but that integration test has
**not** been run against a real repo this iteration (confirmed only that it
skips cleanly without credentials, same honesty caveat as S23/S24's App-token
and webhook work). The generated `aorc-rollback.yml` (S17/S18) has also never
been exercised live end-to-end (red main → auto-revert → `repository_dispatch`
→ `on_main_broken`) — only gated by a new CI `actionlint` job
(`.github/workflows/ci.yml`) that renders `install.ROLLBACK_WORKFLOW` to a
file and statically lints it, which itself has not been observed to actually
run (no `actionlint`/Docker available in this dev sandbox). See
`issues/25-actions-execution-wiring.md` for the full open scope (PR-number
extraction under squash merges, the live sandbox exercise).

### One-time GitHub App registration (S23)

The real minter needs a registered GitHub App — this part is manual, not
automated by any AORC code:

1. On the target GitHub org/user: **Settings → Developer settings → GitHub
   Apps → New GitHub App**.
2. Permissions: repository-level `Contents: Read & write`, `Issues: Read &
   write`, `Pull requests: Read & write` — matching `credentials.py`'s
   `MINIMAL_PERMISSIONS` ceiling exactly (broader App permissions than that
   ceiling do nothing useful; `CredentialBroker.mint` still narrows every
   per-issue request down to it, and GitHub separately enforces the App's
   own grant as the hard ceiling). Add repository-level `Actions: Read &
   write` too if this repo will use `container.runtime: actions` (S25) — that
   permission backs `ActionsContainerRuntime`'s own dispatch/cancel token,
   never a per-issue container token, so it sits outside
   `MINIMAL_PERMISSIONS` on purpose.
3. Subscribe to the webhooks S24 will consume (`issues`, `issue_comment`,
   `pull_request`, `repository_dispatch`) — not yet consumed live until S24
   lands, but fine to subscribe to now.
4. Generate and download a private key (PEM) from the App's settings page.
   Store it outside version control; point `AORC_GITHUB_APP_PRIVATE_KEY_PATH`
   at it.
5. **Install** the App on the target repository (App's page → Install App).
   Note the App ID (shown on the App's settings page) for
   `AORC_GITHUB_APP_ID`.
6. Install the exchange's extra: `uv pip install -e ".[apptoken]"` (PyJWT +
   cryptography — separate from the `github` extra since the HTTP half of
   the exchange is plain stdlib `urllib`, not PyGithub).

`tests/integration/test_github_app_token_integration.py` (credential-gated,
`AORC_IT_GITHUB_APP_ID`/`AORC_IT_GITHUB_APP_PRIVATE_KEY`/`AORC_IT_GITHUB_REPO`)
exercises the real exchange end-to-end once an App is registered this way.

`.aorc.yml` (default path `./.aorc.yml`, override with `--config`) must exist
and parse — absent, malformed, or missing `setup`/`test` fails closed with a
clear message and a non-zero exit before anything is constructed.

`compose()` in `__main__.py` takes every collaborator as an overridable
keyword argument, which is how `tests/test_main.py` drives all four
subcommands end-to-end against `MockGitHubClient`/`MockLLMClient`/
`MockContainerRuntime` with zero third-party deps and no environment
variables.
