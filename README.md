# AORC — Autonomous Orchestrated Repo Contributor

AORC takes a GitHub issue written in plain English and drives it to a reviewed pull request without a human writing code. It plans the change, writes failing tests, writes the implementation, reviews its own work, and opens a PR. A human approves the merge.

It is a custom orchestrator built around Claude — not a wrapper over an agent framework. That choice, and the safety machinery it enabled, is most of what makes this project interesting.

> **Status: working prototype.** The pipeline runs end to end against a live repo and has been verified on real multi-issue runs. Several safety components are implemented and tested but not yet wired into the live path — these are listed explicitly under [Known gaps](#known-gaps). Treat it as a serious prototype, not a production system.

---

## How it works

A single issue moves through seven stages:

```
Setup → Triage → Design → Test → Code → Review → Pull Request → (human merges)
```

- **Setup** — mint a scoped token, create a git branch and worktree, start a container
- **Triage** — is this issue actionable? Too vague, or too large and in need of decomposition?
- **Design** — Claude writes a design document: interface, test specs, task list, files to touch. Committed to the branch.
- **Collision checkpoint** — the declared file list is compared against every other in-flight issue and open PR. Overlap means this issue is held until the conflict clears.
- **Test** — Claude writes tests *before* the implementation exists. They must fail for the right reason. A second Claude acts as critic on the tests.
- **Code** — Claude writes the implementation until the pre-written tests pass. Existing code in touched files must survive.
- **Review** — final checks (coverage, gates), then open the PR.
- **Merge** — a human decides. AORC never merges.

State lives in GitHub issue labels (`in-design`, `in-test`, `in-code`, `in-review`, `aorc-held`, `agent-blocked`). There is no private database — where an issue stands is visible to anyone looking at the repo.

**Nine distinct Claude agent roles** exist across the pipeline; a clean run uses six LLM calls (triage, design, tester, test-critic, coder, reviewer). Only the Test stage has a critic pair.

---

## Design decisions, and why

### Custom orchestrator rather than an agent framework

The value of this system is its safety rules — test locking, red-vs-error separation, collision detection, credential scrubbing, single-minter token issuance. Those are far easier to verify and enforce when you own every line of the control flow. A framework optimises for agent coordination; the hard problem here was never coordination, it was *constraint*.

### Tests are written before the implementation, always

This is the keystone. A pre-written failing test is an objective definition of "done" that neither the model nor the operator can talk their way around. It is also what makes the system reviewable by someone who isn't reading every diff: you watch tests go from red to green rather than auditing generated code line by line.

A distinction the pipeline enforces carefully: a test that **fails** because the feature doesn't exist yet (`red`) is correct and expected. A test that **errors** because it can't even be collected is not — that's a broken test, and the stage refuses to advance on it.

### Labels on GitHub are the single source of truth

Pipeline state is stored where humans already look. The cost is that messy labels mean messy state; the benefit is that there is exactly one version of the truth and no hidden bookkeeping that can silently disagree with reality.

### Every build runs in a disposable container

Freshly generated code is untrusted code. It executes inside a sealed container built from a template image, never on the host. One container per **build attempt** — not per issue lifetime. When an issue is held, its container is destroyed; on release, a fresh one is created and the worktree is re-synced onto the current `main`.

**The git branch is the state carrier across hold and release, never the container.** This is the single most load-bearing idea in the runtime model.

### Concurrency is gated on declared file overlap

After design (and only after design, because that's when the touched files are known), the orchestrator compares the issue's claimed files against all in-flight work and open PRs. Non-overlapping issues run in parallel; overlapping ones are held with a breadcrumb comment explaining why. When the blocking work merges, the held issue is released, rebased onto the new `main`, and re-run — so it accumulates on top of finished work rather than clobbering it.

Verified live: two issues touching the same file were correctly sequenced while an independent issue ran in parallel; the released issue's output contained the merged changes plus its own.

### Rebase conflicts are never auto-resolved

If re-syncing a released issue onto `main` conflicts, AORC aborts and labels `agent-blocked` with the conflict output. It does not guess at a resolution. Escalating honestly is treated as the correct outcome, not a failure.

### The human approves every merge

Merging changes the real default branch and is expensive to undo. That decision stays with a person. This gate has already caught a genuine regression before it reached `main`.

### Mechanical guards over model judgement

Where a constraint can be enforced deterministically, it is: credential scrubbing uses fixed patterns rather than asking a model what looks like a secret; the code stage runs a mechanical check that top-level names present before a write still exist after it; red-vs-error classification is structural, not inferred.

---

## Known gaps

Listed honestly, because the difference between "implemented" and "wired into the live path" has been the single most common source of bugs in this project.

| Component | Status |
|---|---|
| **Cost circuit-breakers** (per-issue / per-run / daily caps) | Implemented and tested, **zero live callers**. No spend metering currently runs. |
| **Wall-clock compute limit** | Implemented, never invoked. Runaway containers are bounded only by token expiry. |
| **Clarification reply→resume loop** | The question-asking half is wired; the reply-handling and resume half has no live callers. |
| **Escalation ladder** (retry on a stronger model) | Built, not routed through by any stage. |
| **Container teardown on success/block** | Only the *held* path tears down. Containers from successful or blocked runs linger until token expiry. |
| **Concurrency ceiling** | Enforced only in the batch dispatch path. Direct single-issue runs bypass it. |
| **Import path derivation** | The test import header is machine-written from the design's declared file path. When the design declares an unresolvable path (a typo, or a bare filename for a new module), the derivation faithfully produces an unimportable module. No validation currently checks that the derived module is reachable from the repo's declared source roots. |
| **Generated test file accumulation** | Per-issue test files are committed to the target repo and accumulate over runs. |

The recurring theme across all of these: **the components work; the wiring is where the bugs live.** Several were found only by running the system live after the unit tests were green.

---

## Running it

Requires Docker, Python 3.12, and a GitHub token scoped to the target repository.

```bash
# Run a single issue end to end
python -m aorc --dev-pat-minter --config <config>.yml --repo <owner>/<repo> run-issue <n>

# Process all open issues, applying collision and concurrency rules
python -m aorc --dev-pat-minter --config <config>.yml --repo <owner>/<repo> backfill

# Sweep held issues and release any the latest merges unblocked
python -m aorc --dev-pat-minter --config <config>.yml --repo <owner>/<repo> wake
```

`run-issue` is the predictable path for one issue. `backfill` is the orchestration path — it discovers open issues itself, applies the concurrency ceiling, and holds colliding work.

The target repository is configured with a YAML file describing how to install and test it.

### Local dashboard (optional)

A read-only status dashboard with trigger controls lives on a separate branch. It is an additive layer: it imports the core's GitHub adapter and shells out to the same CLI commands, and makes no changes to the core.

```bash
uvicorn ui.bridge.app:app --port 8000
```

---

## Test suite

```bash
pytest
```

580 tests. The suite runs entirely against fakes — no network, no Docker — which is fast and hermetic, and is also precisely why several bugs surfaced only under live runs. The gap between the fake adapter and the real one has been the most productive place to look for defects.

---

## Repository layout

```
src/aorc/
  __main__.py     CLI entry point and composition root
  wake.py         orchestrator: dispatch, in-flight registry, held sweep
  driver.py       pipeline driver: walks one issue through the stages
  harness.py      containers, worktrees, branch lifecycle
  design.py       design stage and file-path resolution
  tester.py       test stage, critic, import header machinery
  coder.py        code stage and preservation guard
  reviewer.py     review stage and gates
  merge.py        merge-time handling and held release
  guards.py       cost and compute circuit-breakers
  github/         GitHub adapter (real and fake implementations)
tests/            580 tests
```

---

## Notes

This is a personal project, developed against a dedicated sandbox repository. The design goal was never to build the fastest autonomous coder — it was to build one whose failures are legible, whose unsafe actions are structurally prevented rather than discouraged, and which stops and says so when it doesn't know.
