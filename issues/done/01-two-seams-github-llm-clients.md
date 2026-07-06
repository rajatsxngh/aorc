# S1 — Two seams: `GitHubClient` + `LLMClient`

## What to build

The two abstraction boundaries every other slice depends on. A single `GitHubClient` interface wraps all GitHub operations (read issues, post comments, create/update labels, open PRs, update Projects board, merge PRs). A single `LLMClient` interface wraps all provider interactions.

Ship real adapters behind each interface plus in-memory mock implementations for tests:
- `LLMClient` adapters: Claude (Anthropic SDK), OpenAI, and any OpenAI-compatible endpoint (including a local `base_url`). All providers accessed via the OpenAI-compatible API contract. Model names are never hardcoded — selection is driven by `.aorc.yml`.
- `GitHubClient` adapter over the GitHub SDK, plus a mock that records calls for assertion.

All orchestrator logic depends only on these interfaces, never the SDKs directly — the two-seam model that makes the whole system unit-testable without a real repo or real provider.

## Acceptance criteria

- [ ] `LLMClient` interface with Claude, OpenAI, and OpenAI-compatible/local adapters, all satisfying one contract
- [ ] `GitHubClient` interface covering issues, comments, labels, PRs, projects board, merge
- [ ] In-memory mock for each interface, usable in unit tests
- [ ] No orchestrator code imports a provider or GitHub SDK directly
- [ ] Model + provider chosen from config, no hardcoded model names
- [ ] Local `base_url` adapter path exists (reachability constraint enforced later in S18)

## Blocked by

- None — can start immediately
