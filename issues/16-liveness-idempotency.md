# S16 — Liveness & idempotency

## What to build

The stateless-orchestrator guarantees that keep issues from starving and keep double-fired webhooks harmless. The orchestrator holds no memory between invocations — on every wake it re-reads all state from GitHub and rebuilds any working picture from scratch. GitHub is the single source of truth.

**Held-issue wake (both mechanisms).**
- Primary: on every `PR-merged` webhook, sweep the held/blocked queue and re-dispatch any newly-unblocked issue (a held issue is usually waiting on a conflicting issue to merge — and that *is* a merge event).
- Backstop: a cron (every 10–15 min) re-reads all held/blocked issues and re-evaluates; no-ops on an empty queue, so it's nearly free.
- Both, because webhooks are at-least-once *and* can be dropped entirely.

**Webhook double-fire dedup.**
- Idempotency key: before acting, check the tuple `(issue_number, stage, head_sha)` recorded as a label/comment marker (no DB). Same event twice → second is a no-op.
- Artifact-presence check: a container's first action at any stage is "does this stage's artifact already exist on the branch?" Catches the race the key can't.
- Accepted v1 limitation: without a DB there's no atomic compare-and-swap, so a small window remains where two containers spin up before either commits. The artifact check makes this *wasteful* (one wasted design pass), not *wrong* (no double-merge).

**First-run backfill = re-sync.** On install (and re-runnable anytime AORC falls behind), list all currently-open issues via the API and feed each into triage as if newly opened; then event-driven flow takes over. Triage is orchestrator-side (cheap); actionable ones enter the dispatch queue, dispatched 5 at a time.

## Acceptance criteria

- [ ] Orchestrator rebuilds all state from GitHub on each wake; no persisted memory
- [ ] `PR-merged` webhook sweeps held/blocked queue and re-dispatches unblocked issues
- [ ] Cron backstop (10–15 min) re-evaluates held/blocked; no-ops on empty queue
- [ ] Dedup via `(issue_number, stage, head_sha)` marker → duplicate event is a no-op
- [ ] Artifact-presence check catches the pre-commit race (wasteful, not wrong)
- [ ] Backfill/re-sync sweeps all open issues into triage; actionable dispatched 5 at a time

### S15 wiring (credential model becomes a live property here, not a shelf library)

- [ ] `CredentialBroker` is the **only** source of container env: `ContainerHarness.dispatch` must not accept hand-built env dicts — either take an `IssueToken`/broker-built env exclusively, or run every incoming env through the `container_env` leak check (`CredentialLeakError` on any key-shaped value) before it reaches the runtime
- [ ] The wake loop calls `handle_token_expiry` for every in-flight container on every wake; `"re-queue"` feeds the held-issue re-dispatch path with a freshly minted token
- [ ] **All** orchestrator-side GitHub writes go through `ScrubbingGitHubClient` — the composition root wraps the real client once, and no code path holds an unwrapped reference; extend scrubbing to label **names** (`add_label`/`set_labels`/`create_label`) and branch names (`open_pull_request` head, `commit_file` branch), which today pass through unscrubbed

### Known limitation (deferred to S18/S19)

- A container holding `GITHUB_TOKEN` can push or hit the API **directly**, bypassing the orchestrator-side scrubber entirely — layer 2 only covers orchestrator-mediated writes. Mitigate at S18/S19 (real container plumbing): route agent pushes through an orchestrator-mediated path or a scrubbing egress proxy, and stop passing secrets as `docker run -e KEY=value` (visible in host `ps`).

## Blocked by

- S10 (held/blocked queue + dispatch)
