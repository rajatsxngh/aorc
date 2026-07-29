# How AORC Actually Works — a code walkthrough

*A guided tour of the codebase for someone reading it for the first time.
Every claim below points at a real file and line so you can follow along in
the source. Written to be understandable even if you've never seen an
"autonomous agent" codebase before.*

---

## 1. What is this thing?

AORC is a robot employee for a GitHub repository. You write an issue
("add a divide function"). The robot reads it, designs a solution, writes
failing tests, writes code until the tests pass, reviews its own work, and
opens a pull request. A human only ever does two things: **answer questions**
and **click Merge**.

Think of it as a factory assembly line. An issue enters one end; a finished
PR exits the other. There are stations along the line. Each station has a
robot worker (an LLM call) and a mechanical inspector (plain Python, no AI)
that checks the robot's output before anything moves forward.

The core philosophy, in one line: **LLMs propose, plain code disposes.**
Every AI output passes a mechanical gate (a JSON schema check, a set
comparison, an exit code, a git verdict) before anything advances.

---

## 2. The two sacred rules

The whole codebase is shaped by two invariants (see `CLAUDE.md`):

1. **Two seams only.** Orchestrator logic talks exclusively to two abstract
   interfaces defined in `src/aorc/interfaces.py`: `GitHubClient` (~25
   methods: issues, labels, PRs, file contents, board) and `LLMClient` (one
   method: `complete()`). It never imports PyGithub, `anthropic`, or `openai`
   directly. Real SDKs live behind adapters (`github/sdk_adapter.py`,
   `llm/claude_adapter.py`, ...). A test — `tests/test_no_sdk_imports.py` —
   fails the build if core code ever imports an SDK.

   Payoff: the entire pipeline is testable with in-memory mocks. The unit
   suite runs with zero third-party dependencies, zero network, zero env vars.

2. **No hardcoded models or secrets.** Everything comes from `.aorc.yml`
   (parsed by `config.py`) and the environment variables it references. A
   missing or malformed config **fails closed** — clean error, non-zero exit,
   nothing partially started. The app never guesses your toolchain
   ("probably `pip install`?" is exactly the hallucination the config file
   exists to prevent).

---

## 3. GitHub itself is the database

AORC has **no database and no saved state**. Everything it needs to remember
lives inside GitHub:

| GitHub thing | What it stores | Where in code |
|---|---|---|
| **Labels** | Which station an issue is at: `in-design` → `in-test` → `in-code` → `in-review`; plus `needs-clarification` / `agent-blocked` (waiting on a human) and `aorc-held` / `awaiting-config` (parked, auto-released) | `pipeline.py:26-45` |
| **Committed files** | Proof a station finished. Design station's proof: `aorc/issue-N/design.md` on the branch. Test station's: `aorc/issue-N/tests.marker`. Review station's: an open PR. | `ArtifactChecker`, `pipeline.py:86` |
| **Hidden HTML comments** | Sticky notes: `<!-- aorc:event issue=5 stage=x sha=y -->` means "already handled, skip duplicates"; every hold/block also posts a marked comment explaining why | `wake.py:99`, `driver.py:78` |
| **Branch names** | Identity: branch `aorc/issue-42` always belongs to issue 42 — so a merged PR maps back to its issue with a regex, no lookup table | `pipeline.py:48`, `wake.py:86` |

The label alone is never trusted. A crash between "moved the label" and
"saved the work" is caught on restart because the artifact is missing
(`PipelineStateMachine.resume_stage`, `pipeline.py:125`). On every wake the
orchestrator re-reads all of GitHub and rebuilds its picture from scratch
(`rebuild_state`, `wake.py:154`). Crash any time; nothing is lost.

---

## 4. Where the code starts

