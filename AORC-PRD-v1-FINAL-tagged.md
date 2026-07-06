# PRD: Autonomous Orchestrator for Repository Closure (AORC) — v1 (Final, Consolidated)

> **Status.** This is the single, self-contained specification. It merges the original PRD with all gap-resolution decisions (formerly the separate AORC-DECISIONS log). Every decision is applied in place; there is nothing to cross-reference. Where this document once disagreed with an earlier draft, the resolutions here are authoritative and deliberate.
>
> **Design intent.** AORC runs unattended, and the operator may not be able to verify code correctness by reading a diff. Therefore correctness, safety, cost, and liveness are enforced by the *system*, not by human review. Anywhere an AI coding agent might otherwise improvise an important decision, this document removes the ambiguity.
>
> A **decision-provenance index** at the end maps every resolved gap (A1–A3, B1–B27) to the section that now contains it.

---

## Problem Statement

Software teams accumulate GitHub issues faster than they can close them. Triaging, designing, implementing, testing, and reviewing each issue requires sustained human attention at every stage. For a team managing a repo like the v1 evaluation target (a private YAML→SQL semantic-layer repository, ~104 open issues spanning bugs, features, and epics), the backlog is permanent — not because the work is too hard, but because human bandwidth is the bottleneck at every handoff. Existing AI coding tools assist humans; they don't replace the human-in-the-loop at each stage.

## Solution

AORC is a GitHub App that autonomously drives GitHub issues from open to merged PR — with no human required except to answer clarifying questions and approve the final merge. It installs on any repository, reads `.aorc.yml` for repo-specific config, and runs as a GitHub Actions workflow. Each issue passes through a staged pipeline: triage, refinement (clarify/decompose if not ready), design, test authoring, test-critique, implementation, and review. Parallel issues run in isolated Docker containers, one issue per container. A Graphify-backed knowledge graph, combined with each container's post-Design file report, determines collisions and ordering. The human's only recurring jobs are: answer questions posted as GitHub comments, and merge PRs once the pipeline certifies them.

---

## User Stories

### Installation & Onboarding
1. As a repo owner, I want to install AORC as a GitHub App on my repository, so that I don't have to manage credentials or configure webhooks manually.
2. As a repo owner, I want AORC to open a PR adding `.aorc.yml` to my repo on install, so that I have a starting point for configuring test commands and LLM preferences.
3. As a repo owner, I want to specify my test runner, setup commands, and lint commands in `.aorc.yml`, so that AORC can run my tests without guessing my toolchain.
4. As a repo owner, I want to configure which LLM provider and model AORC uses (primary and escalation), so that I can use Claude, OpenAI, or a local model running on my machine.
5. As a repo owner, I want AORC to work without any external database or custom infrastructure, so that I don't have to maintain anything beyond the GitHub App installation.
6. As a repo owner, I want a GitHub Projects board created automatically on install, so that I have a Kanban view of every issue's pipeline stage from day one.

### Issue Triage
7. As a repo owner, I want AORC to classify each open issue as either **actionable** or **not-ready** when it is opened or labeled, so that ready issues proceed to build and unclear or oversized issues are refined first. ("Actionable" means the issue has a bounded, testable definition of done — not merely that it looks small.)
8. As a repo owner, I want not-ready issues to carry a reason tag (`vague` or `epic`) explaining *why* they were parked, so that I can see at a glance whether an issue is waiting on my answers or just needs to be broken down — while both follow the same refinement path.
9. As a repo owner, I want AORC to send every not-ready issue through one shared refinement path (clarify if vague, structure into a PRD, split into sub-issues if oversized) and then re-check it, so that agents never hallucinate implementations for underspecified or oversized work.
10. As a repo owner, I want AORC to apply GitHub labels (`needs-clarification`, `in-design`, `in-test`, `in-code`, `in-review`, `agent-blocked`) to track each issue's pipeline stage, so that the Projects board always reflects real state.

### Clarification (Grill-Me)
11. As a repo owner, I want AORC to post clarifying questions as a GitHub comment when an issue is too vague to implement, so that I can answer them in the place I already track work.
12. As a repo owner, I want the clarifying questions to be generated using the grill-me technique (relentless interrogation of the problem statement), so that the resulting answers produce a crystal-clear spec.
13. As a repo owner, I want AORC to resume the pipeline automatically when I reply to its clarifying questions on the issue, so that I don't have to trigger anything manually.
14. As a repo owner, I want AORC to label vague issues `needs-clarification` and move them to the **Needs Clarification** column on the Projects board, so that I can see exactly which issues are waiting for me (distinct from `agent-blocked` issues, which need debugging rather than an answer). **[A1]**
15. As a repo owner, I want AORC to ask one question at a time rather than dumping a list, so that the clarification dialog feels like a conversation rather than a form.

### Epic Decomposition
16. As a repo owner, I want AORC to automatically decompose epic issues into concrete, implementable sub-issues using the prd-to-issues process, so that I don't have to manually break down large features.
17. As a repo owner, I want AORC to run grill-me on a vague epic before decomposing it, so that the resulting sub-issues are based on a clear understanding of the intent.
18. As a repo owner, I want the sub-issues created by AORC to be linked back to the parent epic (via a deterministic marker), so that the Projects board shows the relationship and re-running decomposition never creates duplicates.
19. As a repo owner, I want the parent epic to close automatically when all of its AORC-created sub-issues are merged, so that I don't have to track epic completion manually.

### Dependency Resolution & Sequencing
20. As a repo owner, I want AORC to build a Graphify knowledge graph of my codebase on startup, so that it understands which modules depend on which.
21. As a repo owner, I want AORC to re-index the Graphify graph after every merged PR, so that blast-radius calculations stay accurate as the codebase evolves.
22. As a repo owner, I want epics to be fully decomposed into actionable sub-issues *before* any sequencing decision, so that every real unit of work is on the board before the orchestrator decides what runs together.
23. As a repo owner, I want AORC to respect declared GitHub dependencies (blocked-by, epic task lists, issue references) when choosing which issues to dispatch, so that explicit relationships are always honored up front.
24. As a repo owner, I want AORC to run Design first inside each container and have the container report its exact file list back before coding, so that collision decisions are made on real data rather than up-front guesses.
25. As a repo owner, I want the orchestrator to compare each issue's real file list (and its Graphify blast radius) against all other in-flight issues *and open unmerged AORC PRs* at the checkpoint, holding any issue that would collide, so that two agents never write to the same files and two open PRs never collide at merge.
26. As a repo owner, I want AORC to default to holding/sequencing an issue when overlap is uncertain, so that correctness is never sacrificed for parallelism.

