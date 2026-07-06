# S14 — Failure escalation + backoff

## What to build

The escalation ladder and the strict separation between provider errors and bad model output.

**Escalation ladder:** primary model × `failure.primary_attempts` → escalation model × `failure.escalation_attempts` → surface to human. Model slots are driven entirely by `.aorc.yml`.

**On exhaustion:** label the issue `agent-blocked` and post a detailed failure summary as a GitHub comment — the full error, the last test output, and what the agent attempted — so the human can decide to clarify the spec, fix a dependency, or close won't-fix.

**Provider errors vs. bad output (separate counters).** A provider error (429/500/timeout/connection reset) is categorically different from bad output (test still red) and must not share a counter, or a flaky network would burn `primary_attempts` and prematurely escalate a solvable issue. Provider error → retry the **same** model with exponential backoff (3 tries: 2s / 8s / 30s); only if backoff exhausts does it count as a real failure and enter the ladder. Exception: connection-refused to a local `base_url` on a cloud runner is not transient — fail fast (Local-LLM constraint, enforced in S18).

**Bot rate-limits.** GitHub enforces per-app secondary limits; bursts can trip 403/429. On such responses, wait and retry with backoff respecting the `Retry-After` header; do not count it as a real failure or `agent-block`.

## Acceptance criteria

- [ ] Ladder: N primary retries → M escalation retries → `agent-blocked` + comment
- [ ] Failure comment contains full error, last test output, and what was attempted
- [ ] Provider-error backoff (2s/8s/30s) on the same model, on a separate counter from bad output
- [ ] Backoff exhaustion counts as one real failure and enters the ladder
- [ ] GitHub 403/429 → backoff on `Retry-After`, never counted as failure/`agent-blocked`
- [ ] Model slots read from `.aorc.yml`, no hardcoded names

## Blocked by

- S1 (`LLMClient` / `GitHubClient`)