```
python -m aorc run-issue 42
        │
        ▼
src/aorc/__main__.py          ← what `-m aorc` runs
  main()          :445        ← sys.exit(run())
  run()           :377
    ├─ parse args              :382
    ├─ load_config(".aorc.yml")        → config.py:139  (fails closed)
    ├─ _require_env("AORC_REPO")       :73
    ├─ compose(config, repo)           :115  ← builds EVERY object (see §5)
    └─ subcommand switch               :396
         run-issue → loop.dispatch_issue(42)
```

There are five subcommands, all landing in the same `run()` switch:

- `install` — one-time repo setup: board, labels, config PR, first backfill
- `backfill` — re-sync: triage every open issue not already in the flow
- `wake` — one cron tick: held-queue sweep + token-expiry pass
- `run-issue N` — dispatch a single issue (traced in §6)
- `serve` — webhook receiver (HMAC-verified GitHub deliveries, see §10)

The process **exits when the command finishes**. Nothing stays running —
statelessness is the design, not an accident.

---

## 5. `compose()` — the factory (`__main__.py:115`)

The one and only place real SDK-backed objects are constructed. Nothing runs
here; objects are just wired together, each holding references to the others:

```
compose()
├─ SdkGitHubClient(GITHUB_TOKEN, repo)            :144   real GitHub adapter (§7)
├─ BackoffLLMClient(build_llm_client(primary))    :153   LLM adapter + retry wrapper (§8)
├─ DockerContainerRuntime(AORC_BASE_IMAGE)        :177   or ActionsContainerRuntime (§9)
├─ ensure_target_clone(repo_dir, repo)            :187 → harness.py:278
│     verifies the local checkout's `origin` is the TARGET repo;
│     wrong repo → clones the target into .aorc/clone (fail closed)
├─ WorktreeManager(repo_dir, ".aorc-worktrees")   :188
├─ CredentialBroker(key, build_app_token_minter)  :211   token minting (§7.2)
├─ ConfigGatedWakeLoop.compose(...)               :216 → wake.py:237
│     wraps the GitHub client in ScrubbingGitHubClient EXACTLY ONCE
│     (secret-scrubbing decorator — no downstream code ever holds an
│     unwrapped reference), builds ContainerHarness
├─ if config.setup and config.test:               :232
│     ContainerTestRunner()                       :253   docker-exec runner
│     CoderStage / ReviewerStage / TesterStage / DesignStage
│     PipelineDriver(..., checkpoint=loop.harness.checkpoint)  :280
│         ← the collision gate MUST share the harness's registry
└─ MergeTimeHandler(loop, LocalGitOps, ...)       :307   post-merge logic (§10)
```

Every parameter is overridable by keyword — that's how `tests/test_main.py`
drives all subcommands end-to-end against mocks.

---

## 6. The journey of one issue (`run-issue 42`)

### 6.1 Dispatch gate

The call lands on `ConfigGatedWakeLoop.dispatch_issue` (`install.py:312`)
first:

- **Config gate**: `ConfigGate.check` (`install.py:239`) reads `.aorc.yml`
  from `main` through the GitHub seam. Not merged / invalid / missing
  `setup`/`test` → issue is parked under `awaiting-config` with an
  explanatory comment, and **nothing builds**.
- Gate open → `WakeLoop.dispatch_issue` (`wake.py:357`):

```
├─ token = broker.mint(42, repo)          credentials.py:129
│    permission-ceiling check, then the real App-JWT exchange (§7.2)
│    → one-repo, minimal-permission token, dies in ~1 hour
├─ env = broker.container_env(token)      credentials.py:156
│    {"GITHUB_TOKEN": ..., "AORC_LLM_API_KEY": ...} — leak-checked:
│    anything PEM-shaped raises CredentialLeakError
├─ handle = harness.dispatch(42, env)     harness.py:615
│    ├─ worktrees.ensure(42)              harness.py:409
│    │    first time: git fetch + `git worktree add -b aorc/issue-42
│    │      .aorc-worktrees/issue-42 origin/main`
│    │    re-dispatch: fetch + reset to origin branch + rebase onto
│    │      origin/main (conflict → WorktreeSyncConflict → agent-blocked)
│    └─ runtime.start(...)                harness.py:157
│         secrets → 0600 temp env-file (never argv — argv is `ps`-visible)
│         `docker run -d --name aorc-issue-42 --env-file f
│            -v <worktree>:/workspace <image> tail -f /dev/null`
│         env-file deleted immediately; `tail -f` keeps container alive
└─ driver.run(42)                          ← the pipeline (§6.2)
```

