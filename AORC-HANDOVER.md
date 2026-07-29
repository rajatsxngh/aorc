# AORC — Project Handover

*Everything a new owner needs: what AORC is, how to stand it up from scratch,
why it is shaped the way it is, exactly what is built versus what is not
(verified against the source, not against the tickets), and where it goes
next.*

**Companion documents in this repo**

| File | What it is |
|---|---|
| `AORC-PRD-v1-FINAL-tagged.md` | The specification. Decision log A1–A3, B1–B27 with a provenance index at the end. |
| `issues/README.md` | The slice map (S1–S25) and dependency order. |
| `issues/` and `issues/done/` | One ticket per slice. `done/` = closed; root = open. |
| `HOW-IT-WORKS.md` | Short guided code tour. |
| `AORC-DEEP-DIVE.md` | Long-form module-by-module account with a full dry-run trace. |
| `CLAUDE.md` | Operating instructions for the coding agent working on this repo. |
| `README.md` | Public-facing summary. |

This document supersedes none of them; it is the one to read first.

---

## Part 1 — What AORC is

AORC (**A**utonomous **O**rchestrated **R**epo **C**ontributor) is an
unattended pipeline that takes a GitHub issue written in plain English and
drives it to a review-approved pull request. A human does exactly two things:

1. Answers clarifying questions the system posts as issue comments.
2. Clicks **Merge**.

Everything between — triaging the backlog, interrogating vague issues,
decomposing epics, designing the change, writing failing tests, implementing
until those tests pass, reviewing the diff, opening the PR, sequencing
conflicting work, reacting to a broken `main` — is done by the system.

The problem it attacks (PRD *Problem Statement*): backlogs are permanent not
because issues are hard, but because **human attention at every handoff** is
the bottleneck. Existing AI coding tools assist a human at one handoff. AORC
removes the handoffs.

### The one-line philosophy

> **LLMs propose; plain code disposes.**

Every place an LLM produces output, a *mechanical* gate — a JSON schema
check, a set comparison, a process exit code, a regex, a git verdict —
decides what happens next. No LLM ever routes its own output, judges its own
work, or decides whether it succeeded. This single rule explains most of the
codebase's shape, and it is the thing to preserve above all else.

Concretely:

| The LLM does | The mechanical gate that judges it |
|---|---|
| Writes a design doc | `design.parse_design_response` — strict JSON schema, missing field = `None` |
| Writes tests | `tester.interface_coverage_gate` — pure set comparison of interface names vs. names the test code calls |
| Runs those tests | `tester.classify_test_run` — string markers in pytest output decide red / error / green / infra-fail |
| Writes implementation | `coder.parse_coder_response` — path must be in the design's `files` list, or the whole response is rejected |
| Rewrites a shared file | `coder.missing_preserved_names` — every pre-existing top-level name must still be defined afterwards |
| Reviews the diff | Smoke + coverage gates run **first**, mechanically; the reviewer LLM is only asked if those pass |
| Says "rebase is fine" | Nobody asks it. `gitops.LocalGitOps.rebase` returns git's own verdict |

### Current status, stated honestly

**A working prototype.** The pipeline runs end to end against a live sandbox
repo and has been exercised on real multi-issue runs (the sandbox is
`rajatsxngh/aorc-sandbox`; see `sandbox*.aorc.yml`). Slices S31–S49 in the
git log are almost all fixes for bugs that only appeared *under live runs*
while the unit suite was green.

It is not a production system. The gap between "implemented and unit-tested"
and "wired into the live path" has been the single most productive source of
bugs in this project, and several components are still on the wrong side of
that line. Part 5 lists every one of them, verified against the source.

---

## Part 2 — Setting it up from scratch

### 2.1 Prerequisites