### Pipeline: Design Stage
27. As a repo owner, I want a design agent to read the issue (and any clarification Q&A) and produce a design doc — in a strict machine-readable schema — containing the interface (function signatures), test specifications, an ordered implementation task list, the exact file list, and a confidence flag, so that subsequent agents have a machine-checkable definition of done and a defined sequence of steps.
28. As a repo owner, I want the design agent to operate with a fresh, scoped context (issue + Q&A + relevant repo files only), so that token usage stays minimal and context never accumulates across stages.
29. As a repo owner, I want the design doc to be committed to the issue's working branch, so that it serves as the persistent record of what was decided and why, and as a resumable artifact after a tear-down.

### Pipeline: Test Authoring Stage
30. As a repo owner, I want a tester agent (separate from the coder) to write failing tests from the design doc, so that the tests encode the spec rather than the implementation.
31. As a repo owner, I want the tester agent to never see the implementation code when writing tests, so that it tests behavior rather than mirroring internal details.
32. As a repo owner, I want the failing tests to be committed to the branch before the coder runs, so that there is a hard red-green boundary between spec and implementation.

### Pipeline: Implementation Stage
33. As a repo owner, I want a coder agent to write minimal code to pass the tests authored by the tester agent, so that implementations are driven by specs, not by the coder's own assumptions.
34. As a repo owner, I want the coder agent to receive only pass/fail results and error output from test runs, not the test source code, so that it cannot game the tests by writing code that exploits test internals.
35. As a repo owner, I want the coder agent to iterate until tests pass or the attempt limit is reached, so that transient failures don't cause the issue to be abandoned prematurely.
36. As a repo owner, I want the coder agent to run `setup`, `test`, and `lint` commands from `.aorc.yml`, so that it uses my project's actual toolchain rather than guessing.

### Pipeline: Review Stage
37. As a repo owner, I want a reviewer agent to evaluate the diff against the design doc and original issue requirements, so that PRs are reviewed for correctness before a human sees them.
38. As a repo owner, I want the reviewer agent to block the PR if it finds deviations from the design doc, so that the coder must fix issues before the PR is surfaced for human merge.
39. As a repo owner, I want reviewer feedback posted as PR comments so that I can read the reviewer's reasoning when I do the final merge review.

### Parallel Execution
40. As a repo owner, I want independent issues to run in parallel in isolated Docker containers, so that the orchestrator processes my backlog faster than sequential execution would allow.
41. As a repo owner, I want each container to use a pre-baked base image with Claude Code, skills, and MCPs already installed, so that spinning up a new agent is near-instant.
42. As a repo owner, I want each container to work on its own git worktree, so that agents never collide on the working tree.
43. As a repo owner, I want each container to be torn down after its issue's pipeline completes (or when the orchestrator issues a hold at the checkpoint), so that compute is not wasted on idle agents and one container always maps to exactly one issue.

### Failure Handling
44. As a repo owner, I want AORC to retry a failing stage up to N times on the primary model before escalating, so that transient LLM errors don't immediately surface to me.
45. As a repo owner, I want AORC to automatically retry on the escalation model (e.g. Opus) after the primary model exhausts its attempts, so that harder problems get a more capable agent before I'm interrupted.
46. As a repo owner, I want AORC to label an issue `agent-blocked` and post a detailed failure summary as a GitHub comment when all model tiers are exhausted, so that I know exactly what was tried and what broke.
47. As a repo owner, I want the failure summary to include the full error, the last test output, and what the agent attempted, so that I can decide whether to clarify the spec, fix a dependency, or close the issue as won't-fix.

### LLM Flexibility
48. As a repo owner, I want to configure any OpenAI-compatible LLM provider as the primary or escalation model in `.aorc.yml`, so that I'm not locked into a specific vendor.
49. As a repo owner, I want to point AORC at a locally running LLM (e.g. Ollama, LM Studio) using a `base_url` in `.aorc.yml`, so that I can run the orchestrator without sending code to external APIs (on a self-hosted runtime; see Local-LLM constraint).
50. As a repo owner, I want the primary and escalation model slots to be independently configurable, so that I can use a fast cheap model for most work and a powerful model only for hard cases.

### Merge & Completion
51. As a repo owner, I want AORC to open a PR automatically when the reviewer agent approves, so that I only need to review and merge rather than tracking down what's ready.
52. As a repo owner, I want to manually merge PRs in v1, so that I retain final control while I build confidence in the pipeline's output quality.
53. As a repo owner, I want to enable auto-merge in `.aorc.yml` once I trust the pipeline (and only on repos that have smoke tests), so that the orchestrator can eventually close issues without my involvement.
54. As a repo owner, I want the GitHub issue to close automatically when its PR is merged, so that the Projects board and issue list stay accurate without manual cleanup.

### Kanban Visibility
55. As a repo owner, I want a GitHub Projects board that reflects each issue's pipeline stage in real time, so that I can see the full backlog status without running any commands.
56. As a repo owner, I want the Projects board to have columns: Backlog, Needs Clarification, In Progress, Blocked, In Review, Done, so that I can distinguish issues waiting for me from issues the agent is actively working.
57. As a repo owner, I want the board columns to be **derived from labels** via a fixed lookup (agents never set a column directly), so that label and board never disagree.

---

## Implementation Decisions

### GitHub App
- AORC ships as a GitHub App installable on any repository or organization.
- On install, the App opens a PR adding `.aorc.yml` to the target repo and creates a GitHub Projects board with the standard column set.
- The App registers a webhook for issue events (opened, edited, labeled, comment created), PR events (merged, opened, review-comment created), and push events (to main, for auto-rollback).
- Fine-grained permissions: issues (read/write), pull requests (read/write), contents (read/write), projects (read/write), actions (read/write).

