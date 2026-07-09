# AORC dashboard UI (Slice 1 — read-only status)

Two pieces, both new, neither touches `src/aorc/`:

- `bridge/` — a small FastAPI app that reads live pipeline state from GitHub
  **through AORC's own adapter** (`aorc.github.sdk_adapter.SdkGitHubClient`
  plus the label rules in `aorc.pipeline`) and serves it as JSON at
  `GET /api/issues`. Strictly read-only: it only lists issues/PRs and reads
  comments; it can't trigger, label, or write anything.
- `frontend/` — the dashboard design (`index.html` + `support.js`), served by
  the bridge itself and fetching `/api/issues` on load.

## Run it

From the repo root:

```sh
.venv/bin/pip install -r ui/bridge/requirements.txt   # once
.venv/bin/uvicorn ui.bridge.app:app --port 8000
```

Then open **http://localhost:8000** in your browser. Ctrl-C stops it.

Repo and token come from the same environment AORC uses: `GITHUB_TOKEN` /
`AORC_REPO`, falling back to the `AORC_IT_GITHUB_TOKEN` / `AORC_IT_GITHUB_REPO`
pair in the repo-root `.env` (the sandbox: `rajatsxngh/aorc-sandbox`). The
bridge loads `.env` itself, so no exports are needed.

## What the dashboard shows

Each issue's stage is derived exactly the way AORC derives it:

- the pipeline label (`in-design` / `in-test` / `in-code` / `in-review`,
  `needs-clarification` / `agent-blocked`) via `aorc.pipeline.current_pipeline_label`
- `aorc-held` (and `awaiting-config`) → the **AORC held** badge, with the hold
  reason pulled from the orchestrator's own `<!-- aorc:checkpoint-hold -->`
  comment
- closed issues → **Done / merged** (collapsed by default)
- an open issue with no pipeline label → **Backlog**
- a PR link appears when a PR exists whose head is the issue's
  `aorc/issue-<n>` branch
