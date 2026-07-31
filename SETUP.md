# AORC — setup guide for a fresh clone

Everything needed to get the orchestrator and its dashboard running against
your own GitHub repository, plus answers to the three questions about
branches and how the UI is wired.

---

## 1. Which branch

| Branch | Head | What's on it |
|---|---|---|
| `main` | latest | The orchestrator: `src/aorc/`, the 580-test suite, `Dockerfile`, README and the three companion docs |
| `ui-build` | 2026-07-21 | Everything on `main` as of that date, **plus `ui/`** — the dashboard |

Only these two branches exist on the remote. Any `aorc/issue-*` branches you
see are per-issue work branches the pipeline creates; they are not part of
setup.

**The dashboard exists only on `ui-build`.** Check that branch out if you want
the UI. `main` is ahead on documentation but has no `ui/` directory.

---

## 2. How the UI is linked to a repository

It is **not hardcoded to any particular sandbox.** The link is made entirely
through environment variables, in `ui/bridge/app.py`:

**Reads** (`app.py:72-75`) — the repo is resolved in this order:

1. `AORC_REPO`
2. `AORC_IT_GITHUB_REPO`
3. the literal fallback `"rajatsxngh/aorc-sandbox"`

That value is used for direct GitHub REST calls
(`api.github.com/repos/{REPO}/issues`) to build the dashboard's status view.
The bridge is read-only on this path — it lists issues and PRs and reads
comments, nothing else.

**Writes / actions** (`app.py:234-249`) — the buttons don't reimplement any
pipeline logic. They shell out to the real CLI:

```
python -m aorc --dev-pat-minter --config $AORC_UI_CONFIG --repo $REPO {run-issue N | backfill}
```

The frontend (`ui/frontend/index.html`) is repo-agnostic — it just renders
whatever `repo` value the bridge reports in its `/api/issues` response.

Environment comes from a `.env` file at the repo root, which the bridge loads
itself on import (`app.py:59`). No `export` needed.

---

## 3. Pointing it at a different repository

No code change required. Set two variables in `.env`:

```
AORC_REPO=owner/your-repo
AORC_UI_CONFIG=your-repo.aorc.yml
```

`AORC_UI_CONFIG` defaults to `sandbox.aorc.yml` (`app.py:230`). Each target
repository needs its own config file, because it carries that repo's `setup`
and `test` commands. `GITHUB_TOKEN` must be scoped to the new repo as well,
or the bridge exits at startup.

**One repo per bridge process.** `REPO` is a module-level constant read once
at import, so a single running bridge serves exactly one repository. Serving
several at once would mean making `REPO` a per-request parameter at
`app.py:72` and threading it through `/api/issues` and `_aorc_argv` — a real
code change, not configuration.

---

## 4. Setup, step by step

### Prerequisites