### Credential & Token Model

This section is a **hard safety requirement**, not an implementation preference. Because agents run unattended with real repo-write permissions, the credential model bounds the blast radius of a misbehaving agent. Container isolation alone is insufficient: a sealed container holding a broad credential can still reach every repo that credential can touch. Isolation and credential-scoping are partners — both are required.

**Rules:**

1. **The App private key is the master credential and lives in exactly one place: the orchestrator.** Stored in the orchestrator's secret store (GitHub Actions encrypted secrets in v1; a dedicated secrets manager if/when self-hosted). It is the only thing that can mint tokens.
2. **The private key is NEVER passed into an agent container.** No agent, at any stage, receives the signing key. An agent able to mint its own tokens could mint one for any repo the App is installed on — recreating the broad-credential blast radius this model exists to prevent.
3. **The orchestrator mints a short-lived, narrowly-scoped installation access token per issue** — scoped to a single repository and the minimal permission set that issue needs, expiring in ≈1 hour. The orchestrator mints it, hands only that token to the container, and the token dies on expiry.
4. **An agent therefore holds, at most, a single-repo, ~1-hour token.** A compromised or rogue agent is bounded to one repo for at most one hour; it cannot reach other repos, the org, or the orchestrator.
5. **GitHub repo tokens and LLM provider credentials are separate secrets with separate lifetimes**, kept in separate slots. Rotating or swapping one never touches the other; an LLM key is never used for GitHub access or vice versa.

**Token expiry mid-pipeline (tear-down-and-resume).** The ~1-hour token *will* expire mid-pipeline on hard issues (fix loop + escalation can exceed an hour). On expiry, **tear down the container and re-queue the issue**, resuming from the last committed artifact (design/tests already on the branch). The container **must never re-authenticate or mint/refresh its own token** — the private key is not in the container, and an in-container refresh path would violate the credential model. This reuses the checkpoint/re-queue machinery and adds zero new security surface. **[B11]**

**Secret scrubbing (two layers, no LLM judgment).** **[B10]**
- **Primary:** tokens/keys are passed as environment variables; the agent is instructed never to print them, so they should never appear in output at all.
- **Safety net:** before any agent-produced text is posted to GitHub, run it through a **hardcoded regex set drawn from an existing secret scanner** (gitleaks / trufflehog pattern lists) and blank out matches. Use a battle-tested list, not a homemade one. The pattern set **must include GitHub *and* LLM-provider keys**: GitHub prefixes `ghp_` / `ghs_` / `github_pat_`, plus LLM keys `sk-ant-` (Anthropic) and `sk-` (OpenAI). No LLM is used to judge what is a secret — this decision is fully deterministic.

**Flow:** issue dispatched → orchestrator (holding the private key) mints a 1-hour token scoped to *this issue's repo* → token + LLM config injected into the container → agent does branch/commit/PR work with that token only → token expires; container destroyed.

### Runtime: GitHub Actions
- The orchestrator runs as a GitHub Actions workflow, triggered by the App's webhooks plus a cron backstop.
- Each issue pipeline runs as a separate Actions job dispatched by the orchestrator workflow.
- Container agents run inside Actions using Docker-in-Docker or pre-registered runner images.
- No persistent server is required — compute is serverless and event-driven. (Exception: local LLMs and, optionally, the shared Graphify graph DB require a self-hosted runtime; see below.)
- **Stateless orchestrator.** The orchestrator holds no memory between invocations. On every wake it re-reads all state from GitHub (issues, labels, PRs, board) and rebuilds any working picture (e.g. the collision set) from scratch. GitHub is the single source of truth.

### Operational Limits & Gates

These are hard, unattended-safety limits. All are config values; start conservative and tune on real data.

**Cost circuit-breaker (three levels, all ping-on-hit):** **[B1]**
- **Per-issue:** ~$3–5 of tokens across an issue's full pipeline → stop, label `agent-blocked` (reason: cost cap), ping. *Primary guard; catches one stuck agent.*
- **Per-run (one wake cycle):** ~$50 → pause the whole system, ping. *Catches a systemic bug.*
- **Daily:** ~$100/day → halt everything until manually reset. *Backstop.*

**Cost-cap mid-issue behavior (clean-stop).** When the per-issue cap trips mid-pipeline, **finish the current stage's commit, then stop** at the natural artifact boundary → `agent-blocked` (reason: cost cap), ping; resume later from a *valid* committed artifact. **Overshoot guard:** if a single stage is what's blowing the budget (e.g. a stuck fix loop), hard-stop at **1.5× the per-issue cap**, whichever comes first. Rationale: a mid-stage hard-kill would leave a half-written design doc that fails the strict schema and would then masquerade as a *clarity* problem (mis-routing to `needs-clarification`). Clean-stop avoids the wrong signal. **[B4]**

**Container compute limits.** **[B2]**
- **Wall-clock:** 30–45 min per container → kill, `agent-blocked`, ping. (Deliberately well under the 6-hr Actions ceiling.)
- **Memory/CPU:** default GitHub runner (~2 CPU / 7 GB); never opt into larger runners.
- Whichever trips first — cost cap or timeout — stops the agent.

**Concurrency ceiling.** Default **5**, configurable, **global** (one machine). Self-limited by local hardware in v1; per-repo limits and higher values matter only after moving to cloud (v2). Actionable issues beyond the ceiling wait in a dispatch queue. **[B3]**

### Liveness, Idempotency & State Integrity

**Held-issue wake (both mechanisms).** **[B5]**
- **Primary:** on every `PR-merged` webhook, sweep the held/blocked queue and re-dispatch any newly-unblocked issue (a held issue is usually waiting on a conflicting issue to merge — and that *is* a merge event).
- **Backstop:** a cron (every 10–15 min) re-reads all held/blocked issues from GitHub and re-evaluates; no-ops on an empty queue, so it's nearly free.
- Both, because webhooks are at-least-once *and* can be dropped entirely; one dropped merge event would otherwise starve an issue forever with no alarm.