### 6.2 The assembly line — `PipelineDriver.run` (`driver.py:129`)

Reads the issue's label, skips any stage whose artifact already exists
(resume), runs the rest in order. Labels move only through
`PipelineStateMachine.advance` (`pipeline.py:138`), never hand-rolled.

**Station 1 — Design** (`design.py:237`)
The design LLM gets the issue body, any clarification Q&A, and the *current
contents* of files the issue mentions. It must answer in strict JSON:
`interface` (functions + inputs/outputs), `test_specs` (behaviors),
`task_list` (steps), `files` (exact paths), `confidence` (0–1). Routing is
mechanical: broken JSON → retry (3 strikes → `agent-blocked`);
confidence < 0.5 → `needs-clarification`; good → design doc committed to the
branch. That committed doc is the contract everything downstream reads.
`resolve_design_files` (`design.py:101`) then snaps sloppy paths
(`math_utils.py` → `src/sandbox/math_utils.py`) so downstream never inherits
a wrong path.

**Checkpoint — collision gate** (`harness.py:577`)
"Does my file list overlap another in-flight issue's claim, or an open AORC
PR's changed files?" Also asks the Graphify knowledge graph (`graphify.py`)
whether anything *imports* those files (blast radius). Overlap — or a failed
Graphify query (conservative!) → `aorc-held`, container torn down, branch and
design kept. Every merge event re-checks the held queue.

**Station 2 — Tests first** (`tester.py:426`)
Three separate brains so nobody grades their own homework:
1. *Tester LLM* writes one failing test per `test_specs` entry. It sees only
   `interface` + `test_specs` — never implementation, never `task_list`.
2. *Critic LLM* (a distinct model instance) reviews the tests against the
   design; rejections feed back into the tester's retry prompt.
3. *Coverage gate* (pure code): every interface function must appear in the
   test code (`tester.py:276`).

