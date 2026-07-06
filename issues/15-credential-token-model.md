# S15 — Credential & token model

## What to build

A hard safety requirement, not an implementation preference. Because agents run unattended with real repo-write permissions, the credential model bounds the blast radius of a misbehaving agent. Container isolation and credential-scoping are partners — both required.

**Rules:**
1. The App private key is the master credential and lives in exactly one place: the orchestrator (GitHub Actions encrypted secrets in v1). It is the only thing that can mint tokens.
2. The private key is **NEVER** passed into an agent container. No agent, at any stage, receives the signing key.
3. The orchestrator mints a short-lived, narrowly-scoped installation access token **per issue** — scoped to a single repository and the minimal permission set that issue needs, expiring in ≈1 hour. Only that token is handed to the container.
4. An agent therefore holds at most a single-repo, ~1-hour token.
5. GitHub repo tokens and LLM provider credentials are separate secrets with separate lifetimes, in separate slots.

**Token expiry mid-pipeline (tear-down-and-resume).** The ~1-hour token *will* expire on hard issues. On expiry, tear down the container and re-queue the issue, resuming from the last committed artifact. The container **must never** re-authenticate or mint/refresh its own token — reuses the checkpoint/re-queue machinery, adds zero new security surface.

**Secret scrubbing (two layers, no LLM judgment).**
- Primary: tokens/keys passed as env vars; the agent is instructed never to print them.
- Safety net: before any agent-produced text is posted to GitHub, run it through a hardcoded regex set drawn from an existing scanner (gitleaks/trufflehog lists) and blank matches. Must include GitHub prefixes (`ghp_` / `ghs_` / `github_pat_`) **and** LLM keys (`sk-ant-`, `sk-`). Fully deterministic.

## Acceptance criteria

- [ ] Private key confined to the orchestrator; never injected into any container
- [ ] Per-issue token minted: single-repo, minimal permissions, ~1h expiry; only the token reaches the container
- [ ] GitHub and LLM credentials in separate slots with separate lifetimes
- [ ] Token expiry → teardown + re-queue from last committed artifact; no in-container refresh path
- [ ] Two-layer scrubbing; regex from a battle-tested list covering GitHub + LLM prefixes; no LLM judgment
- [ ] All agent-posted text scrubbed before it reaches GitHub

## Blocked by

- S4 (container harness receiving the token)