**Webhook double-fire dedup.** **[B6]**
- **Idempotency key:** before acting on a webhook, check the tuple `(issue_number, stage, head_sha)` recorded as a label/comment marker (no DB). Same event twice → second is a no-op.
- **Artifact-presence check:** a container's first action at any stage is "does this stage's artifact already exist on the branch?" Catches the race the key can't.
- **Accepted v1 limitation:** without a DB there is no atomic compare-and-swap, so a small window remains where two containers spin up before either commits. The artifact check makes this *wasteful* (one wasted design pass), not *wrong* (no double-merge). Acceptable at this scale; a DB-backed compare-and-swap is deferred to v2 (multi-tenant).

**First-run backfill (one-time sweep = re-sync).** On install, AORC lists all currently-open issues via the GitHub API and feeds each into triage as if newly opened; then normal event-driven flow takes over. The same sweep is a **re-sync command** — re-run anytime AORC falls behind (missed events / offline) to rebuild state from GitHub. Because triage is orchestrator-side (cheap, no container), all ~104 issues can triage; actionable ones enter the dispatch queue and are dispatched **5 at a time**. ⚠️ A large first-run triage burst can approach the per-run cost cap on the first wake — keep that in mind when setting the cap, or batch the initial sweep. **[B7]**

**Decomposition idempotency.** When creating sub-issues, tag each with a deterministic marker in its body (e.g. `aorc-parent: #42`, `aorc-sub-index: 3`). Before creating sub-issue N of parent #42, search for that exact marker; if it exists, **skip**. Re-running decomposition (via re-sync) only creates what's missing — never duplicates, never delete-and-redo. **[B8]**

**Decomposition depth.** No behavioral cap — a runaway "epic decomposes into epics" loop is halted by the per-issue / per-run cost cap. **Diagnostic-only** depth label (e.g. `depth:3`) is applied per level so that when a loop trips the cost cap, it's obvious it was a non-converging decomposition rather than a genuinely expensive issue. **[B9]**

### State Machine: GitHub Labels (label is truth, column is derived)
- Pipeline state is stored exclusively in GitHub labels. No external database. The board **column is derived from the label** via this fixed lookup — agents never set a column directly:

  | Label | Column |
  |---|---|
  | (none / triaged) | Backlog |
  | `needs-clarification` | Needs Clarification |
  | `in-design`, `in-test`, `in-code` | In Progress |
  | `in-review` | In Review |
  | `agent-blocked` | Blocked |
  | (issue closed) | Done |

- `needs-clarification` (waiting on a human answer) and `agent-blocked` (a stuck agent needing debugging) are **separate columns** because they demand different human actions.
- Stage is considered complete only when its artifact exists (design doc committed, test file committed, PR opened). Label alone is not sufficient — artifact presence is the source of truth.
- On orchestrator restart, it reads labels + checks for artifacts to reconstruct pipeline state and resume correctly.

### Repo Configuration: `.aorc.yml`
- Every target repo must have a `.aorc.yml` at its root.
- Schema:
  ```yaml
  llm:
    primary:    { provider: claude, model: claude-sonnet-4-6, api_key: $ANTHROPIC_KEY }
    escalation: { provider: claude, model: claude-opus-4-8, api_key: $ANTHROPIC_KEY }
    # Local LLM example (requires a self-hosted runtime):
    # primary: { provider: ollama, model: llama3, base_url: http://host.docker.internal:11434 }
  setup: pip install -e .
  test:  pytest tests/
  lint:  ruff check .
  smoke:                        # end-to-end examples: app as a whole
    - { input: examples/case1.yml, expect: examples/case1.sql }
    - { input: examples/case2.yml, expect: examples/case2.sql }
  merge:
    auto: false   # set true only on a smoke-tested repo, after the graduation gate
  failure:
    primary_attempts: 3
    escalation_attempts: 1
  ```
- **Config validation:** on a malformed or partial `.aorc.yml`, **fail closed with a clear error** rather than running on guessed defaults. Missing required fields (setup/test) block the build pipeline for that repo (see Install-time behavior).
- **Missing `smoke:` block:** run the pipeline normally and **skip reviewer gate 2** (smoke), but **permanently disqualify that repo from auto-merge** until smoke tests exist. You still get PRs to review manually; the system never unattended-merges a repo it cannot whole-app-verify. Warn once in the config PR. **[B27]**
- **Auto-merge graduation criteria (deferred — v1 ships `auto: false`).** `merge.auto` is not a bare on/off switch. Before it may be enabled for a repo, a measurable confidence gate must be satisfied and recorded. Proposed gate (finalized before the feature is ever enabled): tests pass AND reviewer approves AND the repo has smoke tests AND has accrued a threshold of consecutive human-approved AORC merges (≥ N) with zero post-merge reverts or attributable follow-up bug-fix PRs. Never auto-tuned by the agents themselves.
- All LLM providers are accessed via the OpenAI-compatible API contract.
- **Local-LLM runtime constraint:** a locally running model is only reachable when the orchestrator and its containers run on the *same host as the model* — a self-hosted runner or VM. On GitHub-hosted (cloud) runners, `host.docker.internal` resolves to the cloud runner, not the user's machine, so local models are **not** reachable there. Hosted providers work on any runtime. A local `base_url` must be paired with a self-hosted runner; the orchestrator surfaces a **clear, fail-fast error** if a local model is configured on a cloud runner (this specific misconfiguration is not treated as a transient provider error).

### LLM Abstraction Layer
- A single `LLMClient` interface wraps all provider interactions. Adapters implement it for Claude (Anthropic SDK), OpenAI, and any OpenAI-compatible endpoint.
- The escalation ladder is: primary model × `failure.primary_attempts` → escalation model × `failure.escalation_attempts` → surface to human.
- Model selection is fully driven by `.aorc.yml`; no model names are hardcoded in orchestrator logic.
- **Provider errors vs. bad output (separate counters).** A provider error (429/500/timeout/connection reset) is categorically different from bad model output (test still red). They must not share a counter, or a flaky network would burn `primary_attempts` and prematurely escalate a solvable issue. A provider error → retry the **same** model with exponential backoff (3 tries: 2s / 8s / 30s); only if backoff exhausts does it count as a real failure and enter the escalation ladder. Exception: connection-refused to a local `base_url` on a cloud runner is not transient — fail fast (see Local-LLM constraint). **[B25]**