Then the tests actually run — but no implementation exists yet, so they'd
crash with `ImportError`, and a crash is not a proper failure. The fix:
`_seed_stubs` (`tester.py:531`) commits stub functions that
`raise NotImplementedError`, so imports resolve and tests fail *cleanly*.
The classifier (`classify_test_run`, `tester.py:282`) sorts the output with
string markers, zero AI: `red` (clean assertion failure — perfect, proceed),
`error` (broken tests — retry), `green` (passing with no implementation?! —
suspicious, retry), `infra-fail` ("container is not running" — Docker died,
hard stop, don't burn retries).

**Station 3 — Coder fix loop** (`coder.py:197`)
The coder LLM implements `task_list` while **blindfolded from test source** —
it only sees pass/fail + error output, so it can't cheat by hardcoding
expected values. Loop (max 3): write full file contents → run
setup → test → lint (all inside the container) → exit 0 = done; failure text
becomes the next attempt's feedback. Guards:
- schema check rejects writes outside the design's `files` list — tests are
  structurally locked (`coder.py:88`);
- *preservation guard* (`coder.py:132`): every top-level function/class that
  existed before must still exist after (the coder writes whole files — this
  stops it from nuking neighbors' code);
- provider outages retry on a separate counter and never consume attempts.

**Station 4 — Review + PR** (`reviewer.py:146`)
Cheap gates first: smoke examples (if configured), coverage floor (last
`NN%` in the coverage command's output vs the configured floor). Then the
reviewer LLM — the *escalation-tier* model, not the coder's — reads the real
diff (main vs branch, built with `difflib` from two `get_file` calls) against
the design and the original issue. Any failure re-enters the coder's same fix
loop with the failure text as feedback — no second retry mechanism exists.
All green → rebase onto main (`LocalGitOps.rebase`, `gitops.py:37` — real
`git rebase`; a conflict means `agent-blocked`, the robot never freelances a
conflict resolution) → **PR opened**, with the whole attempt history posted
as PR comments.

The robot never merges in v1. Even opting in (`merge.auto: true`) requires a
`smoke:` block — AORC refuses to auto-merge an app it cannot whole-app-verify
(`config.py:88`).

### 6.3 Any hard failure, anywhere

Same channel every time: label `agent-blocked`, post a marked comment with
the stage, the reason, and (via the escalation ladder, `escalation.py:147`)
the full error + last test output. A human reads one comment and knows the
whole story.

---

## 7. How real GitHub API calls work

### 7.1 `SdkGitHubClient` (`github/sdk_adapter.py`) — the translator

The orchestrator says "commit this file"; GitHub speaks HTTPS. This adapter
converts, in both directions, and **GitHub SDK objects never escape it** —
everything exits as plain dataclasses (`Issue`, `Comment`, `PullRequest`).

**Lazy imports.** `from github import Github` happens inside `_client()`
(`:174`) on the first real call, not at the top of the file — so the unit
suite can import this module without PyGithub installed.

**REST calls (most methods)** go through PyGithub, which turns each call into
an HTTPS request to `api.github.com` with the token in the `Authorization`
header. The interesting ones:

- `get_file(path, ref)` (`:273`) → `GET /repos/.../contents/{path}?ref=...`;
  a 404 is caught and returned as `None`. That `None` **is** the pipeline's
  resume signal ("does the design doc exist?" is literally this call).
- `commit_file(branch, path, content, message)` (`:284`) → try
  `get_contents` first; 404 → `create_file` (a `PUT` that makes a real git
  commit server-side — no local git involved); exists → `update_file` with
  the current blob's `sha`. The sha is GitHub's optimistic lock: if someone
  changed the file meanwhile, the write 409s instead of silently clobbering.
- `create_branch(branch)` (`:256`) → check `GET git/refs/heads/{branch}`
  first; already exists → no-op (re-dispatch stays idempotent); 404 → read
  the default branch's sha → `POST git/refs`. Necessary because the contents
  API refuses to auto-create branches.
- `_to_pr` (`:143`) calls `pr.get_files()` — one extra API request per PR —
  so the collision checker can treat an open PR's changed files like a live
  container's claim.

**GraphQL calls (Projects board only).** GitHub sunset the classic Projects
API; Projects v2 is GraphQL-only and PyGithub has no first-class support, so
the adapter carries raw query strings (`:39-129`) and posts them through
`_graphql()` (`:186`). A board "column" is really a single-select **Status**
field: `set_board_column` (`:392`) finds the field + option ids, finds (or
adds) the issue's project item, then fires one mutation. If the token can't
create a Projects v2 board at all (fine-grained PATs can't),
`create_board` catches the auth error (`:20`, `:347`), logs one warning, and
sets `self._project = None` — after which every board op is a silent no-op.
The board is a display-only projection; **labels stay the source of truth**.

### 7.2 Authentication — the App-JWT exchange (`github/app_token.py`)

The orchestrator holds a GitHub App's **private key** — the master
credential. It never leaves the orchestrator. Per issue, the broker mints a
throwaway token via a three-step dance (`:78-112`):

1. **Sign a JWT**: claims `{iss: app_id, iat: now−60s, exp: now+600s}`,
   RS256 with the private key (PyJWT, lazily imported). The 60-second
   backdate tolerates clock drift against GitHub's clock.
2. `GET /repos/{owner}/{repo}/installation` with `Authorization: Bearer
   <jwt>` → the installation id (which install of the App covers this repo).
3. `POST /app/installations/{id}/access_tokens` with body
   `{"repositories": ["repo"], "permissions": {...}}` → GitHub answers with a
   `ghs_...` installation token scoped to **one repo**, **narrowed
   permissions**, **~1 hour** of life.

The HTTP here is stdlib `urllib`, deliberately not PyGithub — the App-auth
half stays dependency-free, and both `sign_jwt` and `transport` are
injectable so unit tests exercise the exchange against fakes.

Around this sits `CredentialBroker` (`credentials.py:107`):
- `mint()` enforces a **permission ceiling** — any request broader than
  `{contents: write, issues: write, pull_requests: write}` raises before the
  exchange even runs.
- `container_env()` builds the container's entire credential surface (the
  token + the LLM key) and **fails closed** if anything private-key-shaped
  would ride along (`CredentialLeakError`).
- Token expiry has **no refresh path** on purpose: an expired container is
  torn down (branch kept) and re-dispatched with a fresh token — a container
  can never re-authenticate because it never had the key.

Also in `credentials.py`: `ScrubbingGitHubClient` (`:213`), a decorator
applied exactly once at the composition root, which regex-scrubs every piece
of text the app writes to GitHub (comments, file contents, even label and
branch names) for token shapes (`ghp_…`, `sk-ant-…`, PEM blocks) →
`[REDACTED]`.

---

## 8. How LLM calls work (`llm/`)

`build_llm_client(slot)` (`llm/__init__.py:57`) picks the adapter from
config: provider `claude`/`anthropic` → `ClaudeLLMClient`; anything else →
`OpenAICompatibleLLMClient` (hosted OpenAI, gateways, or local runtimes like
Ollama via `base_url`). First it runs a fail-fast check: a local `base_url`
configured on a GitHub-hosted runner can never be reached (localhost is the
runner itself), so it raises `FailFastProviderError` — which the backoff
layer is forbidden to retry.

`ClaudeLLMClient.complete` (`claude_adapter.py:45`): system messages are
pulled out into Anthropic's separate `system=` argument; the rest go to
`client.messages.create(...)`. The error translation is the seam contract:

- connection errors / 429 / 5xx → `ProviderError` (transient — the
  `BackoffLLMClient` wrapper retries the *same* model after 2s / 8s / 30s,
  `escalation.py:72`);
- 400 / 401 → propagate untouched (retrying can't fix a bad request or a
  wrong key).

Only when backoff exhausts does the failure count as **one** real attempt on
the escalation ladder (`escalation.py:147`): primary model ×N → escalation
model ×M → `agent-blocked` with a full report. A flaky network can never
burn real attempts.

Everything returns a plain `Completion(text, model, finish_reason)`. Since
real models love wrapping JSON in markdown fences despite instructions,
every parser first runs `strip_code_fences` (`interfaces.py:75`).

---

## 9. Containers — where untrusted code runs

Two interchangeable `ContainerRuntime` implementations, selected by
`.aorc.yml`'s `container:` block:

**Docker (default)** — `DockerContainerRuntime` (`harness.py:147`):
one container per issue, named `aorc-issue-N`, worktree mounted at
`/workspace`, pinned alive with `tail -f /dev/null`. Secrets arrive via a
0600 temp env-file deleted the moment `docker run` returns — never argv,
which any user could read via `ps`. Every toolchain command later runs as
`docker exec -w /workspace aorc-issue-N sh -c "<cmd>"`
(`ContainerTestRunner`, `tester.py:357`) — the container name is derived
from the worktree path (`harness.py:86`), so no registry has to map issues
to containers. This is the real isolation boundary: LLM-generated
setup/test/lint commands never run on the host.

**GitHub Actions** — `ActionsContainerRuntime`
(`github/actions_runtime.py`): `start` fires a `workflow_dispatch` instead
of a local container. The secret path is the clever part: dispatch *inputs*
are visible in run logs, so per-issue credentials never ride there. Instead
each value is sealed-box encrypted (libsodium) against the repo's Actions
public key and written as a repository secret `AORC_ISSUE_<n>_<KEY>`
(`:104`); the workflow reads it back via `secrets.*`. `teardown` cancels the
run and deletes those secrets — a per-issue credential never outlives its
issue.

---

## 10. What wakes the app up

The process is not long-running (except `serve`). Three triggers:

**Webhooks** (`python -m aorc serve`, `webhook.py`): a tiny stdlib HTTP
server. Every POST is HMAC-SHA256-verified against `X-Hub-Signature-256`
in constant time (`verify_signature`, `webhook.py:45`) **before the body is
even parsed** — a bad signature gets a 401 and nothing runs. Verified
deliveries are ACKed 200 immediately, then routed through the
`route_webhook` table (`install.py:471`):

- **PR merged** → `MergeTimeHandler.on_pr_merged` (`merge.py:275`):
  dedup via comment marker → close the issue, Done column, delete the branch
  → *stale-PR re-check*: every other open AORC PR touching the same files is
  rebased, re-tested, re-reviewed (broke? → fix loop; conflict? →
  `agent-blocked`; never silently stale) → reindex Graphify → run the wake
  sweep.
- **PR comment** → `on_pr_comment` (`merge.py:294`): one LLM call classifies
  the human's comment as `code` (implementation wrong → coder fix loop with
  the comment as feedback) or `spec` (spec/tests wrong → relabel
  `in-design`, re-run the pipeline from the top). The bot's own comments are
  filtered by author + marker prefix so it never argues with itself.
- **`repository_dispatch: aorc-main-broken`** → `on_main_broken`
  (`merge.py:320`) — **auto-rollback**. The generated workflow
  (`install.py:179`) runs the repo's tests on every push to main; red main →
  it reverts the merge and reports the PR number back. The handler then
  triages every in-flight issue against the reverted files
  (`rollback_verdict`, `merge.py:142`): overlap, blast-radius overlap, or
  "no design doc yet, can't prove safe" → torn down + re-dispatched fresh
  against the corrected main.
- **new/edited issue** → `loop.backfill()` (idempotent re-sync).

**Cron** (`python -m aorc wake`): the backstop, because webhooks drop.
`wake()` (`wake.py:286`) = token-expiry pass over in-flight containers →
rebuild state from GitHub → rebuild the collision registry from committed
design docs (`wake.py:336`) → sweep the held queue and re-dispatch whatever
is now unblocked.

**Backfill** (`python -m aorc backfill`, `wake.py:298`): list every open
issue not already in the flow, run triage (`triage.py:45` — one LLM call:
"is the definition of done bounded and testable?"), route the answers:
actionable → dispatch selector (`dispatch.py:61` — declared `blocked by #N`
blockers + concurrency ceiling, default 5); vague → the clarification
interview (`clarification.py` — one question at a time as comments, answers
gated by repo write access, 7-day nudge then block); epic → decomposition
(`decomposition.py` — PRD + sub-issues, deduped by hidden markers so re-runs
never duplicate).

**Duplicate-event safety**: comment-marker dedup (`claim_event`,
`wake.py:108`) plus the artifact check. If two containers race past both,
the loser finds the survivor's committed artifact and no-ops — *wasteful,
never wrong*.

---

## 11. The safety rails

- **Cost circuit breakers** (`guards.py:61`): per-issue / per-run / daily
  dollar caps, checked most-severe-first; a 1.5× per-issue overshoot stops
  mid-stage instead of finishing.
- **Wall clock** (`guards.py:131`, enforced at `harness.py:645`): default
  45 min per container, then kill — branch kept, resumable.
- **Escalation ladder** (`escalation.py:147`): primary ×3 → escalation ×1 →
  `agent-blocked` + a comment carrying everything that was attempted.
- **Rate limits** (`escalation.py:263`): GitHub 429/403-with-Retry-After is
  backed off and retried, never counted as a failure. A plain 403 is a
  permissions error and propagates.
- **Secret scrubbing** (`credentials.py:205`): deterministic regex pass over
  every outbound text surface.
- **Fail closed, everywhere**: bad config, wrong repo checkout, un-mintable
  permissions, key-shaped env values, failed Graphify queries — all stop the
  machine rather than guessing.

---

## 12. The dashboard (`ui/`)

A local FastAPI app (`ui/bridge/app.py`), strictly read-only against GitHub
through AORC's own `SdkGitHubClient` + the label rules in `aorc.pipeline` —
no pipeline logic is re-implemented. `GET /api/issues` answers from an
in-memory snapshot a background thread refreshes; the frontend
(`ui/frontend/`) polls it every 2.5 s. Three POST endpoints (Run / Release /
Backfill) simply spawn the real CLI (`python -m aorc ... run-issue N`) and
stream its stdout into a terminal panel — same tested code path as typing
the command yourself.

Run it: `.venv/bin/uvicorn ui.bridge.app:app --port 8000` → open
http://localhost:8000.

---

## 13. Map of the source tree

```
src/aorc/
├─ interfaces.py      the two seams: GitHubClient + LLMClient + data types
├─ config.py          .aorc.yml parsing (fail-closed)
├─ pipeline.py        label state machine + artifact checker
├─ triage.py          actionable / not-ready classifier
├─ clarification.py   "grill-me" Q&A for vague issues
├─ decomposition.py   epic → sub-issues
├─ dispatch.py        blockers + concurrency gate
├─ design.py          design stage (strict JSON schema + path snapping)
├─ tester.py          tester + critic + coverage gate + red/error classifier
├─ coder.py           bounded fix loop (blind to tests, preservation guard)
├─ reviewer.py        smoke/coverage gates + reviewer LLM + PR open
├─ driver.py          sequences the four stages for one issue
├─ harness.py         worktrees, Docker runtime, collision checkpoint
├─ credentials.py     broker, token model, secret scrubbing
├─ guards.py          cost + wall-clock circuit breakers
├─ escalation.py      backoff, escalation ladder, rate-limit wrapper
├─ wake.py            stateless wake loop (sweep, dedup, backfill)
├─ merge.py           merge-time: auto-close, stale PRs, feedback, rollback
├─ install.py         App install, config gate, webhook routing table
├─ webhook.py         HMAC-verified HTTP receiver
├─ gitops.py          real git rebase/revert (LocalGitOps)
├─ graphify.py        knowledge-graph seam (blast radius)
├─ __main__.py        composition root + CLI (START HERE)
├─ github/
│  ├─ sdk_adapter.py     PyGithub adapter (REST + Projects v2 GraphQL)
│  ├─ app_token.py       App-JWT → installation-token exchange
│  ├─ actions_runtime.py workflow_dispatch runtime + sealed secrets
│  └─ mock.py            in-memory GitHub for the unit suite
└─ llm/
   ├─ __init__.py        build_llm_client factory + local-LLM fail-fast
   ├─ claude_adapter.py  Anthropic SDK adapter
   ├─ openai_adapter.py  OpenAI-compatible adapter
   └─ mock.py            scripted LLM for the unit suite

tests/            unit suite — zero third-party deps, all mocks
tests/integration/ credential-gated tests against real services
ui/               read-only dashboard + CLI-spawning control buttons
.aorc-worktrees/  fossils of real sandbox runs (per-issue worktrees)
```

---

## 14. One picture

```
issue opened
   │ triage (LLM: bounded + testable?)
   ├─ vague ──► clarification Q&A ──┐
   ├─ epic ──► decompose into subs ─┤ (re-enter)
   ▼                                │
dispatch gate (blockers? capacity? .aorc.yml merged?)
   ▼
per-issue: 1h token + git worktree + Docker container
   ▼
DESIGN ──► collision checkpoint ──► TESTS (tester + critic + coverage,
   ▼                                        must fail "red")
CODE (blind fix loop, ≤3 tries)
   ▼
REVIEW (smoke → coverage → reviewer LLM → rebase onto main)
   ▼
PR opened ──► human merges ──► issue closed · branch deleted ·
                               stale PRs re-checked · held issues released ·
                               red main auto-reverted
any hard failure ──► `agent-blocked` + one comment telling the whole story
```
