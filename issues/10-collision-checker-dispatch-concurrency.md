# S10 — Collision checker + dispatch selector + concurrency

## What to build

Real parallelism with correctness. This replaces S4's trivial checkpoint verdict with the authoritative post-Design collision check, and adds the up-front dispatch selector.

**Dispatch selector (cheap, up-front).** No clever collision prediction from issue text before dispatch. Only:
- Skip declared-blocked issues (`blocked by #X`, or an epic task-list child of an unfinished parent). Declared dependencies are the one reliable up-front signal, always honored.
- Respect the concurrency ceiling (default **5**, configurable, global — one machine). Excess actionable issues wait in a dispatch queue. An epic is never dispatched before decomposition.

**Checkpoint collision check (authoritative, post-Design).** The container reports its exact `files` and waits. The orchestrator compares against the **collision set** and returns a verdict:
- **Proceed** — no overlap → container continues to Tester → … → PR.
- **Hold** — overlap → container torn down, issue re-queued; Design work preserved on the branch so it resumes from the checkpoint without re-running Design.
- **Conservative default** — overlap uncertain → hold.

**Collision set scope:** in-flight = active containers **plus open, unmerged AORC PRs** (an open PR occupies its files; two could otherwise pass clean and collide at merge). Open PRs treated exactly like a live container's reported file list.

**Collision computation:** hold if **either** the exact file-path sets intersect **or** Graphify shows one issue's files sit in the other's import/call blast radius. Not directory-prefix (too coarse), not exact-path-only (too narrow). Graphify query fails/times out or overlap uncertain → hold.

**Stateless bookkeeping:** each container's reported file list is written to GitHub (labels/comments); on every wake the orchestrator re-reads and rebuilds a throwaway collision picture — no separate state file.

## Acceptance criteria

- [ ] Dispatch selector skips declared-blocked issues; never dispatches an epic pre-decomposition
- [ ] Concurrency ceiling enforced (default 5, configurable, global); overflow queues
- [ ] Checkpoint verdict: overlapping sets → hold+teardown+re-queue (Design preserved); disjoint → proceed
- [ ] Collision set includes open unmerged AORC PRs, treated like live file lists
- [ ] Computation = path-intersect OR Graphify blast-radius; uncertain/timeout → hold
- [ ] File lists persisted to GitHub; collision picture rebuilt from scratch each wake
- [ ] Uses a SHARED InFlightRegistry across all concurrent harnesses (not each harness's private default), or cross-issue collision detection silently never fires. A test must prove two concurrent issues claiming the same file actually collide.

## Blocked by

- S4 (checkpoint harness), S9 (Graphify blast-radius)
- 09b (checkpoint injection) — collision logic cannot plug in until the checkpoint is injectable and can see other issues/PRs
