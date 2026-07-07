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

- `GITHUB_TOKEN` — a GitHub token for `SdkGitHubClient`, and, until S23 lands
  the real App-JWT → installation-token exchange, also the value returned by
  the interim PAT-passthrough minter (`aorc.__main__.pat_passthrough_minter`,
  a single clearly-marked stand-in for `CredentialBroker`'s real minter).
- `AORC_REPO` — `owner/repo` (or pass `--repo`).
- `AORC_BASE_IMAGE` — the Docker image `DockerContainerRuntime` starts
  per-issue containers from.
- Whatever `.aorc.yml`'s `llm:` block references for provider API keys (e.g.
  `$ANTHROPIC_KEY`), expanded by `config.py`.

`.aorc.yml` (default path `./.aorc.yml`, override with `--config`) must exist
and parse — absent, malformed, or missing `setup`/`test` fails closed with a
clear message and a non-zero exit before anything is constructed.

`compose()` in `__main__.py` takes every collaborator as an overridable
keyword argument, which is how `tests/test_main.py` drives all four
subcommands end-to-end against `MockGitHubClient`/`MockLLMClient`/
`MockContainerRuntime` with zero third-party deps and no environment
variables.