### GitHub API Abstraction Layer
- A single `GitHubClient` interface wraps all GitHub operations: read issues, post comments, create/update labels, open PRs, update Projects board, merge PRs.
- All orchestrator logic depends only on this interface, never on the GitHub SDK directly — enabling full unit testing without a real repo.
- **Bot rate-limits.** All posts go through one App identity; GitHub enforces per-app secondary limits, and bursts (the backfill sweep; 5 containers committing at once) can trip a 403/429. On such responses, **wait and retry with backoff, respecting GitHub's `Retry-After` header**; do not count it as a real failure or `agent-block`. Concurrency stays at 5; this is handled as a throttle, in v1. **[B26]**

### Agent Placement (orchestrator-side vs container-side)
- **Orchestrator-side (routing / coordination, no per-issue work container):** Triage, Clarification, Decomposition. These decide *what work exists* and *what runs next*; they are cheap and don't require a build container. A vague or epic issue is handled here without spinning one up.
- **Container-side (per-issue build work):** Design, Tester, Test-critic, Coder, Reviewer. These operate inside a single issue's isolated container.
- A heavy work container is spun up only for an actionable issue ready to build.

### Issue Triage (cheap first-pass; Design is the authoritative gate)
- On each issue event, a triage agent makes a **cheap first-pass** classification: `actionable` or `not-ready`. Triage is a fast, low-cost guess — deliberately **not** the authoritative gate.
- **"Actionable"** = has a **bounded, testable definition of done** (Design can produce a bounded interface and the tester can write a finite pass/fail test set). Defined by *testability*, not by an LLM's judgment of size.
- **Not-ready** issues carry a reason tag for the human only: `vague` (needs clarification) or `epic` (needs decomposition; hints: a `- [ ]` task list or an `epic` label). These tags do not define separate pipelines — both route to the one shared refinement path.
- **Idempotent re-triage:** issues produced by the refinement path (clarified issues, decomposed sub-issues) re-enter triage. Most pass through as actionable in one cheap step; only those still vague or oversized loop again, until every issue is actionable. A misclassification is not fatal — an oversized issue slipping through is caught by the Design gate.

