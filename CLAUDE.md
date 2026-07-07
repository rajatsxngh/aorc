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
  per-issue containers from.
- Whatever `.aorc.yml`'s `llm:` block references for provider API keys (e.g.
  `$ANTHROPIC_KEY`), expanded by `config.py`.

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
   own grant as the hard ceiling).
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