- **Python 3.12** (project requires ≥3.11), provided here via
  [uv](https://github.com/astral-sh/uv).
- **Docker**, if you use the default `container.runtime: docker`.
- **A target repository** — a throwaway sandbox, not something you care
  about. AORC branches, commits, comments, labels, and opens PRs on it.
- **A GitHub token** with write access to that repo (dev path), or a
  registered **GitHub App** (production path).
- **An LLM provider key** (Anthropic or OpenAI-compatible).

### 2.2 Local environment

```sh
export PATH="$HOME/.local/bin:$PATH"      # uv
uv venv --python 3.12                      # create .venv (once)
uv pip install -e ".[dev]"                 # pytest etc. (once)
.venv/bin/pytest -q                        # 580 pass, 13 integration deselected
```

The unit suite runs with **zero third-party dependencies** — no network, no
Docker, no environment variables, no real config file. That is deliberate
(see Decision 1) and worth protecting.

Optional extras, each pulling in one real SDK:

```sh
uv pip install -e ".[github]"     # PyGithub — SdkGitHubClient
uv pip install -e ".[claude]"     # anthropic — ClaudeLLMClient
uv pip install -e ".[openai]"     # openai — OpenAICompatibleLLMClient
uv pip install -e ".[apptoken]"   # PyJWT[crypto] — the App-JWT exchange (S23)
uv pip install -e ".[actions]"    # PyNaCl — sealed secrets for the Actions runtime (S25)
```

### 2.3 The target repo's `.aorc.yml`

Every target repo needs one. AORC **never guesses a toolchain** — a missing
or malformed config fails closed with a clear message and a non-zero exit
before anything is constructed (`config.load_config`, `__main__.run`).

A real working example (this repo's `sandbox3.aorc.yml`):

```yaml
llm:
  primary:
    provider: claude
    model: claude-sonnet-4-6
    api_key: $ANTHROPIC_API_KEY
  escalation:
    provider: claude
    model: claude-sonnet-4-6
    api_key: $ANTHROPIC_API_KEY

setup: pip install --break-system-packages -e .   # required
test: pytest                                       # required
lint: echo "no lint"

failure:
  primary_attempts: 3
  escalation_attempts: 1

merge:
  auto: false
```

Full schema, all optional unless marked (`config.parse_config`):

| Key | Default | Meaning |
|---|---|---|
| `llm.primary` | **required** | `{provider, model, api_key, base_url}`. `$VAR`/`${VAR}` expanded from the env; an unset referenced var fails closed. |
| `llm.escalation` | none | Second model slot. Used as the **test-critic** and **reviewer** model in `compose()`, so distinct-critic-model is real when set. |
| `setup` / `test` | **required for building** | Present but empty ⇒ config parses, but `build_blockers` blocks the build pipeline. |
| `lint` | none | Third step of the coder's toolchain run. |
| `smoke` | `[]` | `[{input, expect}]`. Removing it permanently disqualifies the repo from auto-merge (`auto_merge_allowed`). |
| `coverage.command` / `coverage.floor` | none / `80.0` | Coverage gate; skipped entirely if no command. The command owns scoping to changed lines (e.g. `diff-cover`). |
| `merge.auto` | `false` | Opt-in for a future auto-merge path. **No code ever merges a PR today.** |
| `failure.primary_attempts` / `escalation_attempts` | `3` / `1` | Escalation ladder rungs. |
| `dispatch.concurrency` | `5` | Global concurrency ceiling. |
| `clarification.nudge_days` / `block_days` | `7.0` / `7.0` | `infinity` restores wait-forever. |
| `cost.*` | `5 / 50 / 100 / 1.5` | Per-issue, per-run, daily caps + overshoot multiplier. |
| `compute.wall_clock_minutes` | `45.0` | Container wall-clock limit. |
| `container.runtime` | `docker` | `docker` or `actions`. |
| `container.workflow_file` | none | **Required** when `runtime: actions`. |
| `runner` | omitted | Only `default` is accepted; anything else fails closed (never opt into a larger runner). |

### 2.4 Environment variables

Read **only** at the composition root (`src/aorc/__main__.py`):

| Variable | Needed for |
|---|---|
| `GITHUB_TOKEN` | `SdkGitHubClient` — the orchestrator's own issue/label/PR operations. Also the token every container gets under `--dev-pat-minter`. |
| `AORC_REPO` | `owner/repo` (or pass `--repo`). |
| `AORC_BASE_IMAGE` | The Docker image per-issue containers start from. Required when `container.runtime` is `docker`. |
| `AORC_GITHUB_APP_ID` + `AORC_GITHUB_APP_PRIVATE_KEY_PATH` | The real per-issue token minter (production path). Skipped by `--dev-pat-minter`. |
| `AORC_WEBHOOK_SECRET` | Only `python -m aorc serve`. Read once, never logged. |
| Whatever `.aorc.yml`'s `llm:` block references | e.g. `ANTHROPIC_API_KEY`. |

### 2.5 The base image

`Dockerfile` at the repo root builds the per-issue container image: Node 22 +
Claude Code + skills + Python 3.12 + uv + pytest, running as a non-root
`agent` user. Build and point `AORC_BASE_IMAGE` at it:

```sh
docker build -t aorc-base .
export AORC_BASE_IMAGE=aorc-base
```

Note the image is *not* currently where the LLM agent loop runs — it is where
the **toolchain** (`setup`/`test`/`lint`/coverage) runs, via `docker exec`
(S27). See "Known open limitation" in Part 5.

### 2.6 Fast path: run it with a PAT (dev)

This skips GitHub App registration entirely and is how the live testing so
far was done.

```sh
# One issue, end to end — the predictable path
python -m aorc --dev-pat-minter --config sandbox.aorc.yml --repo <owner>/<repo> run-issue 42

# All open issues, applying triage + collision + concurrency rules
python -m aorc --dev-pat-minter --config sandbox.aorc.yml --repo <owner>/<repo> backfill

# One cron tick: sweep held issues, re-queue expired tokens
python -m aorc --dev-pat-minter --config sandbox.aorc.yml --repo <owner>/<repo> wake
```

`--dev-pat-minter` makes every per-issue container token the fixed
`GITHUB_TOKEN` PAT (`__main__.pat_passthrough_minter`) instead of a minted,
scoped, expiring one. **Never use it for a live run** — it discards the
entire credential model described in Decision 12.

`--no-container` is the other escape hatch: runs `setup`/`test`/`lint` on the
host via `SubprocessTestRunner` instead of `docker exec`. Also never for live
use — untrusted generated commands then run as your own user.

### 2.7 Production path: register the GitHub App

Manual, not automated by any AORC code.

1. **Settings → Developer settings → GitHub Apps → New GitHub App** on the
   target org/user.
2. Repository permissions: `Contents: Read & write`, `Issues: Read & write`,
   `Pull requests: Read & write` — matching `credentials.MINIMAL_PERMISSIONS`
   exactly. Add `Projects: Read & write` for the board, and `Actions: Read &
   write` only if this repo will use `container.runtime: actions`.
   (Broader App permissions buy nothing: `CredentialBroker.mint` narrows
   every per-issue request down to `MINIMAL_PERMISSIONS` anyway, and GitHub
   enforces the App's own grant as the hard ceiling.)
3. Subscribe to `issues`, `issue_comment`, `pull_request`,
   `pull_request_review_comment`, `push`, `repository_dispatch`
   (`install.APP_WEBHOOK_EVENTS`).
4. Generate and download the private key (PEM). Store it outside version
   control; point `AORC_GITHUB_APP_PRIVATE_KEY_PATH` at it.
5. **Install** the App on the target repository. Note the App ID for
   `AORC_GITHUB_APP_ID`.
6. `uv pip install -e ".[apptoken]"`.

Then:

```sh
python -m aorc install    # board + labels + .aorc.yml config PR + immediate backfill
```

`install` creates the six board columns, creates every pipeline label up
front (so a later label write can never 404), opens the config PR with the
`.aorc.yml` template **and** the auto-rollback workflow, then runs the
first-run backfill. Until that config PR merges, every dispatch-ready issue
is parked under `awaiting-config` — the pipeline refuses to build against
guessed defaults (PRD B23).

### 2.8 Webhook receiver

```sh
export AORC_WEBHOOK_SECRET=<the App's webhook secret>
python -m aorc serve --host 0.0.0.0 --port 8080
```

Verifies `X-Hub-Signature-256` with `hmac.compare_digest` **before parsing
the body** — a missing or wrong signature is a 401 and nothing is routed.
Verified deliveries are ACKed (200) immediately, then routed through
`install.route_webhook`. Redelivery is harmless: `wake.claim_event`'s
`(issue, stage, head_sha)` comment-marker dedup is the idempotency layer.

Dev loop with no public URL:

```sh
npx smee-client --url <channel> --target http://localhost:8080/webhook
# or: ngrok http 8080
```

### 2.9 The local dashboard (optional, branch `ui-build`)

```sh
.venv/bin/pip install -r ui/bridge/requirements.txt
.venv/bin/uvicorn ui.bridge.app:app --port 8000    # then open http://localhost:8000
```

A FastAPI bridge (`ui/bridge/app.py`) that **imports AORC's own adapter and
label rules** (`SdkGitHubClient` + `aorc.pipeline`) to derive state exactly
the way AORC does, plus Run / Release / Backfill buttons that shell out to
the same CLI commands you'd type yourself. It reimplements no pipeline logic
and touches nothing under `src/aorc/`. Credentials come from the repo-root
`.env` it loads itself.

This is an additive layer on a separate branch. It is not required to
operate AORC.

---

## Part 3 — How it works

### 3.1 The pipeline

```
Setup → Triage → [Clarify | Decompose] → Design → ⟨checkpoint⟩ → Test → Code → Review → PR → (human merges)
```

| Stage | Module | What happens |
|---|---|---|
| **Dispatch** | `wake.dispatch_issue` | Mint a per-issue token → broker builds the container env → harness ensures the worktree and starts the container. |
| **Triage** | `triage.triage` | One LLM call: `actionable` or `not-ready`. `epic` vs `vague` is a *mechanical* hint (an `epic` label or a `- [ ]` task-list body), never an LLM opinion of size. |
| **Clarify** | `clarification.ClarificationStage` | Vague issues get interrogated one question at a time as issue comments. Resume requires **both** write-level `author_association` *and* the LLM re-judging the whole conversation as clear. |
| **Decompose** | `decomposition.DecompositionStage` | An epic becomes a PRD + scoped sub-issues in one JSON response. Idempotent by hidden `aorc:parent=N` / `aorc:sub-index=I` markers. Too-vague-to-decompose routes to clarification instead. |
| **Design** | `design.DesignStage` | Emits strict JSON: `interface`, `test_specs`, `task_list`, `files`, `confidence`. Committed to `aorc/issue-<n>/design.md` on the issue branch. |
| **Checkpoint** | `harness.Checkpoint` via `driver` | The declared `files` are compared against every other in-flight issue's claim and every open AORC PR's changed files. Overlap ⇒ **hold**. |
| **Test** | `tester.TesterStage` | Tester LLM writes one failing test per `test_specs` entry → static interface-coverage gate → a **distinct** critic LLM reviews → commit → run → must classify **red**. |
| **Code** | `coder.CoderStage` | Coder LLM writes full file contents for the design's `files` only, runs `setup`/`test`/`lint`, retries with the failure output until green or the cap. |
| **Review** | `reviewer.ReviewerStage` | Smoke → coverage → reviewer LLM, in that order. Any failure re-enters the *coder's* fix loop. All gates green in one attempt ⇒ open the PR and post the whole attempt trail on it. |
| **Merge-time** | `merge.MergeTimeHandler` | On merge: close the issue, delete the branch, re-check every overlapping open PR for staleness, sweep the held queue. |

Nine distinct agent roles exist across the pipeline. A clean run makes six
LLM calls: triage, design, tester, test-critic, coder, reviewer. Only the
Test stage has a critic pair.

### 3.2 State lives in GitHub

There is **no database and no saved state**. Everything the orchestrator
needs to remember is in GitHub:

| GitHub thing | What it stores | Code |
|---|---|---|
| **Labels** | Which stage an issue is at | `pipeline.py:18–45` |
| **Board column** | *Derived* from the label, never set directly | `pipeline.LABEL_COLUMN` |
| **Branch name** | `aorc/issue-<n>` — a deterministic scheme is what lets a stateless orchestrator map a PR back to its issue with no DB (PRD B24) | `pipeline.branch_name` / `wake.issue_for_branch` |
| **Committed artifacts** | Design doc, tests marker, open PR — a stage is complete only when its artifact exists | `pipeline.ArtifactChecker` |
| **Hidden HTML comment markers** | Idempotency keys, hold reasons, clarification turns, handled-feedback records | `wake.event_marker`, `driver.HELD_PING_MARKER`, … |

Labels:

- Stages: `in-design` → `in-test` → `in-code` → `in-review`
- Waiting on a **human**: `needs-clarification`, `agent-blocked` (nothing
  auto-releases these)
- Waiting on the **system**: `aorc-held` (collision/blocker — auto-released
  by the sweep), `awaiting-config` (auto-released when the config PR merges)

`PipelineStateMachine.advance` refuses to move an issue forward if the
current stage's artifact isn't actually committed. `resume_stage` reconstructs
the true stage from labels + artifact presence, so a crash between "label
moved" and "artifact committed" is caught on restart rather than silently
skipping work.

### 3.3 The runtime model

- **One git worktree per issue** at `.aorc-worktrees/issue-<n>`, on branch
  `aorc/issue-<n>` (`harness.WorktreeManager`).
- **One container per build attempt**, not per issue lifetime. Held ⇒ the
  container is destroyed; released ⇒ a fresh one, with the worktree re-synced
  onto the current `main`.
- **The git branch is the state carrier across hold and release — never the
  container.** This is the single most load-bearing idea in the runtime
  model. Everything a stage produces is committed to the branch, so any
  resume reads it back from GitHub.
- Worktrees are built from a checkout of the **target** repo. If the
  directory AORC is pointed at isn't one, it materializes its own clone at
  `.aorc/clone` and says so on stderr (`harness.ensure_target_clone`, S35 —
  before this, containers ran AORC's own test suite).
- Every API commit is mirrored into the worktree with the exact bytes just
  committed (`harness.write_worktree_file`, S22), because `GitHubClient` is a
  content API with no fetch surface and the toolchain runs against the
  worktree.

### 3.4 Credentials

```
App private key  →  lives ONLY in the orchestrator (CredentialBroker)
       ↓ mint(issue, repo, MINIMAL_PERMISSIONS)
per-issue token  →  single repo, contents/issues/pull_requests write, ~1h TTL
       ↓ container_env()
container env    →  { GITHUB_TOKEN, AORC_LLM_API_KEY }   ← nothing else, ever
```

- `mint` **enforces** the permission ceiling — a request for a scope outside
  `MINIMAL_PERMISSIONS`, or at a higher level, raises
  `PermissionCeilingError` before the exchange. Not advisory.
- `container_env` and `assert_env_clean` raise `CredentialLeakError` on any
  PEM-shaped value, so the private key structurally cannot be injected — and
  `ContainerHarness.dispatch` re-checks every incoming env, so a hand-built
  dict can't smuggle it past the broker.
- **There is no token refresh path.** On expiry, `handle_token_expiry` tears
  the container down through the branch-preserving path and reports
  `re-queue`; the wake loop re-dispatches with a freshly minted token and the
  issue resumes from its last committed artifact. A container *cannot*
  re-authenticate, because minting needs the key it was never given.
- Secrets never ride in argv: Docker gets a 0600 temp `--env-file` deleted
  the moment `docker run` returns; git gets the token via
  `GIT_CONFIG_KEY_0`/`extraheader`, never in a remote URL; the Actions
  runtime seals each value (libsodium sealed-box) into a repo secret and
  deletes it on teardown.
- `ScrubbingGitHubClient` wraps the real client **exactly once**, at the
  composition root, and blanks known credential shapes (gitleaks/trufflehog
  patterns) on every agent-authored write surface — comments, issue/PR text,
  committed content, label names, branch names.

### 3.5 Concurrency and collisions

Two gates, deliberately at different times:

1. **Before any container starts** (`dispatch.select_dispatch`) — cheap and
   deliberately dumb: the global concurrency ceiling (default 5) plus
   declared blockers (`blocked by #N` in the body, or an un-decomposed epic).
   No collision prediction from issue text: that needs real file lists.
2. **After design** (`harness.Checkpoint.verdict`) — the real rule. The
   design's declared `files` are intersected (path-normalized) against every
   other in-flight issue's claim *and* every open unmerged AORC PR's changed
   files (PRD A3). Overlap ⇒ `aorc-held` + an explanatory comment. If a
   `GraphifyClient` is configured, either side's import/call blast radius
   reaching the other also counts, and a **failed** blast-radius query holds
   conservatively rather than proceeding.

The in-flight registry is in-process and dies with the orchestrator, so every
wake rebuilds it from GitHub first (`design.rebuild_in_flight_registry`, via
`wake._adopt_in_flight_claims`) — reading each in-flight issue's committed
design doc. Without that, a restarted process checks an empty registry and
detects nothing (this was live bug S43).

Verified live: two issues touching the same file were correctly sequenced
while an independent issue ran in parallel, and the released issue's output
contained the merged changes plus its own.

### 3.6 Merge-time and rollback

- **Merged AORC PR** ⇒ close the issue, move the card to Done, delete the
  branch, re-check every *other* open AORC PR whose files overlap (rebase →
  re-test → fix loop if broken → reviewer re-run), reindex Graphify if
  present, then run the wake sweep.
- **Human PR comment** ⇒ one LLM call classifies intent as exactly `code` or
  `spec`, then routing is pure: `code` re-enters the coder's fix loop with
  the comment as feedback (tests stay locked structurally); `spec` relabels
  `in-design` and re-dispatches. The agent's own comments are filtered by
  author + marker prefix so the trail can never self-trigger; each human
  comment is handled exactly once via a `feedback_marker`.
- **Main goes red after a merge** ⇒ the generated `aorc-rollback.yml` runs
  the repo's own `setup`/`test`, reverts the offending commit, pushes, and
  fires an `aorc-main-broken` `repository_dispatch`, which
  `route_webhook` sends to `MergeTimeHandler.on_main_broken`. Every in-flight
  container is then triaged against the reverted file set using its
  *committed* checkpoint claim: overlap ⇒ tear down and re-queue against the
  corrected HEAD; no committed design doc yet ⇒ conservatively re-queue
  (non-overlap can't be proven).
- **Rebase conflicts are never auto-resolved.** Anywhere a rebase conflicts —
  worktree sync on release (S45), stale-PR recheck, PR-open — the operation
  aborts, the tree is left exactly where it was, and the issue is labeled
  `agent-blocked` with the conflict output. Escalating honestly is the
  correct outcome, not a failure.

---

## Part 4 — Architectural decisions, and why

These are the decisions worth understanding before changing anything. Each
one has a *why* that is not obvious from the code alone.

### 1. Two seams, and only two

`interfaces.py` defines `GitHubClient` (~25 methods) and `LLMClient` (one
method: `complete`). Orchestrator logic imports from that module and nothing
else. PyGithub, `anthropic`, and `openai` live behind adapters
(`github/sdk_adapter.py`, `llm/claude_adapter.py`, `llm/openai_adapter.py`),
all lazily imported.

**Why:** the whole pipeline becomes testable with in-memory mocks. 580 tests
run with no network, no Docker, no credentials, in ~12 seconds. That is what
made it possible to build 49 slices without a live environment at every step.

**Enforcement:** `tests/test_no_sdk_imports.py` fails the build if core code
ever imports an SDK. This is a real guard, not a convention.

**The trade-off, learned the hard way:** the mocks are more permissive than
reality, and *that gap is where the bugs live*. `MockGitHubClient.commit_file`
stores content whether or not the branch exists — so "commit to a branch
nobody created" was invisible to the entire suite until a live run 404'd on
the very first stage (S29). Likewise the Docker mount bug (S30) and the
wrong-clone bug (S35). **Any new adapter behavior should be assumed
mock-invisible until proven otherwise.**

### 2. No hardcoded models or secrets — config or nothing

Everything comes from `.aorc.yml` and the environment variables it
references. A missing, malformed, or partial config **fails closed**: clear
message, non-zero exit, nothing partially started.

**Why:** "probably `pip install`?" is exactly the hallucination the config
file exists to prevent. An unattended agent guessing your toolchain and
confidently reporting green is the worst possible failure mode.

**Consequence:** the pipeline can't run at all until the install config PR
merges. Issues pile up under `awaiting-config` in the Blocked column with an
explanatory comment. That's the intended behavior, not a bug.

### 3. GitHub is the database

Pipeline state is labels. The board column is *derived*. Idempotency keys are
hidden HTML comments. Stage completion is artifact presence on the branch.
The orchestrator holds no memory between wakes.

**Why:** one version of the truth, visible to anyone reading the repo, with
no hidden bookkeeping that can silently disagree with reality. It also means
a crashed orchestrator loses nothing — the next wake re-reads everything.

**Cost:** messy labels mean messy state, and there is no atomic
compare-and-swap without a DB. Two containers *can* spin up before either
commits. The artifact-presence check makes that **wasteful** (one redundant
pass), never **wrong** (no double work survives to merge). Accepted for v1;
a DB-backed CAS is a v2 concern. It also produced open ticket S47 (stale
labels), which is worse than cosmetic — a stale `aorc-held` on an
actively-running issue makes the sweep re-dispatch it.

### 4. Tests are written before the implementation, always

The tester writes failing tests from the design's `interface` and
`test_specs` **only** — never `task_list` (the coder's contract), never repo
files, never implementation code. The coder never sees test source at all;
it only learns whether the toolchain passed and what the error output was.

**Why this is the keystone:** a pre-written failing test is an objective
definition of "done" that neither the model nor the operator can talk their
way around. It also makes the system reviewable by someone not reading every
diff — you watch tests go red → green rather than auditing generated code
line by line.

**Test locking is structural, not conventional:** `parse_coder_response`
rejects any response whose `path` isn't in the design's `files` list, and the
generated test path is deliberately never in that list. The coder *cannot*
edit tests; it's a schema constraint, not a rule to remember.

### 5. Red is not the same as error

A test that **fails** because the feature doesn't exist yet is correct and
expected. A test that **errors** because it can't even be collected is a
broken test, and the stage refuses to advance on it. The discriminator is
structural — string markers in pytest output (`ImportError`,
`ModuleNotFoundError`, `collected 0 items`, …), not an LLM's opinion.

**The catch-22 this created, and the fix:** tests that run before any
implementation exists naturally die with `NameError`/`ImportError` — which
the classifier correctly calls "error", so the stage could never proceed.
S40 seeds each interface function as a `NotImplementedError` stub and
prepends a deterministic import header, turning "not implemented yet" into a
clean *runtime* failure classified "red", while keeping the error markers
honest for genuinely broken test code. The coder replaces the stub wholesale.

A third class was added in S33: `infra-fail`. `docker exec` against a dead
container returned output carrying none of the error markers, so it
classified as "red" and the tester falsely proceeded. Now dead-target markers
hard-fail immediately — regenerating tests cannot fix a missing container, so
no LLM attempt is spent on it.

### 6. The critic is a different model instance

The test-critic is a **distinct** `LLMClient` from the tester's, and the
reviewer is distinct from the coder's — in real wiring both are the
escalation-tier slot from `.aorc.yml`.

**Why:** no incentive to pass its own work. Combined with the mechanical
interface-coverage gate (a pure set comparison, no execution), a shallow test
suite can't slip through by being plausible.

### 7. One retry mechanism, reused everywhere

Reviewer rejection, smoke failure, coverage shortfall, human PR feedback, and
stale-PR breakage all re-enter **the same** `CoderStage.run` bounded fix loop
via its `review_feedback` parameter.

**Why:** the alternative is four parallel retry mechanisms with four sets of
bugs and four attempt caps to reason about. The feedback slot was added to
the coder specifically so nothing else would grow its own loop. Keep it that
way.

### 8. Design-late collision detection

Collisions are computed *after* design, not predicted from issue text.

**Why:** the declared file list is the first moment there is anything real to
compare. Guessing from issue prose would be an LLM opinion routing an LLM
output — exactly what the philosophy forbids. The cost is that a colliding
issue burns one design pass before being held; that's the accepted price for
a mechanical verdict.

The verdict is also *conservative in both directions*: a failed Graphify
query holds rather than proceeds, and an open PR occupies its files just like
a live container (otherwise two issues pass clean and collide at merge).

### 9. Every build runs in a disposable container, and the branch survives it

Freshly generated code is untrusted code. It executes in a sealed container
from a template image, never on the host — enforced since S27 by running the
toolchain through `docker exec` into the issue's own container rather than a
host subprocess. Before that, the container was a *cosmetic* isolation
boundary: it was started, and then everything ran on the host anyway.

One container per **build attempt**. Held ⇒ destroyed; released ⇒ fresh
container, worktree re-synced onto the current `main` (S45 — without that, a
released issue's full-file rewrites silently dropped the merged changes).

### 10. The human approves every merge

Merging changes the real default branch and is expensive to undo, so that
decision stays with a person. `merge.auto` exists in the config and
`auto_merge_allowed` gates it on *both* the opt-in and a `smoke:` block —
but **no code path calls `merge_pull_request`**. Auto-merge is specified and
deliberately unbuilt.

This gate has already caught a genuine regression before it reached `main`.

### 11. Provider failure is not model failure

`BackoffLLMClient` retries the *same* model on transient `ProviderError`s
(2s / 8s / 30s); only backoff exhaustion counts as one real failure to the
escalation ladder. `FailFastProviderError` — a local `base_url` configured on
a GitHub-hosted runner, where connection-refused will never heal — is never
retried. GitHub 403/429 secondary rate limits are backed off and **never**
counted as a pipeline failure; a plain 403 with no `Retry-After` is a
permissions error and propagates.

**Why:** a flaky network must never burn `primary_attempts` and prematurely
escalate a solvable issue. Separate counters, separate meanings.

### 12. The credential model bounds the blast radius, mechanically

Covered in 3.4. The design principle: container isolation alone is
insufficient — a sealed container holding a broad credential can still reach
every repo that credential can touch. Isolation and credential-scoping are
partners; both are required.

Each rule is enforced by code that raises rather than by documentation:
`PermissionCeilingError`, `CredentialLeakError`, no refresh path existing at
all, argv-free secret delivery.

### 13. `compose()` is the only place real adapters are built

`__main__.compose()` takes every collaborator as an overridable keyword
argument. Real SDK-backed adapters are constructed *only* for the parameters
left `None`. That is how `tests/test_main.py` drives all five subcommands end
to end against mocks with zero deps and no environment.

**Why this matters when you change things:** if you find yourself
constructing an adapter anywhere else in `src/aorc/`, you have broken
invariant #1 and `test_no_sdk_imports.py` will tell you.

### 14. Container runtime is a config choice, not a code default

`ContainerRuntime` has two implementations: `DockerContainerRuntime` (local
container) and `ActionsContainerRuntime` (fires a real `workflow_dispatch`,
resolves the run, cancels it on teardown). Selected by `.aorc.yml`'s
`container.runtime`, constructed only at `compose()`.

The Actions path needed its own credential story: `workflow_dispatch` inputs
are visible in run logs and the runs-list API, so per-issue env values are
sealed against the repo's Actions public key and written as repo secrets
(`AORC_ISSUE_<n>_<KEY>`) immediately before dispatch, then deleted on
teardown — mirroring the Docker env-file discipline.

### 15. Escalate honestly rather than guess

Every hard stop is the same shape: label `agent-blocked`, post a comment with
a greppable HTML marker and the actual reason, keep the branch. Nothing
auto-resolves a conflict, invents a toolchain, or reports success it can't
prove. S31 exists entirely because block reasons used to be swallowed and a
blocked stage looked identical to a clean run.

---

## Part 5 — Implementation status, verified against the source

Every row below was checked against the code, not against the ticket.

### 5.1 Built and live on the dispatch path

| Slice | What | Where | Evidence it's wired |
|---|---|---|---|
| S1 | Two seams + adapters + mocks | `interfaces.py`, `github/`, `llm/` | `tests/test_no_sdk_imports.py` |
| S2 | Label state machine + derived column + artifact gating | `pipeline.py` | Used by `driver.py` at every stage boundary |
| S3 | Triage | `triage.py` | `wake.backfill:314` |
| S4/S20 | Container harness, worktrees, checkpoint, in-flight registry | `harness.py` | `wake.dispatch_issue:378` |
| S5 | Design stage + strict schema + actionability gate | `design.py` | `driver.py:165` |
| S6 | Tester + critic + red/error gate + interface coverage | `tester.py` | `driver.py:200` |
| S7 | Coder bounded fix loop + preservation guard | `coder.py` | `driver.py:216` |
| S8 | Reviewer + coverage gate + PR open | `reviewer.py` | `driver.py:224` |
| S10 | Dispatch selector (concurrency + declared blockers) | `dispatch.py` | `wake.backfill:319`, `_sweep_held:432` |
| S11 | Clarification — **question-asking half only** | `clarification.py` | `install.py:344` (`start` only) |
| S12 | Epic decomposition | `decomposition.py` | `install.py:342` |
| S15 | Credential broker, ceiling, expiry re-queue, scrubbing | `credentials.py` | `wake.dispatch_issue:375`, `WakeLoop.compose:254` |
| S16 | Stateless wake loop, comment-marker dedup, backfill | `wake.py` | `__main__.run` |
| S17 | Merge-time close, stale-PR recheck, feedback routing, rollback triage | `merge.py` | `compose():307`, routed by `route_webhook` |
| S18 | Install flow, config gate, label/board creation, webhook routing table | `install.py` | `__main__.run` `install` |
| S21 | Composition root / CLI | `__main__.py` | The entry point |
| S22 | Pipeline driver + worktree/API mirror | `driver.py`, `harness.write_worktree_file` | `compose():280`, `loop.driver = driver` |
| S23 | Real App-JWT → installation-token minter | `github/app_token.py` | `compose():206` (non-dev path) |
| S24 | Webhook receiver + HMAC verification | `webhook.py` | `__main__.run` `serve` |
| S27 | Toolchain runs inside the issue's container | `tester.ContainerTestRunner` | `compose():253` when runtime is `docker` |
| S29–S49 | The live-run fix series | various | See git log; each is a one-line commit subject |

The S29–S49 series is worth reading as a group — it is the record of what
breaks when a mock-tested system meets reality: branch creation before first
commit (S29), absolute Docker mount paths (S30), block reasons surfaced
(S31), markdown code fences stripped before every JSON parse (S32),
dead-container detection (S33), coder `finish_reason` in format misses (S34),
worktrees built from the *target* repo (S35), tester keyed to `test_specs`
(S36), setup run before the tester's attempt loop (S37/S38), generated tests
written into the project's `tests/` root so pytest actually collects them
(S39), interface stubs (S40), import normalization (S41), design paths
resolved against the worktree (S42), the checkpoint wired into the live
dispatch path (S43), the coder's preservation guard (S44), released-issue
worktree sync (S45), and new-file design paths snapped into the matching
source directory (S49).

### 5.2 Built and tested, but with **zero live callers**

These are real, unit-tested library pieces that nothing on the dispatch path
calls. Confirmed by grep across `src/`.

| Component | Where | Status |
|---|---|---|
| **Cost circuit-breakers** (per-issue / per-run / daily, overshoot) | `guards.CostGuard` | Only `BLOCKED_LABEL` is imported from this module anywhere. **No spend metering runs.** There is also no persistence for the daily total. |
| **Wall-clock compute limit** | `harness.ContainerHarness.enforce_wall_clock` | Implemented and tested; called only from `tests/test_harness.py`. Runaway containers are bounded only by token expiry. |
| **Escalation ladder** (primary ×N → escalation ×M → block) | `escalation.EscalationLadder` | Only referenced by its own tests. `BackoffLLMClient` *is* wired (`compose():153`); the ladder is not. |
| **GitHub rate-limit backoff** | `escalation.RateLimitedGitHubClient` | Never composed. `compose()` wraps only `ScrubbingGitHubClient`. |
| **Clarification reply → resume** | `ClarificationStage.handle_comment`, `.check_timeout` | No callers. A plain issue comment triggers `loop.wake()` (`install.py:502`); the issue only re-enters after a human clears the terminal label. `install.py`'s own module docstring documents this. |
| **Epic parent auto-close** | `decomposition.check_parent_complete` | Library function; nothing fires it when a sub-issue closes. |
| **Graphify** | `graphify.py`, `graphify_adapter.MCPGraphifyClient` | The seam and a real MCP adapter exist and every consumer handles it, but `compose()` never constructs one — so collision detection is path-intersect only, and no reindex happens on merge. |
| **Auto-merge** | `config.auto_merge_allowed` | Gate function tested; **nothing calls `merge_pull_request`**. Deliberate. |
| **Smoke gate** | `reviewer.ReviewerStage._run_mechanical_gates` | Needs both `smoke_examples` *and* a `smoke_command` template. `.aorc.yml` has no `smoke_command` field, so `compose()` passes none and the gate is always skipped live. |

### 5.3 Known behavioral gaps in the live path

| Gap | Detail |
|---|---|
| **Container teardown** | Only the *held* path tears down (`wake.py:404`, `merge.py:341`, `credentials.py:201`). Containers from successful or blocked runs linger until token expiry. |
| **Concurrency ceiling** | Enforced only where `select_dispatch` is called: `backfill`, `_sweep_held`, `_release_awaiting`. A direct `run-issue` bypasses it entirely. |
| **Generated test accumulation** | `tests/test_aorc_issue_<n>.py` is committed to the target repo per issue and accumulates over runs. Nothing prunes them. |
| **Import path derivation** | `tester.implementation_module` derives the test's import header from the design's declared path. S42/S49 snap unresolvable paths to real ones where possible, but nothing validates that the derived module is actually reachable from the repo's declared source roots — an unresolvable declaration still produces an unimportable module. |
| **Push mediation** | See below. |

### 5.4 The one open architectural limitation

A container holding its per-issue `GITHUB_TOKEN` can push or call the API
directly, **bypassing the orchestrator-side `ScrubbingGitHubClient`** —
layer-2 scrubbing covers orchestrator-mediated writes only.

Carried S15 → S16 → S18 → S19. S19 closed the `docker run -e` host-`ps`
exposure half (env now via a 0600 `--env-file`). S27 closed the *toolchain*
half (setup/test/lint now genuinely run in-container). But S27 was explicitly
scoped to the toolchain: **the LLM agent loop still runs orchestrator-side,
not inside the container.** Closing this properly means moving that agent
loop in-container and putting a scrubbing egress proxy in front of it — which
v1 never builds. This is the first work item for any v2.

### 5.5 Open tickets, verified

| Ticket | Real status |
|---|---|
| `issues/25-actions-execution-wiring.md` | **Genuinely open, partially done.** `ActionsContainerRuntime` + config-driven runtime selection + the sealed-secret delivery path + an `actionlint` CI job + a credential-gated integration test all exist. **Not done:** the live sandbox exercise (red main → auto-revert → `repository_dispatch` → `on_main_broken`) has never been run; the `actionlint` job has never been observed to execute; the integration test has never run against a real repo. The fragile pieces the ticket names — PR-number extraction under **squash merges**, the `git revert -m 1 \|\| git revert` fallback, whether `github.token` pushes trip branch protection — are unverified and unfixed in `install.ROLLBACK_WORKFLOW`. |
| `issues/29-create-branch-before-commit.md` | **Actually fixed** (commit `645b112`) — `driver.py:139` and `install.py:445` both call `create_branch` before the first `commit_file`. The ticket file was added in the same commit and never moved to `done/`. **Bookkeeping only.** |
| `issues/30-docker-mount-absolute-path.md` | **Actually fixed** (commit `bf0fbfa`) — `harness.py:168` resolves `os.path.abspath(worktree_path)` at the mount boundary. Same stale-filing situation. **Bookkeeping only.** |
| `issues/46-backfill-does-not-release-held-issues.md` | **Open and confirmed in code.** `backfill()` (`wake.py:298`) never calls `_sweep_held`; `_already_in_flow` (`wake.py:355`) skips anything carrying `HELD_LABEL`. The only caller of `_sweep_held` is `wake()`. So the command documented as "re-sync" silently ignores half the re-sync. |
| `issues/47-stale-state-labels-not-cleaned-up.md` | **Open and confirmed in code.** `_close_merged_issue` (`merge.py:351`) never removes the pipeline label; only `_sweep_held` (`wake.py:439`) removes `HELD_LABEL`, so `run-issue`/`dispatch_issue` stack stage labels on top of a stale hold. Because `rebuild_state` (`wake.py:157`) classifies `HELD_LABEL` **first**, an actively-running issue wearing a stale `aorc-held` is treated as held on every wake and is eligible to be dispatched again. Not cosmetic. |
| `issues/48-backfill-winners-stall-at-in-design.md` | **Open, root cause undiagnosed.** Backfill discards every `DriverResult` (`wake.py:325`), and S31's stage/status/reason surfacing was added to `run-issue` only — so under backfill a blocked or held design stage looks identical to a clean run. Step 1 of the ticket (surface the result) is what turns the next repro from a mystery into a message. |

**Slices S31–S45 and S49 have no ticket files** — they exist only as commits.
If ticket-level traceability matters to you, that's a gap to backfill.

### 5.6 Test suite

```sh
.venv/bin/pytest -q
# 580 passed, 13 deselected, 2 warnings in ~12s
```

The 13 deselected are `tests/integration/` — real adapters, gated on
credentials or a Docker daemon, selected in CI via `pytest -m integration`.
Each skips cleanly (never fails) when its credential is absent, so CI stays
green on forks.

CI (`.github/workflows/ci.yml`) has three jobs: `unit` (zero-dep),
`integration` (all SDK extras + secrets), and `actionlint` (renders
`install.ROLLBACK_WORKFLOW` to a file and lints it statically). Per ticket
S25, the `actionlint` job has never been observed to run.

**The most important thing to know about this suite:** it runs entirely
against fakes, which is fast and hermetic and is also precisely why S29, S30,
S33, S35, S39, S42, S43, S44 and S45 surfaced only under live runs. When you
add behavior, ask specifically "would the mock let this bug through?"

---

## Part 6 — Where this goes next

### 6.1 Immediate — finish v1.5 (the go-live glue)

In dependency order:

1. **S48 first** — surface `DriverResult` (stage/status/reason) in
   `BackfillReport` and the `backfill` CLI output. It's a small change that
   makes the next two tickets diagnosable instead of mysterious.
2. **S46** — make `backfill()` run the same held-queue sweep `wake()` runs,
   post-`_adopt_in_flight_claims`, and report released issues.
3. **S47** — one label-transition helper enforcing "at most one AORC state
   label at a time", applied at every `add_label` site. This is the one with
   a correctness consequence, not just tidiness.
4. **S25's live half** — a throwaway repo with `.aorc.yml` + the generated
   rollback workflow merged in; exercise red main → auto-revert →
   `repository_dispatch` → `on_main_broken`; fix PR-number extraction for
   squash merges; confirm the `actionlint` job and the Actions integration
   test actually run.
5. **Bookkeeping** — move `issues/29` and `issues/30` into `issues/done/`;
   write retroactive tickets (or a single changelog) for S31–S45 and S49.

### 6.2 Near-term — close the wired-but-unused gaps

Each of these is "the component works; the wiring is where the bug lives":

- **Cost metering.** `CostGuard` needs a caller and a running total. Requires
  the adapters to report token usage back through `Completion` (the `raw`
  field already carries the provider response) and a place to accumulate the
  *daily* figure across wake cycles — the one genuine persistence need in the
  whole system.
- **Wall-clock enforcement.** `enforce_wall_clock` needs the wake loop to
  track each container's start time and call it on every tick. Small, and it
  closes the "runaway container bounded only by token expiry" hole.
- **Clarification round trip.** Route `issue_comment` deliveries on
  `needs-clarification` issues into `ClarificationStage.handle_comment`
  instead of a blanket `loop.wake()`, and drive `check_timeout` from the
  cron tick. The stage is fully built; only the routing is missing.
- **Escalation ladder.** Wrap the design/tester/coder stage passes in
  `EscalationLadder.run` so a hard issue actually gets retried on the
  escalation model before hitting `agent-blocked`.
- **Rate-limit decorator.** Wrap `SdkGitHubClient` in
  `RateLimitedGitHubClient` at `compose()` — one line, real protection.
- **Graphify.** Construct an `MCPGraphifyClient` at `compose()` when
  configured, and pass it to the harness (blast-radius collisions), the
  design stage (context), and the merge handler (reindex on merge). Every
  consumer already handles `ok=False` conservatively.
- **Container teardown on success and block**, plus a concurrency check on
  the direct `run-issue` path.
- **Generated test hygiene** — decide whether per-issue test files should be
  merged, renamed into the project's own conventions, or pruned.

### 6.3 v2 — the architectural work

1. **Move the agent loop into the container.** This is the big one. Today the
   LLM stage sequencing runs orchestrator-side and only the toolchain runs
   in-container, which means the per-issue token can bypass the scrubbing
   client. Doing it properly means an in-container driver plus a scrubbing
   egress proxy in front of it — real push mediation, not just log scrubbing.
2. **A persistence layer, narrowly scoped.** Not a state store — GitHub stays
   the source of truth — but a DB-backed compare-and-swap would close the
   two-containers race, and the daily cost total needs somewhere to live.
   Resist letting it grow into pipeline state.
3. **Multi-tenant / cloud.** The concurrency ceiling is global because v1 is
   one machine. Per-repo limits, higher ceilings, and a hosted orchestrator
   are the same project.
4. **Auto-merge, if ever.** The gate function and the config opt-in exist.
   The graduation criteria should be earned on data (how often did the human
   merge unchanged? how often did the human catch something?), not enabled on
   a hunch.
5. **Import-path validation.** The one place where a wrong LLM output can
   still propagate silently instead of failing loudly: validate that the
   derived implementation module is reachable from the repo's declared source
   roots, and block loudly when it isn't. (There is an agreed plan for this —
   resolve → repair → validate → block loudly — with the S49 diff as its
   first part.)

---

## Part 7 — Orientation for the new owner

### Repo layout

```
src/aorc/
  __main__.py      CLI + compose() — the ONLY place real adapters are built
  interfaces.py    the two seams; everything else imports from here
  config.py        .aorc.yml parsing, fails closed
  pipeline.py      label state machine, derived column, artifact checker
  triage.py        actionable / not-ready
  clarification.py grill-me interrogation
  decomposition.py epic → PRD + sub-issues
  design.py        design stage, schema, path resolution, registry rebuild
  tester.py        tester + critic + red/error classifier + stubs + runners
  coder.py         bounded fix loop, locked tests, preservation guard
  reviewer.py      smoke → coverage → reviewer → PR open
  driver.py        sequences the four stages for one issue
  harness.py       containers, worktrees, checkpoint, target-repo clone
  wake.py          the stateless loop: dispatch, sweep, backfill, expiry
  merge.py         merge-time, stale PRs, human feedback, rollback
  install.py       App manifest, install flow, config gate, webhook routing
  webhook.py       HMAC-verified HTTP receiver
  credentials.py   broker, permission ceiling, scrubbing, expiry
  guards.py        cost + compute circuit breakers (no live callers)
  escalation.py    backoff, ladder, rate-limit decorator (ladder unwired)
  graphify.py      knowledge-graph seam (+ graphify_adapter.py)
  gitops.py        LocalGitOps — real rebase/revert
  github/          SDK adapter, mock, App-token exchange, Actions runtime
  llm/             factory, Claude adapter, OpenAI-compatible adapter, mock
tests/             580 unit tests + 6 credential-gated integration files
ui/                dashboard bridge + frontend (branch ui-build, additive)
```

### Reading order for a first pass

1. `interfaces.py` — the two seams. Everything else is downstream of this.
2. `pipeline.py` — how state works.
3. `driver.py` — the actual sequence, with S42/S43/S44 comments explaining
   why each defensive step exists.
4. `wake.py` — dispatch, sweep, backfill.
5. `__main__.py:compose()` — how it's all wired together.

The module docstrings are unusually load-bearing. Almost every one records
*why* the code is shaped that way, and several record what is deliberately
**not** built. Read them before changing behavior.

### Conventions worth keeping

- **Every hard stop looks the same:** `agent-blocked` label + board column +
  a comment starting with a greppable `<!-- aorc:... -->` marker + the actual
  reason. Grep for `_MARKER` to see the full set.
- **Idempotency is a comment marker**, never a DB row. Same pattern
  everywhere: check for the marker, act, post the marker.
- **Fail closed.** Absent config, malformed config, an unset referenced env
  var, a failed Graphify query, an unresolvable clone — all stop, with a
  message. None of them proceed on a guess.
- **New behavior needs a mock-invisibility check.** Ask whether the fake
  adapter is more permissive than reality at the point you're touching. That
  question would have caught most of S29–S45 before the live run did.

### Things that will bite you

- `--dev-pat-minter` and `--no-container` look convenient and disable the two
  safety systems the project is actually about. They exist for a dev loop
  with no App registered and no Docker; nothing more.
- `.aorc-worktrees/` and `.aorc/` are runtime artifacts (gitignored). A stale
  worktree from an old run is a common source of confusion — the branch, not
  the worktree, is the state carrier, so deleting one is safe and `ensure`
  rebuilds it from the fetched remote.
- `sandbox*.aorc.yml` at the repo root are local test configs for the sandbox
  repo, not part of AORC's own configuration.
- The branches `aorc/issue-11` … `aorc/issue-14` in this repo are leftovers
  from AORC being pointed at itself before S35 fixed the target-repo clone
  bug. They are not meaningful.
