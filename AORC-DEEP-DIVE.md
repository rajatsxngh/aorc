# AORC — The Deep Dive

*A complete, code-accurate account of the AORC codebase: what every file
does, how the pieces are wired together, why the architecture is shaped the
way it is, and a step-by-step dry run from an empty repo to a merged PR.*

**How to read this.** Every factual claim points at a real file (usually
`file.py:line`) so you can verify it in the source. Nothing here is inferred
from the docs alone — the whole of `src/aorc/` (~7,700 lines), `ui/`,
`ralph/`, the `Dockerfile`, `pyproject.toml`, the PRD, and the slice files
were read to write it. Where the code and the documentation disagree, the
code wins and the disagreement is called out (Part VIII).

**Companion documents.** `AORC-PRD-v1-FINAL-tagged.md` is the specification
and carries the decision log (IDs `A1–A3`, `B1–B27`, referenced throughout
as **[B23]** etc). `issues/README.md` is the slice map (S1–S25 plus the
open list). `issues/done/` holds one file per shipped slice. `CLAUDE.md` is
the operator's quick reference. `HOW-IT-WORKS.md` is a shorter tour of the
same ground.

---

## Table of contents

- [Part 0 — Orientation](#part-0--orientation)
- [Part I — How this codebase was built (and why that shows)](#part-i--how-this-codebase-was-built-and-why-that-shows)
- [Part II — The twelve architectural decisions](#part-ii--the-twelve-architectural-decisions)
- [Part III — Every file, explained](#part-iii--every-file-explained)
- [Part IV — The wiring: `compose()` in detail](#part-iv--the-wiring-compose-in-detail)
- [Part V — The dry run](#part-v--the-dry-run)
- [Part VI — Reference tables](#part-vi--reference-tables)
- [Part VII — The test strategy](#part-vii--the-test-strategy)
- [Part VIII — Gaps, findings, and honest status](#part-viii--gaps-findings-and-honest-status)
- [Appendix — PRD decision → code traceability](#appendix--prd-decision--code-traceability)

---

## Part 0 — Orientation

### What AORC is

AORC (Autonomous Orchestrated Repo Contributor) is a GitHub App plus a small
Python CLI that drives an issue from "opened" to "review-approved PR" with
no human in the loop except two actions:

1. **Answer clarifying questions** the system posts as issue comments.
2. **Click Merge** on the finished PR.

Everything between — triage, interviewing the author of a vague issue,
decomposing epics into sub-issues, designing a solution, writing failing
tests, implementing until they pass, reviewing the diff, opening the PR,
rebasing when main moves, reacting to a broken main — is done by the system,
unattended.

The problem it attacks is stated in the PRD: backlogs are permanent not
because the work is hard but because *human attention at every handoff* is
the bottleneck. AORC removes the handoffs, and — critically — assumes the
operator **cannot verify correctness by reading the diff**. Therefore
correctness, safety, cost control, and liveness must be enforced by the
system, not by human review. That assumption is the source of nearly every
design decision in Part II.

### The 60-second mental model

```
                        ┌──────────────── GitHub is the only database ─────────────────┐
                        │  labels = stage · committed files = proof · comments = memory │
                        │  branch name = identity (aorc/issue-42 ⇄ issue 42)            │
                        └───────────────────────────────────────────────────────────────┘
                                                  ▲
   issue opened                                   │ every wake re-reads ALL of this
        │                                         │
   ┌────▼─────┐   not-ready ┌──────────────┐      │
   │  triage  ├────────────►│ clarify /    │──────┘ (re-enters)
   │ (1 LLM)  │             │ decompose    │
   └────┬─────┘ actionable  └──────────────┘
        │
   ┌────▼──────────────────────────────────────────┐
   │ dispatch gate: config merged? blockers? slots? │
   └────┬──────────────────────────────────────────┘
        │  mint 1-hour single-repo token · git worktree · docker container
   ┌────▼─────┐   ┌────────────┐   ┌───────┐   ┌────────┐   ┌──────────┐
   │  DESIGN  ├──►│ CHECKPOINT ├──►│ TESTS ├──►│  CODE  ├──►│  REVIEW  ├──► PR
   │ JSON spec│   │ collision? │   │must be│   │ ≤3 tries│   │gates+LLM │
   └──────────┘   └─────┬──────┘   │ "red" │   └────────┘   └──────────┘
                        │ hold     └───────┘
                        ▼
                   aorc-held ──(next merge/cron sweep)──► retry
   any hard failure anywhere ──► agent-blocked + one comment telling the whole story
```

### The one-line philosophy

> **LLMs propose; plain code disposes.**

Everywhere an LLM produces output, a *mechanical* gate — a JSON schema
check, a set comparison, a process exit code, a regex, git's verdict —
decides what happens next. No LLM ever routes its own output, grades its own
work, or declares itself finished. Concretely:

- Design output is valid or not by `json.loads` + a required-field check
  (`design.parse_design_response`), and *routing* on invalid is a lookup
  table, not a judgment.
- "Did the tests fail correctly?" is answered by string markers in pytest's
  output (`tester.classify_test_run`), not by asking a model.
- "Did the rebase conflict?" is git's exit code (`gitops.LocalGitOps.rebase`).
  The agent never resolves a conflict.
- "Is this text a secret?" is a regex set drawn from gitleaks/trufflehog
  (`credentials.scrub`) — never a model's opinion.

The result: the system is deterministic everywhere determinism is possible,
and LLM calls are quarantined to the places where *generation* is the actual
job (design, tests, code, review, and two small classifiers).

### Suggested reading order for the source

1. `src/aorc/interfaces.py` — the two seams and the data types. The
   constitution; everything else obeys it.
2. `src/aorc/pipeline.py` — the label state machine (~150 lines, all pure).
3. `src/aorc/__main__.py` — the composition root: what gets built and wired.
4. `src/aorc/driver.py` — the four stages in sequence, with the resume rules.
5. `src/aorc/design.py` → `tester.py` → `coder.py` → `reviewer.py` — the
   stages themselves, in pipeline order.
6. `src/aorc/wake.py` + `install.py` — the loop, the gate, the event table.
7. `src/aorc/harness.py` + `credentials.py` — isolation and the token model.
8. `src/aorc/merge.py` — everything that happens around a merge.

---

## Part I — How this codebase was built (and why that shows)

This is the deepest "why is the code like this" answer, and it isn't in the
PRD: **AORC was built by an autonomous agent loop, in a Docker sandbox, one
vertical slice per iteration.** The harness is in the repo — `ralph/` plus
`run-sandbox.sh` and the root `Dockerfile`.

`ralph/afk.sh` is the loop: for N iterations it feeds Claude Code the open
issue files (`issues/*.md`), the last five commits, and `ralph/prompt.md`,
lets it do exactly one task, and then applies **script-enforced gates** that
the agent cannot talk its way past:

```bash
head_before=$(git rev-parse HEAD)          # before the agent runs
… claude --print --permission-mode acceptEdits --allowedTools "Read,Edit,Write,Bash"
head_after=$(git rev-parse HEAD)
[ "$head_before" == "$head_after" ] && exit 1     # no commit → the loop STOPS
pytest -q || exit 1                               # red tests → the loop STOPS
```

`ralph/prompt.md` supplies the policy: work one slice at a time, respect the
`Blocked by` field, never start a blocked slice, use TDD (write the failing
test first, watch it fail for the right reason), never commit red code, make
exactly one commit per iteration recording *which slice and what decisions*,
and move the issue file to `issues/done/` when finished. Two rules are
repeated verbatim in the prompt because they are the architecture: the two
seams, and no hardcoded models or secrets. `run-sandbox.sh` runs the whole
loop inside a container with only the project directory mounted and only an
Anthropic key in `.env` — no org credential exists, so the agent physically
cannot reach a real repo during development (the script even documents the
"hard network wall" upgrade path for when real GitHub access enters).

**Why this matters for reading the code.** Five properties of the codebase
are direct consequences of the build method:

1. **Docstrings carry provenance.** Almost every module opens with its slice
   number and rationale, and fixes cite the *live* issue that motivated them
   ("live issue 21", "live issues 24/25", "live issues 66/67", "S35: before
   this, repo_dir='.' made every worktree a checkout of AORC itself"). The
   code is its own decision log because each iteration had to justify itself
   in one commit, and the next iteration only saw the last five commits plus
   the code. **Comments are the memory the loop didn't otherwise have.**
2. **Invariants are pinned by tests, not conventions.** An agent cannot be
   trusted to remember "don't import the SDK" across dozens of iterations, so
   `tests/test_no_sdk_imports.py` fails the build if core code ever does. The
   architecture is enforced by the same gate that enforces behavior.
3. **Zero-dependency unit suite.** The loop's gate is `pytest -q` inside a
   fresh container. Every third-party dependency is a way for that gate to
   break for reasons unrelated to the code, so the whole suite runs against
   in-memory mocks with no SDKs, no network, no env vars — and
   `pyproject.toml` declares `dependencies = []` with every SDK in an
   optional extra.
4. **Vertical slices, not layers.** The dependency graph in
   `issues/README.md` (S1 seams → S2 labels → S4 harness → S5–S8 the build
   spine → S10 collision → S16 liveness → S17 merge-time → S18 install)
   means every iteration ended with something that *worked end to end at its
   scope*, which is why the pipeline can be read top-to-bottom today.
5. **Honest gap-tracking.** Because the loop shipped one slice at a time,
   "built but not wired" was a real and recurring state — and the codebase
   names it rather than hiding it. Several slices exist *only* because a
   previously "done" component turned out to be cosmetically wired: S27 (the
   container was started but the toolchain ran on the host), S35 (worktrees
   were checkouts of the wrong repo), S43 (the driver checked an empty
   collision registry). Part VIII continues that tradition with three
   findings from this review.

The numbered slices ran past the original 18: S21–S25 are the "go-live glue"
(entry point, driver, real token minter, webhook receiver, Actions wiring)
added after a readiness review found that every v1 component existed but
nothing could actually run; S26–S49 are fixes discovered by pointing the
thing at a live sandbox repo. Docstrings reference slices up to **S49**,
while `issues/done/` holds 27 files — the later fixes were folded into
commits rather than issue files (see `git log`).

---

## Part II — The twelve architectural decisions

Each decision below is (a) stated, (b) justified, and (c) shown to be
*enforced in code* rather than merely documented.

### 1. Exactly two seams (architecture invariant #1)

**What.** Orchestrator logic depends only on two abstract base classes in
`src/aorc/interfaces.py`:

- `GitHubClient` — ~25 methods covering issues, comments, labels, PRs,
  branches, file contents, and the Projects board.
- `LLMClient` — one method, `complete(messages, max_tokens, temperature) ->
  Completion`, plus a `model` property.

No module in the core imports PyGithub, `anthropic`, or `openai`. Real SDKs
live behind adapters (`github/sdk_adapter.py`, `llm/claude_adapter.py`,
`llm/openai_adapter.py`, `graphify_adapter.py`), and each imports its SDK
**lazily inside a method** so even importing the adapter module doesn't
require the package installed (`sdk_adapter._client`, `github/sdk_adapter.py:174`).

**Why.**
- *Testability.* The entire pipeline runs against in-memory doubles. The
  unit suite (28 files in `tests/`) needs zero third-party packages, zero
  network, zero environment variables — and `tests/test_main.py` drives all
  five CLI subcommands end to end that way.
- *Provider independence.* `.aorc.yml` can point a model slot at Claude,
  OpenAI, a gateway, or local Ollama; nothing in the core changes.
- *A clean failure vocabulary.* Adapters translate SDK exceptions into three
  seam-level errors (`ProviderError`, `FailFastProviderError`,
  `GitHubRateLimitError`), so orchestrator logic routes failures
  mechanically without ever touching an SDK exception type.

**Enforced by.** `tests/test_no_sdk_imports.py`. Also note the deliberate
boundary of the rule, written into `harness.py`'s docstring: shelling out to
**git** and **docker** does *not* cross it. Git is the substrate the product
operates on and Docker is the isolation mechanism; neither is a provider SDK
that could lock the design in.

### 2. GitHub is the database (the stateless orchestrator)

**What.** No database, no state file, no long-running process (except the
optional webhook listener). All durable state lives in GitHub:

| GitHub primitive | Stores | Code |
|---|---|---|
| **Labels** | Pipeline stage (`in-design`→`in-test`→`in-code`→`in-review`), terminal states (`needs-clarification`, `agent-blocked`), auto-releasable queues (`aorc-held`, `awaiting-config`) | `pipeline.py:26–45` |
| **Committed files** | Stage completion proof: `aorc/issue-N/design.md`, `aorc/issue-N/tests.marker`, an open PR | `pipeline.ArtifactChecker:86` |
| **Hidden HTML comments** | Idempotency keys (`<!-- aorc:event issue=5 stage=x sha=y -->`), one explanation per hold/block, sub-issue lineage (`aorc:parent=N` / `aorc:sub-index=I`), handled-feedback markers | `wake.py:99`, `driver.py:78`, `decomposition.py:45`, `merge.py:170` |
| **Branch names** | Identity. `pipeline.branch_name(42) == "aorc/issue-42"` and `wake.issue_for_branch` is its exact regex inverse — a merged PR maps back to its issue with no lookup table | `pipeline.py:48`, `wake.py:86` |

Every wake rebuilds the working picture from scratch: `wake.rebuild_state`
(`wake.py:154`) re-reads all open issues and buckets them held / in-pipeline
/ backlog, and `design.rebuild_in_flight_registry` (`design.py:175`)
reconstructs the collision registry by reading each in-flight issue's
*committed design doc*.

**Why.**
- *Crash safety falls out for free.* The process exits after every command.
  Kill it anywhere; the next wake reconstructs state. There is no "corrupted
  state store" failure mode because there is no store.
- *The label alone is never trusted.* A crash between "label moved" and
  "artifact committed" is the classic distributed-state bug.
  `PipelineStateMachine.advance` (`pipeline.py:138`) **refuses** to advance
  past a stage whose artifact is missing, and `resume_stage`
  (`pipeline.py:125`) re-derives the true resume point from labels *and*
  artifact presence.
- *Human legibility.* The system's entire memory is visible in the GitHub
  UI. Every hold, block, and dedup marker is a comment a human can read.
- *No infrastructure* (PRD user story 5): install an App, maintain nothing.

**The one honest exception.** `WakeLoop.in_flight` — a dict of live
container handles + tokens — is process-local, because a Docker handle
cannot be re-read from GitHub. It dies with the process, and the design
absorbs that: those issues are simply re-dispatched by the next sweep and
resume from their last committed artifact.

### 3. Config over code, failing closed (architecture invariant #2)

**What.** No model names, no toolchain commands, no secrets in code.
Everything comes from `.aorc.yml` (parsed in `config.py`) and the
environment variables it references (`$VAR` / `${VAR}` expansion,
`config.py:97`; a referenced-but-unset variable is itself a fatal error).
Anything malformed raises `ConfigError` and the CLI exits 1 before a single
collaborator is constructed (`__main__.run:391`).

**Why.** The failure being prevented has a name in the PRD: **toolchain
hallucination**. An agent guessing "probably `pip install -e .` and
`pytest`?" for an unfamiliar repo produces confidently wrong builds that
look like real ones. `.aorc.yml` is the repo owner's signed statement of the
real toolchain, and the system prefers doing nothing to guessing. The
principle runs deeper than the parser:

- The build pipeline refuses to run at all until `.aorc.yml` with `setup`
  and `test` is merged **to main** — issues pile up under `awaiting-config`
  (`install.ConfigGate:229`, **[B23]**). This makes the config PR
  self-motivating: the queue visibly fills.
- The gate re-reads the file from main *through the GitHub seam* on every
  dispatch — stateless, like everything else.
- Even the generated rollback workflow reads `setup`/`test` out of
  `.aorc.yml` at run time rather than baking commands in
  (`install.ROLLBACK_WORKFLOW:179`).
- `runner:` may only be `"default"`; the parser hard-rejects opting into a
  larger GitHub runner (`config.py:215`, PRD B2).
- Auto-merge requires *both* the `merge.auto` opt-in **and** a `smoke:`
  block: `config.auto_merge_allowed` (`config.py:88`) permanently
  disqualifies a repo AORC cannot whole-app-verify (**[B27]**). Any future
  auto-merge path must consult that function, never `merge_auto` directly.

### 4. Separation of powers between agents

**What.** The stages are played by different LLM roles with deliberately
restricted views — an information-flow architecture, not a prompt
convention:

| Agent | Sees | Never sees | Why |
|---|---|---|---|
| **Design** (`design.py:197`) | Issue body, clarification Q&A, current contents of files the issue names, optional blast radius | Other issues, other stages' outputs | Fresh scoped context per stage (PRD story 28). Its output becomes the single contract downstream |
| **Tester** (`tester.py:373`) | Design's `interface` + `test_specs` + the derived implementation module name | `task_list`, repo files, implementation code | Tests must encode *behavior*, not mirror implementation. S36 learned this the hard way: keying tests to `task_list` produced "the file was created" tests the critic correctly rejected forever |
| **Test-critic** | The tests + the design doc | — but is a **distinct `LLMClient` instance** | "No incentive to pass its own work" (PRD safety rule 2). Live it is the escalation-tier model |
| **Coder** (`coder.py:148`) | Design's `interface`/`task_list`/`files`, current contents of those files, pass/fail + error output | **Test source code** | Cannot game tests it cannot read (PRD story 34). The generated test path is dropped defensively even if a caller passes it (`coder.py:182`) |
| **Reviewer** (`reviewer.py:116`) | The real unified diff (main vs branch), design doc, original issue | — but is the **escalation-tier slot** (**[B14]**) | Doesn't share the coder's blind spots |

**Why.** With one agent doing everything, self-consistency substitutes for
correctness: the coder that misread the spec writes tests embodying the same
misreading, then passes them. Splitting the roles and starving each of the
information it could cheat with makes the red-green boundary real — tests
are written from the spec *before* implementation exists, are committed,
are proven to fail cleanly, and are then **locked**.

**Enforced by**, not asked for: `coder.parse_coder_response` (`coder.py:88`)
rejects any write to a path outside the design's `files` list, and the
generated test file is never in that list. "Tests are locked" is a schema
constraint, not a runtime convention someone has to remember.

### 5. A mechanical gate at every boundary

The complete inventory, in pipeline order:

| # | Gate | Code | Behavior |
|---|---|---|---|
| 1 | **Design schema** | `design.parse_design_response:74` | Must be JSON with `interface`, `test_specs`, `task_list`, `files`, `confidence`. Routing **[B13]**: parse failure = *format miss* → retry ≤3 → `agent-blocked`; parses but `confidence < 0.5` → `needs-clarification`. The two are routed differently on purpose — pinging a human to "clarify" a formatting bug is the wrong signal |
| 2 | **Interface coverage** | `tester.interface_coverage_gate:276` | Every function in the design's `interface` must be referenced by the test code. Pure static set-comparison, no execution. Half of the PRD's two-part coverage answer (**[A2]**): line coverage is impossible before code exists, so *interface* coverage runs pre-coder and *line* coverage pre-merge |
| 3 | **Critic verdict** | `tester.parse_critic_response:250` | Exactly `approve`/`reject`; a rejection's reason is fed into the tester's retry prompt |
| 4 | **Red/error/green/infra** | `tester.classify_test_run:282` | returncode 0 → `green` (suspicious: passing with no implementation); docker dead-target markers → `infra-fail` (S33: a dead daemon is not a test outcome — hard-stop, never burn retries); ImportError/SyntaxError/`collected 0 items` → `error` (broken tests → retry tester); any other non-zero → **`red`**, the only outcome that proceeds. PRD safety rule 4 |
| 5 | **Coder schema** | `coder.parse_coder_response:88` | One entry per `task_list` item, in order; every `path` ∈ design `files`; non-empty `code` |
| 6 | **Preservation** | `coder.missing_preserved_names:132` | The coder writes *full file contents*, so every pre-existing top-level `def`/`class`/assignment must still exist afterwards. Added in S44 after live issue 29, where the coder rewrote a shared file from a blank slate and deleted its neighbors' functions. Validated for the whole batch **before any write** |
| 7 | **Toolchain** | `coder._run_toolchain:293` | setup → test → lint, all from config, stopping at the first non-zero |
| 8 | **Smoke + coverage** | `reviewer._run_mechanical_gates:226` | Run *before* the reviewer LLM because they are cheaper than an LLM call. Coverage = parse the last `NN%` in the command's output vs the floor |
| 9 | **Rebase discriminator** | `gitops.LocalGitOps.rebase:37` | git says up-to-date / clean / conflict. Clean → re-run every gate against the rebased result. Conflict → `agent-blocked`; **the agent never freelances a resolution** (**[B17]**) |
| 10 | **Collision verdict** | `harness.Checkpoint.verdict:577` | Path intersection or blast radius → `hold`. Uncertainty → `hold` |
| 11 | **Env leak check** | `credentials.assert_env_clean:173` | Any PEM-shaped value in a container env raises before the runtime sees it |

Note what is *not* a gate: nothing asks a model "did that go well?".

### 6. The credential model bounds the blast radius

**What.** The GitHub App's private key is the master credential and lives in
exactly one place — the orchestrator process. Per dispatched issue,
`CredentialBroker.mint` (`credentials.py:128`) produces a **single-repo,
minimal-permission, ~1-hour** installation token through the App-JWT
exchange (`github/app_token.py:78`):

1. Sign an RS256 JWT — `{iss: app_id, iat: now−60s, exp: now+600s}`; the
   60-second backdate tolerates clock drift against GitHub's clock.
2. `GET /repos/{owner}/{repo}/installation` → the installation id.
3. `POST /app/installations/{id}/access_tokens` with
   `{"repositories": [name], "permissions": {...}}` → a `ghs_…` token.

The container receives only that token plus the LLM key, in separate env
slots.

**Why** (PRD "Credential & Token Model", explicitly a *hard safety
requirement*): container isolation alone is insufficient — a sealed
container holding a broad credential can still reach everything that
credential can touch. Isolation and credential-scoping are partners. A
compromised agent is bounded to **one repo for at most one hour**.

**Enforced by**, layer by layer:

- **Permission ceiling.** `mint` raises `PermissionCeilingError` for any
  scope outside `{contents, issues, pull_requests}` or any level above
  `write` — checked *before* the exchange runs, so it is enforcement, not
  advice (`credentials.py:135`).
- **Leak-proof env construction.** `container_env` (`credentials.py:156`)
  raises `CredentialLeakError` if a slot value would contain the broker's
  own key, and `assert_env_clean` re-checks *every* env at the harness
  boundary (`ContainerHarness.dispatch`, `harness.py:625`) for PEM blocks.
  A hand-built env dict therefore cannot smuggle the master credential into
  a container: the broker is structurally the only viable source.
- **No refresh path, deliberately (B11).** An expired token means teardown
  through the branch-preserving path and re-dispatch with a fresh token
  (`credentials.handle_token_expiry:189`, run over every in-flight container
  on every wake at `wake._expiry_pass:411`). A container can never
  re-authenticate because it never had the key. This reuses the checkpoint
  machinery and adds **zero** new security surface.
- **Secret scrubbing, layer two (B10).** `ScrubbingGitHubClient`
  (`credentials.py:213`) decorates the real client **exactly once, at the
  composition root** (`WakeLoop.compose`, `wake.py:254`) and regex-scrubs
  every agent-authored text surface — comments, issue and PR text, committed
  file contents, **and label and branch names** (a secret rides in a name
  just as well as in a body). Patterns come from the gitleaks/trufflehog
  lists: `gh[pousr]_…`, `github_pat_…`, `sk-ant-…`, `sk-…`, PEM blocks.
  Deterministic; no model judges what a secret is.
- **Secrets never in argv.** `DockerContainerRuntime.start`
  (`harness.py:157`) writes env into a 0600 temp file, passes `--env-file`,
  and unlinks it the moment `docker run` returns — because
  `docker run -e KEY=value` is visible to every user on the host via `ps`
  (closed in S19). The Actions runtime solves the same problem differently
  (sealed-box repository secrets, never `workflow_dispatch` inputs, which
  echo in run logs).
- **Separate slots, separate lifetimes.** The ~1h GitHub token and the
  long-lived LLM key never share a slot; rotating one never touches the
  other.

### 7. Dumb dispatch, smart checkpoint (the Design-late collision model)

**What.** AORC does **no** clever up-front collision prediction from issue
text. `dispatch.select_dispatch` (`dispatch.py:61`) checks only the two
signals that are reliable before any work exists: declared blockers
(`blocked by #N` in the body, or an un-decomposed epic) and the concurrency
ceiling (default 5). The authoritative check happens **after Design**, when
the design doc's `files` list is real data. `Checkpoint.verdict`
(`harness.py:577`) holds if the issue's files intersect:

- (a) another in-flight issue's registered claim, or
- (b) an **open unmerged AORC PR's** changed files (**[A3]** — an open PR
  occupies its files exactly like a live container; two approved PRs could
  otherwise pass clean and collide at merge), or
- (c) either direction of the Graphify import/call blast radius (**[B19]** —
  exact-path-only is too narrow, since `compiler.py` importing a changed
  `parser.py` must collide; directory-prefix is too coarse and would destroy
  parallelism).

**Why the conservatism.** A failed or timed-out Graphify query **holds**
(`Checkpoint._collides` returns `True` on `ok=False`). This is why
`graphify.BlastRadiusResult` carries an explicit `ok` flag instead of
returning an empty set on failure: "nothing depends on these files" and
"I couldn't ask" must never be the same value. Correctness is never traded
for parallelism; a hold is cheap (container torn down, branch and design doc
preserved, every merge re-sweeps the queue), a collision that reaches merge
is not.

**Two implementation details that had to be right.**

- **Path normalization.** `harness._normalize_path:507` canonicalizes to
  repo-relative POSIX form before intersecting, because collision inputs
  arrive from three sources (design docs, the registry, PR file lists) with
  no shared format guarantee — `./x` and `x` must collide.
- **The registry must be shared *and* rebuildable.** `compose()` passes
  `checkpoint=loop.harness.checkpoint` (`__main__.py:298`) so the driver
  records into and checks against the *same* `InFlightRegistry` the harness
  owns. A driver-private checkpoint would always see an empty registry —
  which is exactly what happened live (issues 24/25 both ran to review
  editing the same file; fixed in S43). And because that registry dies with
  the process, every wake and backfill first re-adopts one rebuilt from
  committed design docs (`wake._adopt_in_flight_claims:336`).

### 8. One failure channel, one retry mechanism

**What.** Every hard failure anywhere lands in the same place: label
`agent-blocked`, board card to Blocked, and one marked comment carrying the
stage, the reason, and the attempt history. Distinct markers identify the
source (`aorc:blocked-ping`, `aorc:sync-conflict`, `aorc:merge-conflict`,
`aorc:timeout-ping`, `aorc:cost-cap-ping`, `aorc:escalation-failure`,
`aorc:merge-time`, `aorc:checkpoint-hold`, `aorc:rollback`,
`aorc:spec-kickback`, `aorc:awaiting-config`) but the human experience is
one comment that tells the whole story.

Likewise there is exactly **one** retry mechanism for code problems: the
coder's bounded fix loop. The reviewer has none of its own — any gate
failure (smoke, coverage, reviewer rejection) re-enters `CoderStage.run`
with the failure text as `review_feedback` (`reviewer._fix:216`), landing in
the *same prompt slot* the test-failure summary uses (`coder.py:211`). Human
PR comments classified as `code` intent take the identical path
(`merge._feedback_code:410`), and so does the stale-PR recheck after someone
else's merge (`merge._recheck:367`).

**Why.** Every parallel retry mechanism is a second place for bugs, a second
attempt-counting policy, and a second thing to explain. The design collapses
them: *feedback is just another attempt's context, wherever it came from.*

### 9. Provider failures are not model failures

**What.** Two counters, separated mechanically by exception type:

- A **transient provider error** (429 / 5xx / timeout / connection reset)
  becomes `ProviderError` at the adapter boundary
  (`llm/claude_adapter.py:77`, `llm/openai_adapter.py:71`), and
  `BackoffLLMClient` (`escalation.py:72`) retries the *same* model on a
  fixed 2s/8s/30s schedule. Only exhaustion counts as one real attempt. The
  coder keeps its own separate `provider_retries` counter so provider
  trouble never consumes fix-loop attempts (`coder.py:218`).
- **Bad model output** (schema miss, still-red tests) consumes real
  attempts: primary × `failure.primary_attempts` → escalation ×
  `failure.escalation_attempts` → `agent-blocked` with a full report
  (`escalation.EscalationLadder:147`).
- `FailFastProviderError` is the one provider error that is **never**
  retried: a local `base_url` configured on a GitHub-hosted runner
  (`llm.assert_local_llm_reachable:35`), where localhost is the cloud VM and
  connection-refused will never heal. Fail fast with an explanatory message
  instead of grinding through backoff and every ladder rung.
- GitHub's side mirrors this: `RateLimitedGitHubClient` (`escalation.py:263`)
  retries 429s and 403s-*with-`Retry-After`* (the secondary-limit signature)
  and never counts them as failures; a plain 403 is a permissions error and
  propagates. The header is the mechanical discriminator (**[B26]**).

**Why (B25).** Without the split, a flaky network burns `primary_attempts`
and prematurely escalates — or blocks — a perfectly solvable issue.

### 10. Hard safety rails for unattended operation

Because nobody is watching:

- **Cost circuit breakers** (`guards.CostGuard:61`, **[B1]**): per-issue
  (~$5) → block that issue; per-run (~$50) → pause the whole wake cycle;
  daily (~$100) → halt everything. Checked **most-severe-first** so a spend
  crossing two thresholds reports the wider-blast-radius action. Once a
  run/daily cap trips, every subsequent `record` — for any issue — returns
  paused/halted, modeling "pause the system" without cross-issue state.
- **The 1.5× overshoot rule** (**[B4]**) exists for a subtle reason: a plain
  cap trip means "finish the current stage's commit, then stop at the
  artifact boundary", because a mid-stage hard-kill would leave a
  half-written design doc that fails the schema and then *masquerades as a
  clarity problem* (mis-routing to `needs-clarification`). But a single
  stage burning 1.5× the cap — a stuck fix loop — hard-stops immediately.
- **Wall clock** (`guards.ComputeGuard:131` + `harness.enforce_wall_clock:645`):
  default 45 min per container, then kill through the same
  branch-preserving teardown any other block uses. Deliberately well under
  the 6-hour Actions ceiling.
- **Concurrency ceiling** (default 5, config): excess actionable issues wait
  in a queue that lives in GitHub, not in memory.

**A recurring seam-design choice.** Neither guard *measures* anything.
`CostGuard.record` trusts caller-supplied `CostTotals`; `ComputeGuard.check`
takes `elapsed_minutes` as an argument; `ClarificationStage.check_timeout`
takes `elapsed_days`. The interfaces have no notion of money or time, so
measurement is the caller's job and the guards stay pure decisions —
trivially testable, with no clocks to mock. (The honest cost of that choice
is in Part VIII: nothing in the live composition currently *does* the
measuring.)

### 11. Idempotency everywhere, because webhooks lie

Webhooks are at-least-once **and** droppable, so every entry point is
written to be safely repeatable:

- **Dedup key** (`wake.claim_event:108`): the tuple `(issue, stage,
  head_sha)` recorded as a comment marker. True exactly once; a duplicate
  delivery gets `False` and the caller no-ops.
- **Artifact check** (`wake.stage_artifact_present:119`): a stage's first
  question is "is my artifact already committed?". This catches the race the
  key cannot — with no DB there is no atomic compare-and-swap, so two
  containers can spin up before either commits. The loser finds the
  survivor's artifact and no-ops: **wasteful, never wrong** (an accepted,
  documented v1 limitation).
- **Install** re-runs find the config PR already open (or the config already
  merged) and only re-run the naturally-idempotent backfill.
- **Decomposition** skips sub-issues whose `(parent, index)` marker already
  exists in any issue, open or closed (**[B8]**).
- **Comment-driven work** (holds, awaiting-config notices, human feedback)
  is marker-deduped so the release→re-hold cycle never spams an issue.
- **Branch creation** is an explicit no-op when the branch exists, so
  re-dispatch stays idempotent (`sdk_adapter.create_branch:256`).

### 12. Escape hatches are explicit, labeled, and never defaults

Two dev-only flags exist, both documented as "never use live" in
`--help` text and in code:

- `--dev-pat-minter` — every per-issue token is a fixed `$GITHUB_TOKEN` PAT
  instead of the real App exchange (`__main__.pat_passthrough_minter:80`).
  It was *demoted from being the default* the moment S23 landed the real
  minter.
- `--no-container` — the toolchain runs on the host via
  `SubprocessTestRunner` instead of `docker exec`, with the help text
  spelling out the consequence: "untrusted, LLM-generated commands then run
  with the orchestrator's own user/filesystem/network".

The same discipline appears in `harness.ensure_target_clone`, which prints
the mismatch to stderr rather than silently substituting a clone, and in
`cleanup_branch`, which raises on an unknown outcome string rather than
guessing a fourth case.

---

## Part III — Every file, explained

### Map

```
src/aorc/
├─ interfaces.py       the two seams, data types, seam error contract    (242 L)
├─ config.py           .aorc.yml parsing, env expansion, fail-closed      (245 L)
├─ pipeline.py         label state machine + artifact checker             (155 L)
├─ triage.py           actionable / not-ready first pass                  ( 65 L)
├─ clarification.py    grill-me interview for vague issues                (151 L)
├─ decomposition.py    epic → PRD + sub-issues, marker-idempotent         (177 L)
├─ dispatch.py         declared blockers + concurrency partitioner        ( 81 L)
├─ design.py           design stage, schema, path snapping, registry      (264 L)
├─ tester.py           tester+critic+coverage+classifier+TestRunners      (564 L)
├─ coder.py            bounded fix loop, schema lock, preservation        (308 L)
├─ reviewer.py         smoke/coverage gates, reviewer LLM, PR open        (287 L)
├─ driver.py           the four stages in sequence, resume rules          (270 L)
├─ harness.py          worktrees, Docker runtime, clone guard, checkpoint (662 L)
├─ credentials.py      broker, token model, leak checks, scrubbing        (304 L)
├─ guards.py           cost + wall-clock circuit breakers (pure)          (144 L)
├─ escalation.py       backoff, escalation ladder, rate-limit decorator   (369 L)
├─ wake.py             stateless wake loop: dedup, rebuild, sweep         (441 L)
├─ merge.py            merge-time: close, stale PRs, feedback, rollback   (478 L)
├─ install.py          board/labels/config PR, config gate, event table   (518 L)
├─ webhook.py          HMAC-verified stdlib HTTP receiver                 (101 L)
├─ gitops.py           real git rebase/revert behind the GitOps seam      ( 75 L)
├─ graphify.py         knowledge-graph seam (ok=False ≠ empty)            ( 80 L)
├─ graphify_adapter.py MCP-backed GraphifyClient                          ( 45 L)
├─ __main__.py         composition root + CLI  ← START HERE               (450 L)
├─ github/
│  ├─ __init__.py         build_github_client factory                     ( 17 L)
│  ├─ sdk_adapter.py      PyGithub REST + Projects v2 GraphQL             (428 L)
│  ├─ app_token.py        App-JWT → installation token (stdlib HTTP)      (114 L)
│  ├─ actions_runtime.py  workflow_dispatch runtime + sealed secrets      (186 L)
│  └─ mock.py             in-memory GitHub for the unit suite             (168 L)
└─ llm/
   ├─ __init__.py         build_llm_client + local-LLM fail-fast          ( 74 L)
   ├─ claude_adapter.py    Anthropic adapter + error translation          ( 91 L)
   ├─ openai_adapter.py    OpenAI-compatible adapter (incl. local)        ( 83 L)
   └─ mock.py              scripted LLM, can raise scripted exceptions    ( 44 L)
```

### `interfaces.py` — the constitution

Defines the plain dataclasses that cross the seams (`Issue`, `Comment`,
`PullRequest`, `Message`, `Completion`) — **SDK objects never escape an
adapter**. Also owns the seam *error* contract and `strip_code_fences`
(`interfaces.py:75`), the S32 lesson that real models wrap requested JSON in
markdown fences despite "JSON only" instructions, so every parser normalizes
first.

Read `GitHubClient`'s method list as a *requirements document*: it is
exactly what a stateless orchestrator needs to rebuild pipeline state from
GitHub alone. Two methods carry special weight:

- `get_file(path, ref) -> str | None` — that `None` **is** the resume
  signal. "Does the design doc exist?" is literally this call.
- `create_branch` — exists because GitHub's contents API never auto-creates
  a branch (a `commit_file` to a nonexistent branch 404s). This was live bug
  S29; the docstring records the lesson, and `MockGitHubClient` now raises
  `UnknownBranchError` for exactly this case (`github/mock.py:18`) so the
  unit suite can never again hide it.

### `config.py` — the fail-closed parser

`load_config` → `parse_config` validates every block's *type* before
reading it (`failure`, `merge`, `smoke`, `coverage`, `dispatch`,
`clarification`, `cost`, `compute`, `container` must each be the right
shape), rejects an unknown `container.runtime`, requires
`container.workflow_file` when the runtime is `actions`, and rejects any
`runner` value but `default`. `build_blockers` is the fail-closed companion:
parsing can succeed while the build is still blocked on missing
`setup`/`test`. `_parse_window` accepts `infinity`/`inf` for clarification
timeouts, restoring wait-forever behavior. Full schema in Part VI.

### `pipeline.py` — the state machine

~150 lines, all pure logic, and worth reading in full:

- `STAGE_ORDER = ["in-design", "in-test", "in-code", "in-review"]` with
  `next_label` — the happy path is a list, not a graph.
- `TERMINAL_LABELS = {"needs-clarification", "agent-blocked"}` — the machine
  never auto-advances out of these; a human must act. They are deliberately
  distinct from `aorc-held`/`awaiting-config`, which are *auto-releasable*
  queue states. The distinction determines what the sweep may touch:
  the sweep releases held issues; **nothing sweeps `agent-blocked`**.
- `derive_column` — the board column is a pure lookup from the label
  (**[A1]**, PRD story 57), so label and board cannot disagree; a closed
  issue is Done regardless of its labels.
- `ArtifactChecker` — the "label is not truth" enforcement. Note that
  `in-code` has **no** static artifact by design: the coder's output is
  verified by running tests, not by presence. `artifact_exists` therefore
  returns `True` unconditionally for it, and the driver compensates by
  *always* re-running the coder rather than trusting that blanket `True` —
  a trap `driver.py`'s docstring calls out explicitly.

### `triage.py` / `clarification.py` / `decomposition.py` / `dispatch.py` — the intake

All four run **orchestrator-side with no build container** (PRD "Agent
Placement"): they decide what work exists and what runs next, and they are
cheap.

**Triage** is deliberately a fuzzy first pass, *not* the authoritative gate —
Design is ("Triage guesses; Design decides"). One LLM call answers one
question: is the definition of done bounded and testable? An empty body
short-circuits to not-ready without a call. The `vague` vs `epic` reason is
**mechanical** (`looks_like_epic`: an `epic` label or a `- [ ]` task list),
never a model's opinion of size, and it is public because
`dispatch.is_declared_blocked` reuses it to keep un-decomposed epics out of
the build pipeline.

**Clarification** implements grill-me: one question at a time, posted as
marker-tagged comments. Resume is double-gated (**[B20]**):

- *Permission* — the replier's `author_association` must be
  OWNER/MEMBER/COLLABORATOR. `CONTRIBUTOR` is deliberately excluded because
  it only means "has had a PR merged", not write access. Gating on
  permission rather than identity means anyone with repo access can answer,
  not just the issue author.
- *Content* — every reply re-runs the clarity evaluation over the whole
  conversation. "thanks!" does not resume; it gets the next question.

Timeouts (**[B21]**): nudge once after `nudge_days`, block after
`block_days` more. History is reconstructed from marker-tagged comments so
it works regardless of which account the bot posts as — the real bot login
is an installation detail this module refuses to assume.

**Decomposition** turns an epic into a PRD + sub-issues in one schema-gated
call. A low-confidence or empty result *is* the "too vague to decompose"
signal and routes to clarification — no separate vagueness pass. Idempotency
(**[B8]**) is a marker pair in each sub-issue body, searched across open
*and closed* issues before creating anything. `depth:N` labels are
**diagnostic only** (**[B9]**): there is no behavioral cap on
epics-decomposing-into-epics because the cost caps are the real backstop —
but when one trips, the depth label makes the cause legible.
`check_parent_complete` closes a parent once every linked sub-issue is
closed (documented as library-only: it needs an event source this repo
doesn't wire).

**Dispatch** is a pure partitioner: candidates → dispatch-now / queued /
blocked, honoring declared blockers first (blocked issues never consume a
capacity slot) and the ceiling second. A dangling `blocked by #N` (the issue
doesn't exist) is ignored — there is nothing real to wait on.

### `design.py` — the contract writer

`DesignStage.run` (`design.py:237`) prompts for the strict schema, retries
format misses up to 3×, routes low confidence to `needs-clarification`, and
commits the winning doc to `aorc/issue-N/design.md` on the issue branch,
where it becomes simultaneously three things: the downstream contract, the
resume artifact, and the issue's **collision claim** (the `files` field is
what the checkpoint and the rollback triage read).

Two hard-won path-hygiene helpers live here, because LLMs are sloppy about
paths and — the real danger — *consistently* sloppy, so nothing fails loudly:

- `resolve_design_files` (`design.py:101`, S42 + S49): an entry that exists
  is kept; a missing entry whose basename matches exactly one file in the
  tree snaps to it (`math_utils.py` → `src/sandbox/math_utils.py`); a
  brand-new file whose stated parent directory doesn't exist but
  suffix-matches exactly one real directory snaps into it
  (`sandbox/validators.py` → `src/sandbox/validators.py`). Live (issues
  66/67) the un-snapped version put the stub and the coder's writes at the
  repo root while the generated tests imported the *installed* package:
  `ModuleNotFoundError` on every attempt, classified `error`, never `red`.
  Ambiguity is never guessed — the entry is kept verbatim.
- `mentioned_files` (`design.py:155`, S44): extracts path-shaped tokens from
  the issue text and resolves them the same way, so the design agent sees
  the *current contents* of files the issue names and designs around the
  interfaces already there. Tokens that resolve to nothing (version numbers,
  files that don't exist) simply drop out.

Also here: `rebuild_in_flight_registry`, the stateless-bookkeeping bridge
described in Decision 7.

### `tester.py` — spec encoding, and the red-vs-error machinery

The most intricate stage, because "run tests that must fail *correctly*
before any code exists" hides a catch-22 (S40): with no implementation the
generated tests die with ImportError/NameError, which the classifier
*correctly* calls `error`, not `red` — so the stage could never proceed. The
fix chain, in order:

1. `implementation_module` (`tester.py:120`) derives the importable module
   from the design's first non-test `.py` file, stripping a `src/` layout
   prefix (`src/pkg/mod.py` → `pkg.mod`).
2. `_seed_stubs` (`tester.py:531`) commits `raise NotImplementedError` stubs
   for interface names the file doesn't define. Imports now resolve, a
   missing implementation fails *cleanly* as `red`, and the error markers
   stay honest for genuinely broken test code. The coder later overwrites
   stubs wholesale. Note the S45 ordering subtlety: stubs read the
   **worktree first, GitHub second**, because after a held issue is released
   the worktree has been rebased onto the merged main while the API's branch
   view may still be pre-merge — reading the API first would rebuild the
   stale snapshot and clobber the sync.
3. `import_header` + `normalize_interface_imports` (S40/S41): the derived
   module is the single source of truth for where the interface lives, so a
   deterministic import header is prepended and the model's own
   *wrong-module* interface imports are stripped. Live, the tester guessed
   `from math_utils import divide` against the real `sandbox.math_utils`,
   and one wrong module-level import kills the whole file before the correct
   header can matter.
4. `generated_test_path` = `tests/test_aorc_issue_<n>.py` (S39): it must sit
   in the project's conventional test root, because pytest configs routinely
   restrict collection (`testpaths=["tests"]`) — a generated test that is
   never *collected* silently turns the red gate green. The per-issue
   basename keeps concurrent issues from colliding on module names.

Per-attempt sequence: tester LLM → schema gate → interface-coverage gate →
critic LLM (distinct model; its rejection reason becomes the retry's
feedback) → commit test + marker → mirror into the worktree → run → classify.
`setup` runs **once before the attempt loop** (S37 — the tester's pytest is
the first toolchain command a fresh container ever sees, so without this the
target package was never installed), and a setup failure costs zero LLM
attempts. Every failed attempt appends one line to a `reasons` list that
becomes the block comment — the only record of what happened (S31), which is
also why a successful setup logs `"setup: ok (returncode=0)"` (S38: a later
block was misread live as "setup never ran").

**The three `TestRunner`s** also live here:

| Runner | Executes | Used by |
|---|---|---|
| `MockTestRunner` | scripted results, records calls | the unit suite |
| `SubprocessTestRunner` | `shell=True` on the host | unit suite, `--no-container`, and the `actions` runtime (no local container to exec into) |
| `ContainerTestRunner` | `docker exec -w /workspace <name> sh -c "<cmd>"` | the live `docker` path (S27) |

The elegant part of `ContainerTestRunner`: the container is resolved from
`cwd` **alone** — `issue_number_from_worktree_path(cwd)` →
`container_name_for(n)` — because the worktree path and the container name
are both pure functions of the issue number. No registry threads a handle
through the driver to keep them in sync.

### `coder.py` — the blindfolded implementer

`CoderStage.run` (`coder.py:197`) loops up to `max_retries` (3): build
messages (design + current file contents + last failure summary) → schema
gate → preservation gate → commit each file **and mirror it into the
worktree** (order matters; see `harness.write_worktree_file`) → run
setup/test/lint → returncode 0 proceeds; otherwise `failing_test_summary`
(pass/fail + output, *never* test source) becomes the next attempt's
feedback. Provider errors retry on their own counter; `infra-fail`
hard-stops rather than being fed to the coder as if it were a failing test.
On a format miss the retry prompt carries what the model actually returned
**and** `finish_reason` (S34), so a provider truncation is distinguishable
from model confusion.

### `reviewer.py` — the last gate and the PR

Gate order is cost-ordered (PRD's container flow): smoke → coverage →
reviewer LLM. Both mechanical gates are *configured, never guessed* — smoke
needs both `smoke:` examples **and** a `smoke_command` template, because
there is no generic way to invoke an arbitrary app, so the gate is skipped
rather than invented (see Part VIII: no such config field exists yet, so it
is skipped live — exactly as the install PR text warns). The coverage gate
only parses the last `NN%` in its command's output; scoping to changed lines
is the command's job (e.g. a `diff-cover` invocation in `.aorc.yml`).

The reviewer then reads a **real diff**, built with `difflib.unified_diff`
from two `get_file` calls per design file (main vs branch) — no new seam
method was needed for this. Any failure re-enters the coder's fix loop.
Then the rebase discriminator; then `_open_pr`, after which the entire
attempt history (rejections *and* the approval) is posted as PR comments in
one batch: the PR didn't exist during earlier rejections, but the human
merging it sees the whole trail (PRD story 39).

The `pr=` parameter is the S17 stale-PR seam: when a PR already exists, the
gates and reviewer run against it, nothing new is opened, the trail is
appended to it, and the caller owns the rebase.

### `driver.py` — the conductor

`PipelineDriver.run` (`driver.py:129`) reads as the pipeline's table of
contents: read label → (terminal? return) → ensure worktree → create the
remote branch (S29) → for each stage, **skip it if its artifact already
exists**, else run it. That skip *is* the entire resume story. Labels move
only through `PipelineStateMachine.advance`, never a hand-rolled
add/remove pair. Between design and test sits the checkpoint call; before
the coder, design files are re-read from the worktree so the coder sees
current contents (S44); a `hold` posts one marker-deduped comment (the
sweep's release→re-hold cycle must not spam the issue).

### `harness.py` — worktrees, containers, and the checkpoint

Four responsibilities in one file:

1. **Target-repo discipline** (`ensure_target_clone:278`, S35). Worktrees are
   only ever built from a checkout of the *target* repo. The live bug this
   fixed is the most memorable in the codebase: with `repo_dir="."`
   defaulting to the orchestrator's own cwd, every per-issue worktree was a
   checkout of **AORC itself**, so containers ran AORC's own test suite
   against sandbox issues. Now the `origin` slug is verified; a mismatch is
   printed to stderr and `.aorc/clone` is materialized (fetched on reuse); a
   foreign occupant of the clone dir raises `TargetRepoError`. Git auth
   rides in ephemeral `GIT_CONFIG_*` env vars — never argv (`ps`-visible),
   never persisted into `.git/config`.
2. **Worktrees** (`WorktreeManager:341`). One per issue on `aorc/issue-<n>`.
   Fresh creation prefers the *fetched* `origin/main` over the local clone's
   possibly-stale HEAD (S26), and re-points the local branch at the remote
   tip when earlier API commits exist. On **re-dispatch** it syncs with the
   current main (S45): fetch → reset to the remote branch tip (safe: every
   mirrored write was also API-committed) → `rebase --autostash origin/main`
   → on a true conflict, `rebase --abort` and raise `WorktreeSyncConflict`,
   which dispatch maps to `agent-blocked`. Rationale: a held issue released
   after another merged **must** build on the merged main, or the coder's
   full-file writes silently drop the merged changes.
3. **The worktree/API split-brain fix** (`write_worktree_file:447` /
   `read_worktree_file:474`, S22). The pipeline commits through the GitHub
   *contents API*, but the toolchain runs against the local worktree — two
   views of one branch with no git transport between them (and none possible
   against `MockGitHubClient`). Since the caller already holds the exact
   committed bytes, it writes them straight into the worktree *before* any
   test run. `cwd == "."` (the unit-suite default) skips the mirror rather
   than spraying files into the process's own directory.
4. **Checkpoint, registry, teardown** (Decision 7). `teardown` composes
   runtime teardown + registry claim clearing + `cleanup_branch`'s three
   fixed cases (**[B24]**: merged → delete; agent-blocked / held → keep,
   resumable). An unknown outcome string raises — no agent judgment can
   invent a fourth case.

`DockerContainerRuntime.start` also pins the container alive with
`tail -f /dev/null` (S33): without a pinned command, a stock image exits
instantly and every later `docker exec` fails with "container is not
running" — which is precisely the string the `infra-fail` classifier
watches for.

### `wake.py` + `install.py` — the loop and its gate

`WakeLoop` owns the three liveness entry points (**[B5]–[B7]**):

- `wake()` — one tick: token-expiry pass over every in-flight container →
  rebuild state from GitHub → re-adopt the collision registry → sweep the
  held queue through the dispatch selector.
- `on_pr_merged` — the *bare* sweep. The full merge behavior lives in
  `merge.MergeTimeHandler`, which runs this internally; the docstrings warn
  that wiring both would claim the same dedup key and silently skip the S17
  behavior. `route_webhook` correctly wires the full handler.
- `backfill()` — the re-sync: every open issue not already in the flow goes
  through triage as if newly opened; actionable → selector; vague →
  clarification; epic → decomposition. Idempotent, so it doubles as the
  install-time sweep, the "AORC fell behind" recovery command, and the
  handler for `issues.opened/edited`.

`_route_not_ready` is a deliberate template-method hook: the bare loop
leaves not-ready issues in the backlog; `ConfigGatedWakeLoop` overrides it
to route epic → decomposition and everything else → clarification.

`install.py` adds four things:
- **The App manifest** (`APP_PERMISSIONS`, `APP_WEBHOOK_EVENTS`,
  `app_manifest`) — the registration payload, and a superset of the
  per-issue token ceiling that must remain mintable under it.
- **`ConfigGate`** — reads `.aorc.yml` from main through the seam and
  fails closed with a specific reason (absent / not YAML / not a mapping /
  invalid / missing required fields).
- **`ConfigGatedWakeLoop`** — overrides exactly the dispatch path (park
  under `awaiting-config` with one deduped comment when closed), widens
  `_already_in_flow` so parked issues aren't re-triaged, skips the held
  sweep while the gate is closed (sweeping would only churn labels), and
  adds `_release_awaiting` to `wake()` so the queue drains through the
  normal selector once the config merges.
- **`InstallHandler.on_install`** — board (six columns), all eight labels up
  front (so a later label write never 404s), the config PR (template +
  rollback workflow, idempotent), and an immediate backfill.

`route_webhook` (`install.py:471`) is the complete event table:

| Event | Condition | Action |
|---|---|---|
| `installation` | `created` | `installer.on_install()` |
| `pull_request` | `closed` + merged | `handler.on_pr_merged(...)` |
| `pull_request` | closed, not merged | ignored (branch kept, no wake) |
| `issue_comment` | `created`, on a PR | `handler.on_pr_comment(...)` |
| `issue_comment` | `created`, on an issue | `loop.wake()` |
| `pull_request_review_comment` | `created` | `handler.on_pr_comment(...)` |
| `issues` | opened/edited/labeled/reopened | `loop.backfill()` |
| `push` | to `refs/heads/main` | noted only — red-main detection belongs to the workflow |
| `repository_dispatch` | `aorc-main-broken` | `handler.on_main_broken(pr)` |

### `merge.py` — everything around a merge

Three webhook-facing entry points on `MergeTimeHandler`:

- **`on_pr_merged`** (`merge.py:275`): dedup → close the issue, Done column,
  delete the branch → reindex Graphify → **stale-PR recheck** (**[B16]**):
  every *other* open AORC PR whose files overlap the merged one is rebased,
  re-tested (fix loop on breakage), and re-reviewed against the new HEAD.
  Still green → stays approved. Conflict → the *issue* is blocked but the PR
  stays open — never auto-closed as superseded, never blindly left stale (a
  stale "approved" PR is a lie waiting for the human to merge it). Finally
  the wake sweep runs, because a merge is exactly the event a held issue was
  waiting for.
- **`on_pr_comment`** (`merge.py:294`, **[B22]**): filter the bot's own
  comments (author **and** `<!-- aorc:` prefix — belt and braces, so the
  reviewer's own trail can never self-trigger a loop) → dedup by
  `feedback_marker(comment_id)` → one LLM call classifies intent → `code`
  routes into the coder's fix loop with the comment as feedback (tests stay
  locked structurally); `spec` relabels `in-design` and re-dispatches, so
  the pipeline re-runs forward from Design where rewriting spec and tests is
  legitimate. An unclassifiable comment is left **unclaimed** so a later
  delivery can retry it.
- **`on_main_broken`** (`merge.py:320`, **[B18]**, safety rule 9): the
  generated `aorc-rollback.yml` runs the repo's own tests on every push to
  main; on red it reverts and fires `repository_dispatch` with the PR
  number. The handler reverts through the `GitOps` seam, then triages every
  in-flight container with `rollback_verdict`: overlap with the reverted
  files (direct or blast-radius), **or no committed design doc yet** (cannot
  prove non-overlap) → teardown + immediate re-dispatch against the
  corrected HEAD; provable non-overlap → continue.

`LocalGitOps.revert_pr` is worth noting for the DB-free trick it repeats:
GitHub stamps the PR number into the merge commit's subject ("Merge pull
request #N …" or a "(#N)" squash suffix), so the offending commit is
recoverable from `git log` alone — and the regex is word-bounded so #9 never
matches #91. Merge commits are reverted with `-m 1`; squash merges without.

### `webhook.py` — the receiver

~100 lines of stdlib. `verify_signature` is a pure function doing
constant-time HMAC-SHA256 comparison (`hmac.compare_digest`) so a timing
side-channel can't leak how many prefix bytes matched. The handler verifies
**before parsing the body** (a bad signature is a 401 and the JSON is never
touched), ACKs 200 **before routing** (GitHub's delivery timeout is ~10s and
a route can be slow), runs on `ThreadingHTTPServer` so one slow delivery
doesn't stall the next, and overrides `log_message` to a no-op so request
data never reaches stderr. It adds **no routing logic of its own** — the
existing `route_webhook` table owns that.

### The adapters

**`github/sdk_adapter.py`** — REST through PyGithub; Projects v2 through raw
GraphQL strings posted via the SDK's requester (classic Projects is sunset
and PyGithub has no v2 support). A board "column" is really a single-select
**Status** field, so `set_board_column` resolves the field and option ids,
finds or adds the issue's project item, then fires one mutation. Three
behaviors are load-bearing:

- `get_file` turns a 404 into `None` — the resume signal.
- `commit_file` uses GitHub's optimistic lock: an update requires the
  current blob's sha, so a concurrent change 409s instead of silently
  clobbering.
- `create_board` catches token-policy refusals (fine-grained PATs cannot
  create Projects v2; GraphQL answers FORBIDDEN), logs once, and sets
  `self._project = None`, after which every board op is a silent no-op
  (S28). This is *correct* rather than a fallback hack: the board is a
  derived, display-only projection and labels remain the source of truth.

**`github/app_token.py`** — the exchange, deliberately on stdlib `urllib`
so the App-auth half stays dependency-free (PyJWT + cryptography are the
`apptoken` extra, lazily imported). `sign_jwt`, `transport`, and `clock` are
all injectable, so unit tests exercise claims, endpoint sequence, permission
narrowing, and error handling against fakes with nothing installed. Errors
carry only the repo and HTTP status — never the key.

**`github/actions_runtime.py`** (S25) — the second `ContainerRuntime`.
`start` writes per-issue secrets → fires `workflow_dispatch` → resolves the
run id by listing runs for that branch+event (the dispatch call returns no
body). `teardown` cancels the run and deletes those secrets. The security
design is the whole point: dispatch **inputs are visible in run logs and the
runs-list API**, so credentials never ride there — each value is libsodium
sealed-box encrypted against the repo's Actions public key and written as a
repository secret `AORC_ISSUE_<n>_<KEY>` that the workflow reads via
`secrets.*`. It mirrors the Docker env-file discipline exactly: a per-issue
credential never outlives its issue and never touches argv or logs.

**`llm/`** — `build_llm_client` picks Claude-native vs OpenAI-compatible
from config (the OpenAI adapter covers hosted OpenAI, gateways, *and* local
runtimes via `base_url`), after the local-on-cloud fail-fast check. The
Claude adapter hoists system messages into Anthropic's separate `system=`
argument; both adapters perform the same error translation
(connection/429/5xx → `ProviderError`; 400/401 propagate, because retrying
can't fix a bad request or a wrong key).

**`graphify_adapter.py`** — `MCPGraphifyClient` over Graphify's MCP server,
with the important behavior in `blast_radius`: it catches *any* exception
and returns `BlastRadiusResult(ok=False, error=...)` rather than raising —
"query failed" is a value every caller must handle conservatively, not an
exception each caller must remember to catch.

### The mocks are architecture, not test scaffolding

`github/mock.py` and `llm/mock.py` are first-class parts of the design,
because they are what makes the two-seam rule pay off.

- `MockGitHubClient` keeps issues, comments, labels, PRs, branches, files,
  and board state in memory, and records every *mutating* call in
  `self.calls` — so tests assert seam traffic ("given this event, expect
  these calls and this label state"), exactly as the PRD's testing section
  mandates. It models real GitHub's sharp edges deliberately:
  `UnknownBranchError` on committing to a nonexistent branch (which used to
  hide S29 from the whole suite), and a `project=None` mode that reproduces
  the real adapter's unconfigured-board no-op.
- `MockLLMClient` returns queued responses in order and — the neat part —
  **raises** a queued value if it is an exception, so the backoff/escalation
  paths are exercised without a real adapter.
- `MockContainerRuntime`, `MockTestRunner`, `MockGitOps`, and
  `MockGraphifyClient` follow the same record-every-call pattern.
  `MockGitOps` even models "main moved under this branch exactly once".

### `ui/` — the dashboard (outside the package)

A local FastAPI bridge (`ui/bridge/app.py`, 469 lines) plus a static
frontend. It is **strictly read-only against GitHub** and reuses AORC's own
`SdkGitHubClient` and the label rules in `aorc.pipeline` — no pipeline logic
is re-implemented. Three details worth knowing:

- **Change detection is free.** A background thread polls one conditional
  request (`If-None-Match`) every 2s; GitHub answers **304** at no
  rate-limit cost until something changes, and the first 200 triggers a full
  refresh. The full refresh loop itself runs at 3s while a job is active and
  30s when idle; the frontend polls `/api/issues` every 1s off the cached
  snapshot. A failed refresh keeps serving the previous snapshot rather than
  erroring the dashboard.
- **Buttons spawn the real CLI.** `POST /api/run-issue/{n}`,
  `/api/release/{n}`, `/api/backfill` each launch
  `python -u -m aorc --dev-pat-minter --config sandbox.aorc.yml --repo <repo>
  <subcommand>` and stream combined stdout/stderr into a terminal panel.
  Same tested code path as typing the command — and note it uses the **dev
  PAT minter** by default, which is appropriate for a local sandbox
  dashboard and would not be for production.
- A reader disconnecting (closing the panel) never kills the running job.

### `Dockerfile`, `run-sandbox.sh`, `ralph/` — the sandbox and the base image

The root `Dockerfile` builds the image that serves double duty: the AORC
base image and the Ralph sandbox. It is `node:22-slim` plus git, Python 3.12
(+pip/venv from apt, so a working Python is *always* present and never has
to be installed by an agent), Claude Code (`@anthropic-ai/claude-code`),
`mattpocock/skills`, `uv` in `/usr/local/bin`, system-wide `pytest`, and a
non-root `agent` user. `CMD ["/bin/bash"]` and `WORKDIR /work` are the
sandbox's defaults — `DockerContainerRuntime` overrides both for per-issue
containers (`-w /workspace`, command `tail -f /dev/null`), which is why the
image's defaults don't matter to the pipeline.

The image bakes in Claude Code per PRD story 41 ("near-instant agent
startup"), but in the current design the agent loop runs **orchestrator-side**
and only the toolchain runs in-container (S27's explicit scope), so that
part of the image is provisioned for a capability v1 doesn't use yet — the
in-container agent loop is v2's first work item (Part VIII).

---

## Part IV — The wiring: `compose()` in detail

`src/aorc/__main__.py` is the **composition root** — the one module allowed
to construct real SDK-backed adapters (S21). It builds this graph:

```
compose(config, repo, *, dev_pat_minter=False, no_container=False, …)
│
│  ── the three real adapters ────────────────────────────────────────────
├─ github  = SdkGitHubClient($GITHUB_TOKEN, repo)                    :144
├─ llm     = BackoffLLMClient(build_llm_client(config.primary))      :153
├─ runtime = DockerContainerRuntime($AORC_BASE_IMAGE)                :174
│            └ or ActionsContainerRuntime(repo, workflow_file, token) :155
│              where token = an App token scoped {actions: write} —
│              wider than MINIMAL_PERMISSIONS on purpose, minted ONCE
│              here (it authenticates the orchestrator, never a container)
│
│  ── the local git substrate ────────────────────────────────────────────
├─ repo_dir  = ensure_target_clone(".", repo, token=$GITHUB_TOKEN)   :187
│              └ origin must be the TARGET repo, else use .aorc/clone
├─ worktrees = WorktreeManager(repo_dir, ".aorc-worktrees")          :188
│
│  ── credentials ────────────────────────────────────────────────────────
├─ broker = CredentialBroker(private_key=<PEM read here, ONLY here>,  :211
│                            minter=build_app_token_minter($APP_ID),
│                            llm_api_key=config.primary.api_key)
│           └ or CredentialBroker("", pat_passthrough_minter($TOKEN)) :196
│
│  ── the loop (this is where the scrub wrap happens) ────────────────────
├─ loop = ConfigGatedWakeLoop.compose(github, runtime, worktrees,     :216
│                                     broker, repo, llm, concurrency)
│         └ wraps github in ScrubbingGitHubClient EXACTLY ONCE and gives
│           that same instance to BOTH the loop and a new ContainerHarness
│           (wake.py:254) — nothing downstream ever holds an unwrapped ref
├─ installer = InstallHandler(loop)                                  :225
│
│  ── the build pipeline, only if config.setup AND config.test ──────────
├─ if driver is None and config.setup and config.test:               :232
│    critic_llm  = BackoffLLMClient(build_llm_client(config.escalation))
│                  └ falls back to `llm` when no escalation slot is set
│    test_runner = ContainerTestRunner()                             :253
│                  └ SubprocessTestRunner() under --no-container or
│                    container.runtime: actions (nothing to exec into)
│    coder    = CoderStage(llm, loop.github, test_runner, setup/test/lint)
│    reviewer = ReviewerStage(critic_llm, coder, loop.github, test_runner,
│                             coverage_command, coverage_floor,
│                             smoke_examples, gitops=LocalGitOps(repo_dir))
│    driver   = PipelineDriver(loop.github, worktrees,
│                 DesignStage(llm, loop.github),
│                 TesterStage(llm, critic_llm, loop.github, test_runner,
│                             test_command, setup_command),
│                 coder, reviewer,
│                 checkpoint=loop.harness.checkpoint)   ← SHARED instance
│    loop.driver = driver
│
└─ merge_handler = MergeTimeHandler(loop, LocalGitOps(repo_dir),      :307
                     coder=coder, reviewer=reviewer,
                     test_runner=test_runner, test_command=config.test,
                     feedback_llm=llm, worktrees=worktrees)
```

### The four wiring rules, and the bugs that earned them

1. **The scrub wrap happens exactly once, at the root.** Not per caller, not
   defensively everywhere. `WakeLoop.__init__` re-wraps only when handed a
   raw client (hand-assembled test loops); one layer is the contract.
2. **The driver's checkpoint must be the harness's checkpoint** — same
   object, same `InFlightRegistry` (S43; the live two-issues-one-file bug).
3. **Build stages exist only when `setup` and `test` are configured.**
   `compose()` also runs for `install`, before any real `.aorc.yml` may
   exist, and then `driver` stays `None` — safe, because the config gate
   guarantees `dispatch_issue` never reaches a `None` driver while closed.
4. **Everything is keyword-overridable.** `tests/test_main.py` substitutes
   mocks for every collaborator and drives all five subcommands with no env,
   no config file, and no network. The CLI escape hatches are the same
   mechanism surfaced to users.

### Object lifetimes

| Object | Lifetime | Notes |
|---|---|---|
| `SdkGitHubClient`, `LLMClient`, runtime, broker | one process | rebuilt every CLI invocation |
| `IssueToken` | ~1 hour | never refreshed; expiry ⇒ teardown + re-dispatch |
| `ContainerHandle` | one dispatch | dies with the process; the sweep re-dispatches |
| Git worktree | **persists across dispatches** | reused and rebased; deleted only by hand |
| Issue branch | until merge | merged ⇒ deleted; blocked/held ⇒ kept |
| `InFlightRegistry` entry | one dispatch | cleared on teardown; rebuilt from design docs each wake |
| Design doc / tests / marker | until the branch dies | the resume + collision-claim substrate |

### The CLI

`run()` (`__main__.py:377`) parses args, composes (unless a caller supplied
collaborators), and executes exactly one subcommand:

| Command | Calls | Prints |
|---|---|---|
| `install` | `installer.on_install()` | config PR number + board columns |
| `backfill` | `loop.backfill()` | dispatched / held / queued counts |
| `wake` | `loop.cron_tick()` | released / requeued |
| `run-issue N` | `loop.dispatch_issue(N)` | `dispatched` + `pipeline: stage=… status=…` (+ the reason on a block), or `parked … (awaiting-config)` when the gate is closed (S31 — before this, a blocked stage looked identical to a clean run) |
| `serve` | reads `$AORC_WEBHOOK_SECRET`, binds `route_webhook` via `functools.partial`, `server.serve_forever()` | listening host:port |

The process **exits when the command finishes** — statelessness is the
design, not an accident. `serve` is the sole long-runner.

---

## Part V — The dry run

A complete trace, from a repo that has never seen AORC to a merged PR.
Target repo: `acme/widgets`. Issue #42: *"add a `divide` function to
`math_utils.py`; raise on division by zero."*

### Pre-flight — what must exist first

| Requirement | Why | Where it's read |
|---|---|---|
| A **registered GitHub App**, installed on the repo, with `Contents/Issues/Pull requests: R+W` (plus `Actions: R+W` for the actions runtime) and a downloaded PEM | The broker's minter needs it; the permission grant is GitHub's hard ceiling above `MINIMAL_PERMISSIONS` | manual — `CLAUDE.md` §"One-time GitHub App registration" |
| `GITHUB_TOKEN` | the orchestrator's own API operations | `__main__.py:146` |
| `AORC_REPO` (or `--repo`) | `owner/repo` | `__main__.py:387` |
| `AORC_GITHUB_APP_ID` + `AORC_GITHUB_APP_PRIVATE_KEY_PATH` | the real minter | `__main__.py:208` |
| `AORC_BASE_IMAGE` | the per-issue container image | `__main__.py:176` |
| Provider key referenced by `.aorc.yml` (e.g. `$ANTHROPIC_API_KEY`) | expanded at parse time | `config.py:97` |
| `AORC_WEBHOOK_SECRET` | only for `serve` | `__main__.py:429` |
| `.aorc.yml` present locally **and** merged on main | local: parsed at startup; remote: the config gate | `config.load_config` / `install.ConfigGate` |
| Docker daemon + `uv`/venv | container runtime + the CLI itself | — |

### Step 0 — `python -m aorc install`

```sh
export GITHUB_TOKEN=… AORC_REPO=acme/widgets AORC_BASE_IMAGE=aorc-base \
       AORC_GITHUB_APP_ID=123456 AORC_GITHUB_APP_PRIVATE_KEY_PATH=~/aorc.pem \
       ANTHROPIC_API_KEY=sk-ant-…
python -m aorc install
```

Call stack: `main()` → `run()` → `load_config(".aorc.yml")` →
`_require_env("AORC_REPO")` → `compose(...)` → `installer.on_install()`
(`install.py:415`), which does four things:

1. `create_board(["Backlog","Needs Clarification","In Progress","Blocked",
   "In Review","Done"])` — a Projects v2 project via GraphQL, or a logged
   label-only degradation if the token can't create one.
2. `create_label(...)` × 8 — every label the pipeline can ever set, up front,
   so a later label write never 404s.
3. `_ensure_config_pr()` — branch `aorc/config`, commit `.aorc.yml`
   (a template with **placeholder** model names — invariant #2 means no real
   model name ships in code) and `.github/workflows/aorc-rollback.yml`, then
   open the PR. Idempotent: an existing PR or an already-merged config
   returns without writing.
4. `self._loop.backfill()` — the first-run sweep, immediately.

**What the backfill does before any config exists.** Every open issue is
triaged (cheap, orchestrator-side, no container). Actionable ones reach
dispatch — and the config gate is **closed** (`.aorc.yml` isn't on main
yet), so each is parked under `awaiting-config` with one explanatory
comment. Vague issues get their first clarification question; epics get
decomposed into sub-issues. The backlog organizes itself before the
pipeline can build anything, and the visibly filling queue is the nudge to
merge the config PR (**[B23]** by design).

Terminal:

```
install: config PR #7, board ['Backlog', 'Needs Clarification', …]
```

The human edits the template — real model names, `setup: pip install -e .`,
`test: pytest -q`, ideally a `smoke:` block — and merges PR #7. The next
`wake` runs `_release_awaiting`, which feeds the parked queue through the
normal dispatch selector.

### Step 1 — `python -m aorc run-issue 42`

```
run()                                          __main__.py:377
 └─ compose(config, "acme/widgets")            __main__.py:115   (the graph in Part IV)
     └─ loop.dispatch_issue(42)                install.py:312
```

### Step 2 — the two gates before any work

**Gate A — config** (`ConfigGate.check`, `install.py:239`): read `.aorc.yml`
**from main, through the seam** → parse → `build_blockers`. If closed:
label `awaiting-config`, board → Blocked, one deduped comment, and
`dispatch_issue` returns `None` — the CLI prints
`parked issue #42 (awaiting-config)` and exits 0. **No container was
started, and the output says so** (S31 made outcomes honest). Open → fall
through to `WakeLoop.dispatch_issue` (`wake.py:357`).

**Gate B — credentials and isolation:**

```
token = broker.mint(42, "acme/widgets")                    credentials.py:128
    ├─ ceiling check: contents/issues/pull_requests ≤ write ✓
    └─ minter(private_key, repo, perms)                    github/app_token.py:78
         ├─ sign JWT {iss: 123456, iat: now−60, exp: now+600}   RS256
         ├─ GET  /repos/acme/widgets/installation        → installation id
         └─ POST /app/installations/<id>/access_tokens
              body {"repositories": ["widgets"],
                    "permissions": {"contents":"write","issues":"write",
                                    "pull_requests":"write"}}
              → "ghs_…"   (one repo · narrowed · ~1h)

env = broker.container_env(token)                          credentials.py:156
    → {"GITHUB_TOKEN": "ghs_…", "AORC_LLM_API_KEY": "sk-ant-…"}
      (raises CredentialLeakError if either value were PEM-shaped)

handle = harness.dispatch(42, env)                         harness.py:615
    ├─ assert_env_clean(env)                               (re-checked here)
    ├─ path = worktrees.ensure(42)                         harness.py:409
    │    first dispatch : git fetch origin
    │                     git worktree add -b aorc/issue-42 \
    │                         .aorc-worktrees/issue-42 origin/main
    │    re-dispatch    : fetch → reset --hard origin/aorc/issue-42 →
    │                     rebase --autostash origin/main
    │                     (true conflict ⇒ WorktreeSyncConflict ⇒ agent-blocked
    │                      + <!-- aorc:sync-conflict --> comment, never a
    │                      dispatch on a stale tree)
    └─ runtime.start(42, branch, path, env)                harness.py:157
         ├─ write env to a 0600 temp file (never argv — `ps` is public)
         ├─ docker run -d --name aorc-issue-42 --env-file <tmp> \
         │      -v /abs/path/.aorc-worktrees/issue-42:/workspace \
         │      -w /workspace aorc-base tail -f /dev/null
         └─ os.unlink(<tmp>)          ← immediately, docker read it already

loop.in_flight[42] = (handle, token)
result = loop.driver.run(42)                               driver.py:129
```

### Step 3 — Design

Issue #42 carries no pipeline label → `advance` sets **`in-design`**, board
card → In Progress. The remote branch is created via the API (`create_branch`,
S29 — the contents API would 404 on the first commit otherwise). No
`aorc/issue-42/design.md` on the branch, so the stage actually runs:

```
context = mentioned_files("…math_utils.py…", cwd)   → ["src/widgets/math_utils.py"]
          (snapped to the real path; its CURRENT contents go in the prompt)
completion = design_llm.complete([system=STRICT_SCHEMA_PROMPT, user=…])
```

The model answers (fences stripped first by `strip_code_fences`):

```json
{
  "interface":  [{"name": "divide", "inputs": ["a", "b"], "outputs": "float"}],
  "test_specs": ["divide(10, 2) returns 5.0",
                 "divide(1, 0) raises ValueError"],
  "task_list":  ["add divide() to math_utils with a zero-divisor guard"],
  "files":      ["math_utils.py"],
  "confidence": 0.9
}
```

`parse_design_response` ✓ · `confidence 0.9 ≥ 0.5` ✓ → committed to
`aorc/issue-42/design.md` on the branch → label advances to **`in-test`**.
Then `resolve_design_files` snaps `math_utils.py` →
`src/widgets/math_utils.py` **before any stage consumes it** (S42).

*Alternate endings:* three unparseable responses → `agent-blocked` + comment.
`confidence: 0.3` → `needs-clarification`, board → Needs Clarification, run
ends until a human answers.

### Step 4 — The checkpoint

```
verdict = loop.harness.checkpoint(CheckpointReport(42, ["src/widgets/math_utils.py"]))
  ├─ collision set = every other in-flight claim ∪ every open AORC PR's files
  ├─ my_files ∩ others?  → hold
  ├─ graphify configured? → blast radius, both directions; ok=False ⇒ hold
  └─ record my claim in the shared registry   ← what the NEXT dispatch checks
```

**If issue #37 is mid-pipeline claiming the same file:** `hold` → label
`aorc-held`, one deduped `<!-- aorc:checkpoint-hold -->` comment, the
in-flight slot is freed, and `harness.teardown(handle, "held")` removes the
container while **keeping the branch and design doc**. When #37's PR merges,
the merge webhook's sweep finds #42 released, re-dispatches it, and the
driver *skips design* (artifact exists) and resumes right here.

In this run nothing collides: **proceed**.

### Step 5 — Tests first

`TesterStage.run(42, design, cwd=".aorc-worktrees/issue-42")`:

1. **Setup once**, before the attempt loop:
   `docker exec -w /workspace aorc-issue-42 sh -c "pip install -e ."`.
   Non-zero ⇒ `agent-blocked` with **zero LLM attempts spent**.
2. **Seed stubs.** `divide` isn't defined in `src/widgets/math_utils.py`, so:
   ```python
   def divide(*args, **kwargs):
       raise NotImplementedError("divide is not implemented yet (AORC stub)")
   ```
   appended, committed, mirrored into the worktree.
3. **Tester LLM** — sees only `interface`, `test_specs`, and
   `implementation_module: widgets.math_utils` — returns one test per spec.
   → schema gate ✓ → **interface coverage**: is `divide` referenced? ✓ →
   **critic LLM** (distinct model) reviews against the design → `approve`.
4. **Normalize + commit.** The deterministic header
   `from widgets.math_utils import divide` is prepended and any wrong-module
   interface import the model wrote is stripped (S41). Committed as
   `tests/test_aorc_issue_42.py` plus `aorc/issue-42/tests.marker`, and
   mirrored into the worktree (S22 — *before* the run; that ordering is the
   whole bug that fix exists for).
5. **Run:** `docker exec … sh -c "pytest -q"` → `2 failed`,
   `NotImplementedError`. No error markers, non-zero exit →
   `classify_test_run` = **`red`** → proceed; label → **`in-code`**.

*Alternate endings:* `error` (a real ImportError) → retry the tester with
the reason; `green` → suspicious, retry; `infra-fail` ("container is not
running") → immediate `agent-blocked`, no retries burned.

### Step 6 — Code until green

`CoderStage.run` — always executed (no static artifact for this stage). The
driver hands it the **current worktree contents** of the design files, so
the coder sees the pre-existing module *plus* the stub.

```
attempt 1
  prompt: interface + task_list + files + "Repo file src/widgets/math_utils.py: …"
          + "No previous attempt yet."          ← never any test source
  → {"tasks":[{"task":"add divide()…","path":"src/widgets/math_utils.py",
               "code":"<FULL new file contents>"}]}
  schema gate      ✓  (1 task = 1 task_list entry; path ∈ files)
  preservation gate ✓  (every pre-existing top-level name still defined)
  commit + mirror  → run toolchain in the container:
        pip install -e .   → 0
        pytest -q          → 0
        <lint>             → 0
  ⇒ proceed;  label → in-review
```

If the tests had failed, `failing_test_summary(result)` — *output only* —
seeds attempt 2. Three strikes ⇒ `agent-blocked` with the last failure tail
in the comment. A `ProviderError` retries without consuming an attempt.

### Step 7 — Review, rebase, PR

```
ReviewerStage.run(42, design, issue.body, cwd=…, pr=None)
 ├─ smoke gate    : skipped (no smoke_command configured)
 ├─ coverage gate : run coverage command, parse the last NN%, compare to floor
 ├─ reviewer LLM  : reads the real unified diff of src/widgets/math_utils.py
 │                  (main vs branch, via difflib) against design + issue
 │                  → {"verdict": "approve", "reason": "…"}
 ├─ gitops.rebase("aorc/issue-42", "main")
 │     up-to-date → continue · clean → LOOP: re-run every gate on the rebased
 │     result · conflict → agent-blocked + <!-- aorc:merge-conflict -->
 └─ open_pull_request("AORC: issue #42", "Implements:\n- add divide()…",
                      head="aorc/issue-42", base="main")
    then post the whole attempt history as PR comments
```

Terminal:

```
dispatched issue #42
pipeline: stage=in-review status=proceed
```

The process exits. **The robot never merges in v1.**

### GitHub state after the run

```
issue #42        labels: [in-review]          board: In Review
branch aorc/issue-42
  ├─ aorc/issue-42/design.md          ← the contract + the collision claim
  ├─ aorc/issue-42/tests.marker       ← the in-test artifact
  ├─ tests/test_aorc_issue_42.py      ← locked: the coder can never write here
  └─ src/widgets/math_utils.py        ← stub, then the real implementation
PR #43  head=aorc/issue-42  base=main
  └─ comments: "attempt 1: approve — …"   (the full trail)
issue #42 comments: any hold/dedup markers from this run
```

Every one of those artifacts is a resume point. Kill the process at any
moment and re-run `run-issue 42`: the driver reads the label, finds the
artifacts, skips what's done, and continues.

### Step 8 — After the human merges

GitHub delivers `pull_request / closed(merged)` to `python -m aorc serve`
(HMAC-verified, ACKed, then routed):

```
route_webhook("pull_request", payload) → MergeTimeHandler.on_pr_merged(43, sha)
 ├─ claim_event(42, "pr-merged", sha)      duplicate delivery ⇒ no-op
 ├─ close issue #42 · board → Done · delete branch aorc/issue-42
 ├─ graphify.reindex()                      (when configured — see Part VIII)
 ├─ recheck every OTHER open AORC PR overlapping math_utils.py:
 │     rebase → re-test → (fix loop if broken) → reviewer re-run
 │     conflict/exhausted ⇒ that ISSUE is blocked; the PR stays open
 └─ loop.wake(): expired tokens re-queued, held queue swept
                 ← this merge is exactly what a held issue was waiting for
```

**If main goes red instead:** the generated workflow runs the repo's own
`setup`/`test` on push, reverts the merge (`git revert -m 1` for a merge
commit, plain revert for a squash), and fires `repository_dispatch:
aorc-main-broken` with the PR number → `on_main_broken` reverts through the
`GitOps` seam and re-queues every in-flight issue whose committed claim
overlaps the reverted files — or that has no design doc yet and therefore
*cannot prove* it doesn't. Worst case is a brief, self-healing breakage.

**If the human leaves a PR comment instead:** the classifier routes it —
"also handle negative divisors" → `code` → the coder's fix loop with the
comment as feedback; "your test asserts the wrong rounding" → `spec` →
relabel `in-design`, re-dispatch, and the pipeline re-runs forward from
Design, where rewriting spec and tests is legitimate.

**And in the background,** cron runs `python -m aorc wake` every 10–15
minutes as the backstop — because webhooks are at-least-once *and*
droppable, and one dropped merge event would otherwise starve a held issue
forever with no alarm.

---

## Part VI — Reference tables

### `.aorc.yml` — the complete schema (from `config.parse_config`)

| Key | Type | Default | Consumed by |
|---|---|---|---|
| `llm.primary` | `{provider, model, api_key?, base_url?}` | **required** | `build_llm_client` → design/tester/coder/feedback |
| `llm.escalation` | same | none | the critic + reviewer slots (falls back to primary) |
| `setup` | string | none — **blocks the build** | tester (once), coder (each attempt) |
| `test` | string | none — **blocks the build** | tester, coder, stale-PR recheck |
| `lint` | string | none | coder's toolchain (skipped if absent) |
| `smoke` | list of `{input, expect}` | `[]` | reviewer's smoke gate; **its absence permanently disqualifies auto-merge** |
| `coverage.command` | string | none (gate skipped) | reviewer |
| `coverage.floor` | float | `80.0` | reviewer |
| `merge.auto` | bool | `false` | `auto_merge_allowed` (needs `smoke` too) |
| `failure.primary_attempts` | int | `3` | escalation ladder |
| `failure.escalation_attempts` | int | `1` | escalation ladder |
| `dispatch.concurrency` | int | `5` | dispatch selector |
| `clarification.nudge_days` | float / `infinity` | `7.0` | clarification timeout |
| `clarification.block_days` | float / `infinity` | `7.0` | clarification timeout |
| `cost.per_issue_cap` | float | `5.0` | `CostGuard` |
| `cost.per_run_cap` | float | `50.0` | `CostGuard` |
| `cost.daily_cap` | float | `100.0` | `CostGuard` |
| `cost.overshoot_multiplier` | float | `1.5` | `CostGuard` |
| `compute.wall_clock_minutes` | float | `45.0` | `ComputeGuard` |
| `container.runtime` | `docker` \| `actions` | `docker` | which `ContainerRuntime` is built |
| `container.workflow_file` | string | none | **required** when runtime is `actions` |
| `runner` | must be `default` or absent | — | rejected otherwise (never a bigger runner) |

A working example (`sandbox.aorc.yml`):

```yaml
llm:
  primary:    { provider: claude, model: claude-sonnet-4-6, api_key: $ANTHROPIC_API_KEY }
  escalation: { provider: claude, model: claude-sonnet-4-6, api_key: $ANTHROPIC_API_KEY }
setup: pip install --break-system-packages -e .
test: pytest
lint: echo "no lint"
failure: { primary_attempts: 3, escalation_attempts: 1 }
merge: { auto: false }
```

### Environment variables

| Variable | Required for | Read at |
|---|---|---|
| `GITHUB_TOKEN` | everything (orchestrator's own API calls) | `__main__.py:146` |
| `AORC_REPO` | everything (or `--repo`) | `__main__.py:387` |
| `AORC_BASE_IMAGE` | the `docker` runtime | `__main__.py:176` |
| `AORC_GITHUB_APP_ID` | the real minter (and the actions token) | `__main__.py:208` |
| `AORC_GITHUB_APP_PRIVATE_KEY_PATH` | the real minter | `__main__.py:209` |
| `AORC_WEBHOOK_SECRET` | `serve` only | `__main__.py:429` |
| *(whatever `.aorc.yml` references)* | LLM providers | `config.py:97` |
| `AORC_LLM_API_KEY` | set **into** the container by the broker | `credentials.py:62` |
| `RUNNER_ENVIRONMENT` | read to detect a GitHub-hosted runner | `llm/__init__.py:46` |
| `AORC_UI_CONFIG` | the dashboard's config path | `ui/bridge/app.py:230` |

### Labels

| Label | Column | Meaning | Cleared by |
|---|---|---|---|
| *(none)* | Backlog | untriaged / not dispatched | backfill |
| `in-design` / `in-test` / `in-code` | In Progress | active stage | the state machine, once the artifact exists |
| `in-review` | In Review | PR open / under review | merge |
| `needs-clarification` | Needs Clarification | waiting on a **human answer** (terminal) | a permissioned, on-topic reply |
| `agent-blocked` | Blocked | needs **human debugging** (terminal) | a human |
| `aorc-held` | Blocked | auto-releasable: collision or blocker | every wake's sweep |
| `awaiting-config` | Blocked | auto-releasable: `.aorc.yml` not merged/valid | the config gate opening |
| `depth:N` | — | diagnostic: decomposition depth | nobody |

### Artifacts (the resume ledger)

| Stage | Proof of completion | Location |
|---|---|---|
| Design | `aorc/issue-N/design.md` | issue branch |
| Test | `aorc/issue-N/tests.marker` (+ `tests/test_aorc_issue_N.py`) | issue branch |
| Code | *none by design — verified by running tests, so always re-run* | — |
| Review | an open PR with `head == aorc/issue-N` | GitHub |

### Comment markers (the system's memory)

`aorc:event` (dedup key) · `aorc:blocked-ping` · `aorc:checkpoint-hold` ·
`aorc:sync-conflict` · `aorc:merge-conflict` · `aorc:timeout-ping` ·
`aorc:cost-cap-ping` · `aorc:escalation-failure` · `aorc:awaiting-config` ·
`aorc:clarification-question` · `aorc:clarification-nudge` ·
`aorc:parent=N` / `aorc:sub-index=I` · `aorc:feedback id=…` ·
`aorc:merge-time` · `aorc:spec-kickback` · `aorc:rollback`

### Packaging (`pyproject.toml`)

Base `dependencies = []` — the core is dependency-free by policy. Extras:
`claude` (anthropic), `openai`, `github` (PyGithub), `apptoken`
(PyJWT[crypto]), `actions` (PyNaCl), `dev` (pytest, pytest-cov, PyYAML).
Console script: `aorc = aorc.__main__:main`. Pytest is configured with
`addopts = "-ra -m 'not integration'"`, so the default run is the zero-dep
unit suite and integration tests are opted into explicitly.

---

## Part VII — The test strategy

**Unit suite** — 28 files in `tests/`, zero third-party dependencies, all
against the mocks. Tests read as specifications of seam traffic: *given this
GitHub event, expect these `GitHubClient` calls and this label state* —
exactly what the PRD's "Testing Decisions" section mandates. Two files carry
special weight: `test_main.py` drives the composition root end to end, and
`test_no_sdk_imports.py` pins architecture invariant #1 permanently.

Run: `.venv/bin/pytest -q` (or `uv run pytest -q`). Verified while writing
this document: **583 passed, 13 deselected** (the integration-marked ones)
in ~12 seconds, with no network and no credentials.

| Test file | Covers |
|---|---|
| `test_github_client.py`, `test_llm_client.py` | the seams + mock contracts |
| `test_pipeline_state.py` | labels, columns, artifact gating, resume |
| `test_triage.py`, `test_clarification.py`, `test_decomposition.py`, `test_dispatch.py` | the intake path |
| `test_design.py`, `test_tester.py`, `test_coder.py`, `test_reviewer.py`, `test_driver.py` | the build spine |
| `test_harness.py`, `test_credentials.py`, `test_guards.py` | isolation, tokens, limits |
| `test_escalation.py`, `test_config.py` | failure policy, config parsing |
| `test_wake.py`, `test_merge.py`, `test_install.py`, `test_webhook.py` | loop, merge-time, install, HMAC |
| `test_actions_runtime.py`, `test_app_token.py`, `test_gitops.py`, `test_graphify.py` | adapters against fakes |
| `test_scaffolding.py`, `test_no_sdk_imports.py`, `test_main.py` | project invariants + wiring |

**Integration suite** — `tests/integration/`, marked `integration` and
deselected by default. Six files, each credential- or daemon-gated and each
skipping cleanly when its prerequisites are absent: PyGithub against a real
repo, the LLM adapters against real providers, Docker, the
`ContainerTestRunner`, the App-token exchange, and the Actions runtime.

**Injectable everything.** Every boundary the two seams don't cover is a
keyword-injectable callable — `sleep`, `clock`, `elapsed_days`,
`elapsed_minutes`, `sign_jwt`, `transport`, `seal`, `minter`, `ensure_clone`,
`runner_environment`. That is why tests can assert an exact backoff schedule
or a token expiry without waiting or touching the network.

---

## Part VIII — Gaps, findings, and honest status

The codebase documents its own gaps; this section keeps that up to date and
adds three findings from this review.

### New findings from this review

**1. Graphify is a seam with no live wiring.** `MCPGraphifyClient` exists
(`graphify_adapter.py`) but is constructed in exactly one place in the whole
repo: `tests/test_graphify.py:60`. `compose()` never builds one, and
`WakeLoop.compose` builds `ContainerHarness(runtime, worktrees, wrapped)`
with no `graphify` argument (`wake.py:255`), so `Checkpoint` gets
`graphify=None`. Live consequences, all silent:

- collision detection degrades to **path-intersection only** — the
  blast-radius half of **[B19]** never runs;
- `MergeTimeHandler` gets no graphify either, so `on_pr_merged` **never
  reindexes** (PRD story 21) and `rollback_verdict` is path-only;
- `DesignStage` gets none, so the design prompt never carries blast radius.

The conservative-hold-on-query-failure behavior is real and tested, but with
no client configured `Checkpoint._collides` takes the `graphify is None`
branch and returns "no blast-radius signal available". PRD stories 20/21/25
are therefore **built and tested, not live**. Wiring is a small change (pass
a client through `compose` → `WakeLoop.compose` → `ContainerHarness` →
`Checkpoint`, and into `MergeTimeHandler`/`DesignStage`), but it is not done.

**2. PyYAML is an undeclared runtime dependency.** `config.py:20` and
`install.py:48` do a module-level `import yaml`, but `pyproject.toml` lists
PyYAML only under the `dev` extra, with `dependencies = []`. A user who
installs the package normally and runs the `aorc` console script hits
`ModuleNotFoundError: yaml` before anything else. The zero-dep *policy* is
about SDKs; YAML parsing is core behavior, so PyYAML belongs in
`dependencies` (or `config.py` needs a lazy import plus a clear error).

**3. Small doc/behavior drift worth knowing.** The UI's actual cadence is a
1s frontend poll against a cached snapshot, with an ETag-conditional
change-detector every 2s and a full refresh every 3s (active) / 30s (idle) —
not a fixed 2.5s poll. And the dashboard shells out with `--dev-pat-minter`
and `--config sandbox.aorc.yml` by default: correct for a local sandbox,
wrong for production use.

### Gaps the codebase already documents

- **Push mediation (the big one).** A container holding its per-issue
  `GITHUB_TOKEN` can call the GitHub API or push directly, bypassing the
  orchestrator-side `ScrubbingGitHubClient` — layer-2 scrubbing covers
  orchestrator-mediated writes only. v1's answer is the scoped token (one
  repo, one hour, minimal permissions) as the blast-radius bound. S19 closed
  the `docker run -e` host-`ps` half; S27 moved the *toolchain* into the
  container. But the LLM agent loop itself still runs orchestrator-side, so
  an egress proxy / in-container agent loop is explicitly **v2's first work
  item** (`issues/README.md`).
- **Actions runtime and the rollback workflow are not exercised live** this
  iteration. Both are unit-tested against fakes and have credential-gated
  integration tests; `CLAUDE.md` records the caveat, and
  `issues/25-actions-execution-wiring.md` holds the open scope (PR-number
  extraction under squash merges, a live sandbox exercise). The CI
  `actionlint` job that statically lints the rendered rollback workflow has
  itself never been observed to run in this sandbox.
- **Clarification answers re-enter via backfill**, not a direct
  comment-webhook resume — a plain issue comment triggers a wake, and the
  issue re-enters once its terminal label is cleared (`install.py` module
  docstring, flagged "noted for honesty").
- **The smoke gate is skipped live**: `.aorc.yml`'s schema has the `smoke:`
  examples list but no `smoke_command` template, and the reviewer refuses to
  invent one, so the gate stays skipped until that field exists — exactly as
  the install PR text warns.
- **`EscalationLadder` and `RateLimitedGitHubClient` are built and tested
  but not composed.** Live behavior comes from per-stage retry loops +
  `BackoffLLMClient` + the escalation-tier model in the critic/reviewer
  slots. The ladder's "primary ×N → escalation ×M → one detailed failure
  comment" flow is therefore approximated, not literal.
- **Cost guards trust caller-supplied totals, and nothing calls them.**
  `CostGuard` is fully tested but no stage records spend against it in the
  live composition, and the *daily* total spans wake cycles, which needs a
  persistence story v1 doesn't have. Same for `ComputeGuard`: the wall-clock
  kill exists on `ContainerHarness` but needs a caller measuring elapsed
  time. These are the honest cost of the "guards are pure decisions" seam
  choice (Decision 10).
- **`decomposition.check_parent_complete`** is library-only: it needs an
  event source ("a GitHub Actions rule") the repo doesn't wire.
- **Open live issues** in `issues/`: backfill doesn't release held issues
  (46), stale state labels aren't cleaned up (47), backfill winners stall at
  `in-design` (48), plus 25/29/30 as noted above.

### The v1 → v2 line, stated plainly

v1 ships: the full pipeline, human merge as the gate, Docker isolation for
the toolchain, scoped per-issue tokens, GitHub-as-database, and the safety
rails as *decisions*. v2 owes: the in-container agent loop with mediated
egress, a persistence layer for cross-wake accounting (daily cost), the
DB-backed compare-and-swap that closes the residual webhook race, live
Graphify wiring, and the auto-merge graduation gate.

---

## Appendix — PRD decision → code traceability

| ID | Decision | Lives in |
|---|---|---|
| A1 | Column derived from label; vague → Needs Clarification | `pipeline.LABEL_COLUMN`, `derive_column` |
| A2 | Interface coverage pre-coder + line coverage pre-merge | `tester.interface_coverage_gate`, `reviewer.coverage_gate` |
| A3 | Collision set includes open unmerged PRs | `harness.Checkpoint._collision_set` |
| B1 | Cost circuit breakers (issue/run/day) | `guards.CostGuard` |
| B2 | Container compute limits; never a bigger runner | `guards.ComputeGuard`, `harness.enforce_wall_clock`, `config.py:215` |
| B3 | Concurrency ceiling (5, global, config) | `dispatch.select_dispatch`, `config.dispatch_concurrency` |
| B4 | Clean-stop + 1.5× overshoot ceiling | `guards.CostGuard.record` |
| B5 | Held-issue wake: merge webhook + cron | `wake.on_pr_merged`, `wake.cron_tick`, `_sweep_held` |
| B6 | Webhook dedup: key + artifact presence | `wake.claim_event`, `should_run_stage` |
| B7 | First-run backfill = re-sync | `wake.backfill`, `InstallHandler.on_install` |
| B8 | Decomposition idempotency by marker | `decomposition.existing_sub_issue_number` |
| B9 | Decomposition depth: diagnostic label only | `decomposition.epic_depth` |
| B10 | Secret scrubbing (env + regex, incl. LLM keys) | `credentials.scrub`, `ScrubbingGitHubClient` |
| B11 | Token expiry → tear-down-and-resume, no refresh | `credentials.handle_token_expiry`, `wake._expiry_pass` |
| B12 | Design doc strict schema | `design.REQUIRED_FIELDS`, `parse_design_response` |
| B13 | Invalid-schema routing (retry vs clarify) | `design.DesignStage.run` |
| B14 | Reviewer on a distinct model slot | `__main__.compose` (`critic_llm` → `ReviewerStage`) |
| B15 | Test-gaming defense (the reviewer stack) | `coder.parse_coder_response` + tester/critic split |
| B16 | Stale approved PR re-checked on every merge | `merge._recheck_stale_prs` |
| B17 | Merge conflict at PR-open → agent-blocked | `reviewer.run` + `gitops.LocalGitOps.rebase` |
| B18 | Rollback vs in-flight containers | `merge.rollback_verdict`, `on_main_broken` |
| B19 | Collision = path ∪ blast radius; uncertain → hold | `harness.Checkpoint._collides` *(blast half not live — Part VIII)* |
| B20 | Clarification resume: permission **and** content | `clarification.WRITE_ASSOCIATIONS`, `handle_comment` |
| B21 | Clarification timeout: nudge then block | `clarification.check_timeout` |
| B22 | Human PR feedback routed by intent | `merge.on_pr_comment`, `classify_feedback_intent` |
| B23 | Pre-config install: triage runs, build holds | `install.ConfigGate`, `ConfigGatedWakeLoop` |
| B24 | Branch naming + three cleanup cases | `pipeline.branch_name`, `harness.cleanup_branch` |
| B25 | Provider errors: backoff, then escalate | `escalation.BackoffLLMClient`, `EscalationLadder` |
| B26 | GitHub 403/429 backoff, never a failure | `escalation.RateLimitedGitHubClient` *(not composed — Part VIII)* |
| B27 | Missing `smoke:` → run, skip gate, block auto-merge | `config.auto_merge_allowed`, `install.INSTALL_PR_BODY` |
| Safety 1–10 | Interface ownership, critic, locked tests, red-vs-error, coverage, smoke, bounded loop, design refusal, auto-rollback, scrubbing | `design.py`, `tester.py`, `coder.py`, `reviewer.py`, `merge.py`, `credentials.py` |
