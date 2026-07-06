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
