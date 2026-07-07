# S21 — Composition root / entry point (AORC can actually be started)

**UNBLOCKS EVERYTHING LIVE.** The full-system readiness review (post-S19) found
zero `def main`, zero `if __name__`, no CLI, and no code anywhere in `src/`
that constructs the real adapters together. `WakeLoop.compose` exists but
nothing outside tests calls it. AORC today is a well-tested engine with no
chassis: it cannot be started as a program.

## What to build

A real entry point — `python -m aorc` (and/or a `[project.scripts]` console
script) — that is the **one and only** place real SDK-backed objects are
constructed:

- Load `.aorc.yml` (env-expanded, invariant #2 — never hardcoded models or
  secrets), fail closed on absent/malformed config with a clear message, the
  same way `ConfigGate` already does.
- Construct, from config + environment:
  - `ClaudeLLMClient` / `OpenAICompatibleLLMClient` per the `llm:` block
    (wrapped in `BackoffLLMClient` per S14),
  - `SdkGitHubClient` for the target repo,
  - `CredentialBroker` — minter is injected; until S23 lands, an interim
    PAT-passthrough minter (`lambda key, repo, perms: env token`) is
    acceptable and must be clearly labeled as the S23 stand-in,
  - `SubprocessTestRunner`,
  - `WorktreeManager` over a local clone of the target repo,
  - `DockerContainerRuntime` from the configured base image,
  - `ConfigGatedWakeLoop` via the S16 composition-root discipline: the real
    GitHub client is wrapped in `ScrubbingGitHubClient` exactly once, and that
    wrapped instance is the only reference anything downstream holds.
- Subcommands (minimum viable): `install` (run `InstallHandler.on_install`),
  `backfill`, `wake` (one cron tick), and `run-issue <n>` (single-issue
  dispatch — becomes the S22 driver's entry once S22 lands).

This is wiring, not new behavior: every collaborator above already exists and
is unit-tested. The defect being fixed is that nothing composes them.

## Acceptance criteria

- [x] `python -m aorc <subcommand>` exists and is documented in the top-level
      README / CLAUDE.md
- [x] Composition root is the only module that imports/constructs provider or
      GitHub SDK adapters; `tests/test_no_sdk_imports.py` still passes
      (invariant #1 intact)
- [x] `.aorc.yml` absent, malformed, or missing required fields → clean
      fail-closed error, non-zero exit, no partial startup
- [x] No hardcoded model names or secrets (invariant #2); credentials come
      from env slots the config references
- [x] `ScrubbingGitHubClient` wraps the real client exactly once at the root;
      no unwrapped reference escapes
- [x] Interim PAT minter (if S23 not yet landed) is a single, clearly-marked
      injection point — swapping in the real S23 minter is a one-line change
- [x] Root is testable: collaborator construction is overridable (factory
      params or equivalent), and a unit test drives each subcommand end-to-end
      against the in-memory mocks with zero third-party deps

## Blocked by

- Nothing. All collaborators shipped in S1–S19. This ticket, with S22,
  unblocks S23, S24, and S25 (there is nothing real to plumb those into until
  AORC can start).

## Outcome (S21 close-out — honesty record)

- `src/aorc/__main__.py` (new): `python -m aorc {install,backfill,wake,
  run-issue <n>}` plus an `aorc` console script (`[project.scripts]`).
  `compose()` is the sole place `SdkGitHubClient`/`DockerContainerRuntime`/a
  real `LLMClient` are constructed; every parameter is overridable by keyword,
  which is how `tests/test_main.py` (15 tests) drives all four subcommands
  end-to-end against `MockGitHubClient`/`MockLLMClient`/`MockContainerRuntime`
  with zero third-party deps and no environment variables.
- Fail-closed startup, verified both by test and by hand (`python -m aorc
  install` with no config/env present): missing `.aorc.yml` (fixed a real gap
  in `config.load_config`, which previously let a bare `FileNotFoundError`
  escape instead of a clean `ConfigError`), malformed YAML, a missing
  `llm.primary` block, and a missing `GITHUB_TOKEN`/`AORC_REPO`/
  `AORC_BASE_IMAGE` env var each print one line to stderr and exit 1 before
  anything is constructed.
- `ScrubbingGitHubClient` wraps the real client exactly once via
  `ConfigGatedWakeLoop.compose` (S16 discipline); `Collaborators.github`
  exposes only the wrapped instance. Test asserts exactly one layer
  (`Collaborators.github._inner is gh`, the unwrapped mock passed in).
- Interim S23 stand-in: `pat_passthrough_minter(token)` returns a fixed
  `$GITHUB_TOKEN` value regardless of repo/permissions requested, and is
  `CredentialBroker`'s only minter call site in `compose()` — swapping in the
  real S23 exchange is a one-line change there. The broker's `private_key` is
  the empty string (no App key exists yet); `container_env`'s leak check
  no-ops on a falsy key by construction, so this is safe, not a workaround.
- `llm` (when not overridden) is wrapped in S14's `BackoffLLMClient` before
  reaching the loop, so real triage/clarification/decomposition calls get
  the transient-provider-error retry schedule; test pins that an overridden
  `llm` (tests) is used as-is, unwrapped.
- **Not composed here, and deliberately not stubbed in just to tick a box in
  the ticket's prose:** `SubprocessTestRunner` is never constructed by this
  module. Nothing in the S21 composition (`ConfigGatedWakeLoop`,
  `InstallHandler`) consumes a `TestRunner` — that only happens inside
  `DesignStage`/`TesterStage`/`CoderStage`/`ReviewerStage`/
  `MergeTimeHandler`, none of which S21 instantiates (that's S22's job, the
  pipeline driver). Wiring a `TestRunner` in now would be exactly the
  "shelf code nothing composes" this project's own commit history calls out
  as a defect, not progress. `run-issue` today only mints a token and starts
  a container (`WakeLoop.dispatch_issue`) — it does not yet run the build
  pipeline; that becomes real once S22 lands and the driver is wired in here.
- Manually exercised (not just unit-tested): ran `python -m aorc install`
  and `--help` for real from a shell with no `.aorc.yml`/env vars present,
  then with a real temp `.aorc.yml` and partial env, confirming each
  fail-closed message by eye before writing the assertions above.
- Documented in `CLAUDE.md` (`## Running AORC`) since the repo has no
  top-level README.
