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

- [ ] `python -m aorc <subcommand>` exists and is documented in the top-level
      README / CLAUDE.md
- [ ] Composition root is the only module that imports/constructs provider or
      GitHub SDK adapters; `tests/test_no_sdk_imports.py` still passes
      (invariant #1 intact)
- [ ] `.aorc.yml` absent, malformed, or missing required fields → clean
      fail-closed error, non-zero exit, no partial startup
- [ ] No hardcoded model names or secrets (invariant #2); credentials come
      from env slots the config references
- [ ] `ScrubbingGitHubClient` wraps the real client exactly once at the root;
      no unwrapped reference escapes
- [ ] Interim PAT minter (if S23 not yet landed) is a single, clearly-marked
      injection point — swapping in the real S23 minter is a one-line change
- [ ] Root is testable: collaborator construction is overridable (factory
      params or equivalent), and a unit test drives each subcommand end-to-end
      against the in-memory mocks with zero third-party deps

## Blocked by

- Nothing. All collaborators shipped in S1–S19. This ticket, with S22,
  unblocks S23, S24, and S25 (there is nothing real to plumb those into until
  AORC can start).