### Actionability Gate (Design is authoritative)
- Triage guesses; **Design decides.** When an issue reaches Design, Design attempts to produce a bounded interface + finite test specs + ordered task list.
- Can bound it → proceeds to the build relay. Cannot bound it (task list explodes, interface can't be pinned down, or low confidence) → **refuses** and returns the issue to the refinement path.
- This two-layer design (cheap fuzzy triage + concrete authoritative Design gate) removes the randomness of "is this actionable?" — the real test is "can Design produce a bounded spec and a finite test set?", an inspectable outcome, not a vibe.

### Clarification Pipeline (Grill-Me)
- Vague issues trigger the grill-me skill, which generates targeted clarifying questions, posted one at a time as GitHub comments by the App bot account.
- Issue is labeled `needs-clarification` (→ Needs Clarification column).
- A webhook listener watches for new comments on `needs-clarification` issues.
- **Who can resume (permission-gated AND content-evaluated).** A reply resumes only if it comes from someone with **write/triage access** (gated on GitHub permission/author-association, not identity — not author-only, not anyone-at-all). And a new comment does **not** blindly resume: the bot re-runs its clarity evaluation on the reply's *content*. Actually answers the open questions → resume. "thanks!" / partial → post the next question / restate what's missing, stay in `needs-clarification`. **[B20]**
- **Timeout (nudge, then block; both windows config).** After window 1 (default 7 days): bot posts one nudge comment. After window 2 (another 7 days): label `agent-blocked` (reason: clarification timed out) and move out of the active waiting list. Set windows to infinity to restore wait-forever behavior. Keeps "Needs Clarification" meaning *actively* waiting on you. **[B21]**

### Epic Decomposition
- Epic issues trigger the design agent to read the epic and produce a PRD; prd-to-issues then creates concrete, scoped sub-issues on GitHub, linked to the parent (with the deterministic idempotency marker).
- Sub-issues re-enter triage and the normal pipeline.
- The parent epic closes automatically via a GitHub Actions rule when all linked sub-issues are closed.

### Dependency Resolution (Design-late, checkpoint model)

**Decompose before sequencing.** Epics are fully decomposed (and sub-issues re-triaged to actionable) *before* any sequencing decision, so every real unit of work is on the board first.

**No clever up-front sequencing.** The orchestrator does **not** predict collisions from issue text or graph guesses before dispatch — those would be redundant, because the authoritative collision check happens *after* Design with real data. Dispatch is deliberately cheap:
- **Skip declared-blocked issues** (`blocked by #X`, or an epic task-list child of an unfinished parent). Declared dependencies are the one reliable up-front signal and are always honored.
- **Respect the parallel limit** (dispatch up to the concurrency ceiling; the rest wait).

**The real collision check happens at the container checkpoint.** Design runs first inside each container and emits the exact files/functions the issue will touch. The container then **stops at a checkpoint** — it does not proceed to the tester/coder — and reports its exact file list to the orchestrator. Because Design ran just before build, this reflects the current codebase (no staleness).

**The orchestrator decides, not the container.** The container is isolated and cannot see other in-flight work, so it never decides ordering — it reports and waits. The orchestrator compares this issue's file list against the **collision set** and returns a verdict:
- **Proceed** — no overlap → container continues to Tester → Test-critic → Coder → smoke → Reviewer → PR.
- **Hold** — overlap → container **torn down**, issue **re-queued** for later; Design work is **preserved** on the branch so it resumes from the checkpoint without re-running Design.
- **Conservative default:** when overlap is uncertain, hold.

**Collision set (scope).** "In-flight" means active containers **plus open, unmerged AORC PRs** — an open PR occupies its files even though no container is running, and two such PRs could otherwise pass clean and collide at merge (self-inflicted rollback). Open PRs are treated exactly like a live container's reported file list. **[A3]**

**Collision computation.** Hold if **either** the exact file-path sets intersect **or** Graphify shows one issue's files sit in the other's import/call blast radius. Not directory-prefix (too coarse — would false-collide unrelated files in a folder and destroy parallelism); not exact-path-only (too narrow — misses `compiler.py` importing a changed `parser.py`). If a Graphify query fails/times out or overlap is uncertain → hold. **[B19]**

**Design is side-effect-free outside its own branch**, because a container may be torn down at the checkpoint. Design may read the codebase, query the graph, and commit to its own issue branch, but must not alter the shared repo or other issues before the checkpoint clears — so tearing down a held container is always safe.

**Stateless collision bookkeeping.** Each container's reported file list is written to GitHub (labels/comments). On every wake the orchestrator re-reads these and rebuilds a throwaway collision picture from scratch — no separate state file. (This issue-to-file collision graph is distinct from the Graphify function-call graph.)

### Parallel Execution
- Dispatched issues run as parallel GitHub Actions jobs, one issue per job, up to the concurrency ceiling.
- Each job runs in a Docker container from a pre-baked base image (Claude Code + skills + MCPs installed).
- **Container flow with checkpoint:** Design → **checkpoint (report exact file list, wait for proceed/hold)** → Tester → Test-critic → Coder (bounded fix loop) → smoke test → Reviewer → PR. The container never proceeds past the checkpoint without an orchestrator "proceed."
- A git worktree is created per issue; each container works on its own worktree.
- On a **hold** the container is torn down and the issue re-queued (Design preserved). On completion the container is destroyed. One container = one issue; nothing carries over.

### Pipeline Stage Agents
- Each stage is a fresh agent with scoped context. Context is never carried across stages.
- Stage inputs:
  - **Design agent**: issue body + clarification Q&A + relevant repo files + Graphify blast-radius queries. Output: the strict design-doc schema (interface, test_specs, task_list, files, confidence).
  - **Tester agent**: design doc (interface + test_specs + task_list). Writes one failing test per task, against the design's interface only.
  - **Test-critic agent**: tests + design doc. Verifies the tests match the design and reference only functions in the design's interface, *before* the coder runs.
  - **Coder agent**: design doc + task_list + failing test output (pass/fail + error messages only, no test source) + repo files it must modify. Executes the task list in order.
  - **Reviewer agent**: design doc + original issue + full diff of the branch. Runs on a **distinct model slot** from the coder (the escalation-tier slot), so it doesn't share the coder's blind spots. **[B14]**

### Design Doc Format (strict schema)
The design doc is a **strict machine-readable schema (YAML/JSON), not freeform** — the single biggest determinism lever, since the tester and coder must consume it identically every time. Required fields: **[B12]**
- `interface` — functions with `name` / `inputs` / `outputs`
- `test_specs` — behaviors to test
- `task_list` — ordered implementation steps (one item for a trivial issue; several for a larger one; the coder executes them in order without reordering)
- `files` — exact files touched (drives the checkpoint: the orchestrator reads this field)
- `confidence` — flag

**Invalid schema routing (mechanical discriminator).** **[B13]**
- **Parses but low `confidence`** (genuinely too vague to bound) → `needs-clarification` (the Actionability Gate working as intended).
- **Fails to parse / missing required fields** (a *format* miss, not a comprehension miss) → **retry the design agent N times**, then `agent-blocked` — **not** `needs-clarification` (nothing was unclear; pinging you to "clarify" would be the wrong signal).
- The discriminator is mechanical ("does it satisfy the schema?"), so no LLM judgment routes it.

### Merge-Time & PR Handling
- **Merge conflict at PR-open.** When the branch is ready but main has moved: attempt a rebase. **Clean rebase** (main changed files this issue didn't touch) → do it automatically, re-run the reviewer against the rebased result, open the PR — no human needed. **True conflict** (same lines edited) → `agent-blocked` (reason: merge conflict with main); the coder must not freelance a resolution. Discriminator is mechanical (git reports conflict or not); the widened collision check makes this rare, so it's a backstop. **[B17]**
- **Stale approved PR.** When any PR merges, every open AORC PR whose file set overlaps the merged one is rebased/re-tested against the new HEAD and the reviewer re-runs. Still passes → stays approved, waiting for you. Broke → back into the bounded fix loop. Never auto-close as superseded; never blindly leave stale. **[B16]**
- **Human PR feedback (in v1).** The agent watches its own PR for your review comments and acts on them, routed mechanically by intent: a comment about **code** ("wrong logic", "handle this edge case") → coder re-enters the fix loop (tests stay locked); a comment implying the **spec/tests are wrong** → kick back to design/tester (where changing spec+tests is legitimate), then re-run forward. The coder never edits a locked test. Requires a PR-comment webhook and distinguishing *your* comments from the reviewer-agent's own (to avoid self-triggered loops). **[B22]**

### Auto-Rollback vs In-Flight Containers
- When a revert lands (a merged PR broke main), compute the reverted PR's file set. For each in-flight container, compare against its already-reported checkpoint `files`: **overlap** (direct or Graphify blast-radius) → tear down + re-queue (Design preserved; re-runs against corrected HEAD); **no overlap** → continue; **no checkpoint reached yet** → treat conservatively as kill + re-queue (can't prove non-overlap). **[B18]**

### Failure Handling: Branch Naming & Cleanup
- Branch name: `aorc/issue-<number>` (deterministic, greppable; lets the orchestrator reconstruct branch↔issue after a crash with no DB). One branch per issue, reused across re-dispatches (a held-then-resumed issue returns to the same branch with Design intact). **[B24]**
- Cleanup, three fixed cases (no agent judgment): **merged → delete** (work is in main); **`agent-blocked` → keep** (holds design doc, failing tests, partial code for investigation); **held → keep** (will resume).

### Install-Time Behavior
- On install: create the board; **triage/clarify/decompose** (orchestrator-side, minimal config) run immediately so the backlog starts organizing itself — including the first-run backfill sweep of all existing open issues.
- Any issue that reaches **dispatch** is **held** (`blocked: awaiting-config`) until the `.aorc.yml` PR is merged, then released. **Never** run the build pipeline against baked-in defaults (guessing `pytest`/`pip install`) — that's the toolchain-hallucination failure the config file exists to prevent. This makes the config PR self-motivating (issues visibly pile into "awaiting config"). State in the install-PR text that the shown defaults are a template and the pipeline won't build until merged. **[B23]**

---

## Safety & Validation

These rules exist because AORC runs unattended and the operator may not be able to verify code correctness by reading a diff. Correctness is enforced by the system, not by human review.

1. **Interface ownership (design agent, not tester).** The design agent defines every function signature (name, inputs, outputs) in the design doc. Tester and coder use only those signatures; the tester must not invent functions the design doc did not specify. The design doc is the single source of truth for the contract.

2. **Test-critic agent.** A separate critic reviews the tester's tests against the design doc *before* the coder runs, rejecting any test referencing a function not in the design or not corresponding to the spec. A different agent from the tester, so no incentive to pass its own work.

3. **Tests are locked during the fix loop.** Once the tester's tests (and the smoke tests) are written and committed, they are read-only. The fix-loop container mounts test files read-only. Prevents turning a red test green by weakening/deleting/skipping it instead of fixing the code.

4. **Red vs. error distinction.** The orchestrator distinguishes a clean assertion **failure** ("red" — referenced code doesn't exist yet or returns the wrong value; expected in TDD → proceed to coder) from a test **error/crash** (broken import, reference to a function not in the design interface, malformed test → stop, return to tester/design). Only clean red proceeds. This lets tests reference not-yet-written code safely without masking real errors.

5. **Coverage floor — two checkable gates at two times** (the single-gate "measure coverage before the coder runs" is impossible, since the code doesn't exist yet): **[A2]**
   - **Interface-coverage — BEFORE the coder.** Every function declared in the design doc's `interface` must be referenced by at least one test. A pure static set-comparison (design functions ⊆ functions-under-test); no execution, no coder needed. This guards against a shallow suite that silently skips part of the spec.
   - **Line-coverage — BEFORE merge.** Once tests pass and code exists, run a coverage tool and assert the PR's changed lines meet the floor (default 80%, config). This is reviewer gate 3.

6. **End-to-end smoke test.** `.aorc.yml`'s `smoke:` field lists known input→output examples for the app as a whole. Before any merge, the full suite **plus** the smoke tests must pass. Smoke verifies real outputs (given input X → output Y), not merely that the app started. Smoke tests are locked during the fix loop like unit tests. Catches a PR that passes its own tests but breaks the app elsewhere. (If no `smoke:` block: run, skip this gate, and hard-disqualify the repo from auto-merge.)

7. **Bounded fix loop.** When the pre-merge suite is red, the coder's loop may modify application code only (never tests). It runs until green **or** the escalation cap is reached (`failure.primary_attempts` on primary, then `failure.escalation_attempts` on escalation). At the cap → `agent-blocked` + human summary. No unbounded loop. (Provider errors are handled separately with backoff and do not consume this counter.)

8. **Design-agent refusal (Actionability Gate).** The design agent emits a confidence flag. Below threshold → the issue returns to the refinement path (`needs-clarification`) rather than being pushed forward. The design agent may refuse a still-unbounded issue instead of proceeding on assumptions.

9. **Auto-rollback.** After a merge, if main's test suite goes red, the orchestrator automatically reverts the offending PR (an Actions workflow on push to main). Worst case becomes a brief, self-healing breakage rather than a persistent one. (In-flight containers are handled per "Auto-Rollback vs In-Flight Containers.")

10. **Credential scrubbing.** Tokens are passed as environment variables, never embedded in prompts or expected in output. All agent-posted content is regex-scrubbed for credential patterns (battle-tested list; GitHub *and* LLM-provider prefixes) before it reaches GitHub. Fully deterministic — no LLM judgment. (Detailed in the Credential & Token Model.)

---

## Testing Decisions

### What Makes a Good Test
Tests verify external behavior only — what goes in and out across the `LLMClient` and `GitHubClient` interfaces. Internal implementation details must not be asserted. Tests read as specifications: given this GitHub event, expect these GitHub API calls and this label state.

### Modules to Test

**Triage module** — Input: issue JSON → Output: `actionable` | `not-ready` (+ reason tag). Mock `LLMClient` for controlled vagueness. Edge cases: task list but no epic label, epic label but no task list, empty body.

**Clarification module** — Input: vague issue + Q&A thread → Output: comment posted, label applied, resume-or-restate decision. Mock `GitHubClient`. Test: question posted when vague; resume only on a permissioned reply that actually answers; next question posted on partial reply; nudge at window 1; block at window 2.

**Collision checker (post-Design)** — Input: reporting issue's file list + other in-flight file lists + open-PR file lists + mock Graphify → Output: `proceed` | `hold`. No LLM/GitHub mocking for pure overlap logic. Test: overlapping sets → hold; disjoint → proceed; graph-connected blast radius → hold; uncertain/graph-timeout → hold; an open unmerged PR's files are in the set.

**Dispatch selector (up-front)** — Input: actionable issues + declared `blocked-by` + parallel limit → Output: batch to dispatch now. Test: declared-blocked not dispatched until blocker closes; batch never exceeds the limit; an epic is never dispatched before decomposition.

**Pipeline state machine** — Input: label events → Output: correct next stage + board column derived from label. Mock `GitHubClient`. Test every label transition; test artifact-presence check for crash recovery; test webhook double-fire is a no-op.

**Design-schema validator** — Input: design agent output → Output: valid | parse-fail | low-confidence, routed correctly. Pure schema check, no LLM. Test: valid → proceed; parse-fail → retry-then-block; low-confidence → needs-clarification.

**LLM abstraction layer** — Input: provider config + prompt → Output: completion. Integration tests hit real providers; unit tests mock at the `LLMClient` boundary. Test: Claude, OpenAI, and local Ollama adapters satisfy the same contract; provider-error backoff does not consume the failure counter.

**Failure escalation** — Input: stage failure signal → Output: N primary retries → 1 escalation → `agent-blocked` + comment. Mock `LLMClient` to fail controllably. Test: exact retry counts; provider-error vs bad-output separation; failure comment contains error detail.

**Cost & compute guards** — Input: simulated token spend / elapsed time → Output: clean-stop at stage boundary, 1.5× overshoot ceiling, per-run pause, timeout kill. Test each threshold fires and lands the issue in `agent-blocked` with the right reason.

### Prior Art
Greenfield codebase. Test structure follows the two-seam model: all orchestrator logic tests mock `LLMClient` and `GitHubClient`; only adapter tests hit real external services.

---

## Out of Scope

- **Knowledge graph / Graphify build**: consumed as an existing tool via its MCP interface, not rebuilt. The shared graph DB is orchestrator-owned (one per repo, re-indexed per merge) and is the one standing piece of infrastructure; agents query it read-only.
- **Custom CLI or GUI**: GitHub Projects board is the only UI in v1.
- **Multi-repo dependency resolution**: v1 operates within a single repo.
- **Issue creation from scratch**: AORC processes existing issues (and sub-issues it decomposes); it does not invent top-level issues without a human-authored parent.
- **Auto-merge in v1**: human merge gate required; auto-merge is config-gated, disabled by default, and requires the graduation gate + smoke tests.
- **Cost *reporting / analytics***: v1 does not produce token-cost reports or dashboards. **Note:** hard cost *caps* (per-issue/per-run/daily circuit-breakers) **are in v1** — see Operational Limits. Only budgeting-as-reporting is out of scope.
- **Agent memory across issues**: agents are stateless per stage; no persistent agent memory between issues.
- **Multi-tenant atomic dedup**: the DB-backed compare-and-swap that would close the residual webhook-race is deferred; not needed at this scale.
- **Per-repo concurrency limits / higher ceilings**: relevant only after moving off local hardware to cloud.
- **Non-GitHub issue trackers** (Linear, Jira, etc.): out of scope. GitHub Issues only.

---

## Further Notes

- **Evaluation target**: the first target is a private YAML→SQL semantic-layer repository (~104 open issues spanning bugs, features, and epics). Success criterion: AORC closes every actionable issue with merged, tested PRs, unattended.
- **Sandbox-first rule**: AORC is validated on a disposable repository owned by the operator before it is ever pointed at any real or organizational repository, and runs under the scoped-token model at all times.
- **Core hypothesis already validated**: manual runs confirm a single Claude Code agent can close an evaluation-target issue end-to-end. AORC is the orchestration layer around a proven primitive.
- **Build discipline**: no component is built until the evaluation target proves it's needed. Graphify, parallel dispatch, and epic decomposition are included because the issue mix (epics, clusters, ~104 items) makes them necessary from the start — not as speculation.
- **`.aorc.yml` as install artifact**: the file serves double duty — repo configuration and proof of AORC installation; its presence signals AORC is active.

---

## Tunable Constants (set conservative, tune on real data)

| Constant | Starting value | Section |
|---|---|---|
| Per-issue cost cap | $3–5 | Operational Limits |
| Per-run cost cap | $50 | Operational Limits |
| Daily cost cap | $100/day | Operational Limits |
| Single-stage cost overshoot ceiling | 1.5× per-issue cap | Operational Limits |
| Container wall-clock timeout | 30–45 min | Operational Limits |
| Runner size | ~2 CPU / 7 GB (default) | Operational Limits |
| Concurrency ceiling | 5 (global) | Operational Limits |
| Line-coverage floor | 80% | Safety rule 5 |
| Design-schema retry count (N) | (set on real data) | Design Doc Format |
| Provider-error backoff | 3 tries: 2s / 8s / 30s | LLM Abstraction |
| Clarification nudge window | 7 days | Clarification |
| Clarification block window | +7 days | Clarification |
| Cron backstop interval | 10–15 min | Liveness |
| `failure.primary_attempts` / `escalation_attempts` | per `.aorc.yml` | LLM Abstraction |

---

## Decision-Provenance Index

Every resolved gap, and the section that now contains it. (A = corrections to earlier drafts; B = design decisions.)

| ID | Topic | Now lives in |
|---|---|---|
| A1 | Column mapping (vague→Needs Clarification; label is truth) | State Machine; Story 14 |
| A2 | Coverage floor (interface-cov pre-coder + line-cov pre-merge) | Safety rule 5 |
| A3 | Collision-set includes open unmerged PRs | Dependency Resolution → Collision set |
| B1 | Cost circuit-breaker (per-issue/run/daily) | Operational Limits |
| B2 | Container compute limits (timeout, runner size) | Operational Limits |
| B3 | Concurrency ceiling (5, global, config) | Operational Limits |
| B4 | Cost-cap mid-issue clean-stop + 1.5× ceiling | Operational Limits |
| B5 | Held-issue wake (merge-webhook + cron) | Liveness |
| B6 | Webhook dedup (key + artifact) | Liveness |
| B7 | First-run backfill = re-sync | Liveness |
| B8 | Decomposition idempotency (tag + skip) | Liveness |
| B9 | Decomposition depth (no cap; diagnostic label) | Liveness |
| B10 | Secret scrubbing (env + regex incl. LLM keys) | Credential Model |
| B11 | Token expiry (tear-down-and-resume) | Credential Model |
| B12 | Design doc strict schema | Design Doc Format |
| B13 | Invalid schema routing | Design Doc Format |
| B14 | Reviewer four gates + distinct model | Safety rules 5/6 + Stage Agents |
| B15 | Test-gaming (reviewer stack sufficient) | Safety (rules 2/3/34/37/6) |
| B16 | Stale approved PR (reviewer re-runs) | Merge-Time & PR Handling |
| B17 | Merge conflict at PR-open | Merge-Time & PR Handling |
| B18 | Rollback vs in-flight (kill overlapping) | Auto-Rollback vs In-Flight |
| B19 | Collision computation (path OR Graphify) | Dependency Resolution |
| B20 | Clarification resume (permission + content) | Clarification |
| B21 | Clarification timeout (nudge then block) | Clarification |
| B22 | Human PR feedback in v1 | Merge-Time & PR Handling |
| B23 | Pre-config install (triage runs, build holds) | Install-Time |
| B24 | Branch naming & cleanup | Failure Handling: Branch Naming |
| B25 | Provider errors (backoff-then-escalate) | LLM Abstraction |
| B26 | Bot rate-limits (backoff on 403/429) | GitHub API Abstraction |
| B27 | Missing smoke block (run, skip gate 2, block auto-merge) | `.aorc.yml`; Safety rule 6 |