- Docker, running
- [uv](https://github.com/astral-sh/uv)
- A GitHub token with `repo` scope on your target repository
- An Anthropic API key

### Step 1 — Clone and choose the branch

```sh
git clone https://github.com/rajatsxngh/aorc.git
cd aorc
git checkout ui-build      # the UI lives only on this branch
```

### Step 2 — Virtualenv

It must be `.venv` at the repo root — the bridge hardcodes
`REPO_ROOT/.venv/bin/python` (`app.py:231`) for the CLI it launches.

```sh
uv venv --python 3.12
uv pip install -e ".[dev,claude,github,apptoken]"
.venv/bin/pip install -r ui/bridge/requirements.txt
```

The `dev` extra alone is enough to run the test suite. The others are the SDKs
a live run needs: `claude` for the model, `github` for the GitHub API,
`apptoken` for the App-token exchange.

Confirm the core works before going further:

```sh
.venv/bin/pytest -q
# expect: 580 passed, 13 deselected
```

### Step 3 — Build the base image

Containers for each build attempt start from this image. The `Dockerfile` is
in the repo.

```sh
docker build -t aorc-base .
```

The name is arbitrary — it just has to match `AORC_BASE_IMAGE` in the next
step.

### Step 4 — Create `.env` at the repo root

**This file is gitignored, so it is not in the clone. You must create it.**

```
GITHUB_TOKEN=<your github token>
ANTHROPIC_API_KEY=<your anthropic key>
AORC_REPO=owner/your-target-repo
AORC_BASE_IMAGE=aorc-base
AORC_UI_CONFIG=sandbox.aorc.yml
```

If `AORC_REPO` is missing the bridge does **not** error — it silently falls
back to `rajatsxngh/aorc-sandbox` and shows the wrong repository. If
`GITHUB_TOKEN` is missing it exits with a clear message.

### Step 5 — Create the orchestrator-side config

**Also gitignored — you must create this too.** The filename has to match
`AORC_UI_CONFIG` above (default `sandbox.aorc.yml`), at the repo root:

```yaml
llm:
  primary:
    provider: claude
    model: claude-sonnet-4-6
    api_key: $ANTHROPIC_API_KEY
  escalation:                    # optional
    provider: claude
    model: claude-sonnet-4-6
    api_key: $ANTHROPIC_API_KEY

setup: pip install -e .          # required — how to install your target repo
test: pytest                     # required — how to test it
lint: ruff check .               # optional

failure:
  primary_attempts: 3
  escalation_attempts: 1
merge:
  auto: false
```

`setup` and `test` are mandatory: AORC fails closed with a clear message
rather than guessing a build command. `$ANTHROPIC_API_KEY` is expanded from
the environment at load time, so no key is ever written into this file.

Other available keys: `coverage.command` / `coverage.floor` (default 80),
`dispatch.concurrency` (default 5), `cost.*` caps,
`compute.wall_clock_minutes` (default 45), `container.runtime`
(`docker` | `actions`). Full schema in `src/aorc/config.py`.

### Step 6 — Install AORC into your target repository

```sh
.venv/bin/python -m aorc --config sandbox.aorc.yml --repo owner/your-target-repo install
```

This creates the project board and the pipeline labels, then **opens a pull
request** adding `.aorc.yml` to your target repo.

**You must merge that PR before anything will build.** Until `.aorc.yml` is
present on the target's `main`, the config gate (`install.py:239-246`) parks
every issue under `awaiting-config` and no container ever starts.

So there are two config files, and both are needed:

- the local one you wrote in step 5, passed via `--config` — read by the
  orchestrator process on your machine
- the one committed to the **target** repo by the install PR — read by the
  gate, so the repo itself declares its own toolchain

### Step 7 — Start the dashboard

```sh
.venv/bin/uvicorn ui.bridge.app:app --port 8000
```

Open <http://localhost:8000>. Ctrl-C stops it.

### Step 8 — Or drive it from the CLI directly

```sh
# One issue, end to end
.venv/bin/python -m aorc --config sandbox.aorc.yml --repo owner/repo run-issue 42

# Every open issue, applying triage, collision and concurrency rules
.venv/bin/python -m aorc --config sandbox.aorc.yml --repo owner/repo backfill

# One cron tick: sweep held issues, re-queue expired tokens
.venv/bin/python -m aorc --config sandbox.aorc.yml --repo owner/repo wake
```

---

## 5. Things to know before you rely on it

**The UI always uses the dev credential path.** Its buttons pass
`--dev-pat-minter` unconditionally (`app.py:238`), which hands the same fixed
personal access token to every container instead of minting a short-lived,
single-repo token per issue. That is fine for a sandbox and wrong for
anything real. A production setup needs a registered GitHub App and
`AORC_GITHUB_APP_ID` + `AORC_GITHUB_APP_PRIVATE_KEY_PATH` instead — see the
"One-time GitHub App registration" section in `CLAUDE.md`.

**A human merges every PR.** AORC never merges: `auto_merge_allowed` exists
in the config schema but has no caller anywhere in the codebase.

**Where the stages actually run.** The pipeline stages — design, test, code,
review — run in the orchestrator process on your machine. What runs inside
the per-issue container is the target repo's `setup` / `test` / `lint` /
coverage / smoke commands, via `docker exec` against the mounted worktree.
That is the untrusted, generated code, and it is the part that needs sealing.

**Status is honest, and worth reading.** The README's "Known gaps" table
lists what is implemented but not yet wired into the live path — cost
circuit-breakers, the wall-clock limit, the clarification resume loop, the
escalation ladder, Graphify blast-radius collision detection. Treat AORC as a
serious prototype, not a production system.

---

## 6. Further reading, in the repo

- `README.md` — architecture, design decisions, known gaps, flow diagrams
- `AORC-HANDOVER.md` — the fullest account: setup, decisions, and an
  authoritative wired-vs-not status
- `HOW-IT-WORKS.md` — a guided tour of the code with file references
- `AORC-DEEP-DIVE.md` — module-by-module reference plus a full dry-run trace
- `ui/README.md` — dashboard internals (on `ui-build`)
