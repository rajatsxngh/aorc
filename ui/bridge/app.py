"""Read-only bridge: AORC's live pipeline state as JSON for the local dashboard.

Slice 1 of the UI. This app *imports* AORC's existing GitHub adapter and label
state machine and never modifies them (and never writes to GitHub -- every
call used here is a list/get/read).

Repo + auth come from the same environment AORC's composition root reads:
`GITHUB_TOKEN` / `AORC_REPO`, with a fallback to the `AORC_IT_GITHUB_TOKEN` /
`AORC_IT_GITHUB_REPO` pair already present in the repo-root `.env` (the
sandbox credentials), which this module loads on import so one command starts
the whole thing.

Run from the repo root:

    .venv/bin/uvicorn ui.bridge.app:app --port 8000

then open http://localhost:8000 (the dashboard frontend is served from the
same port, so no CORS gymnastics are needed).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "ui" / "frontend"

# Make `import aorc` work even when the venv's editable install isn't on the
# path of whatever interpreter launched uvicorn.
sys.path.insert(0, str(REPO_ROOT / "src"))


def _load_dotenv(path: Path) -> None:
    """Minimal `.env` loader (no new dependency): KEY=VALUE lines, optional
    surrounding quotes, never overrides variables already in the environment."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(REPO_ROOT / ".env")

from aorc.driver import BLOCKED_PING_MARKER, HELD_PING_MARKER  # noqa: E402
from aorc.github.sdk_adapter import SdkGitHubClient  # noqa: E402
from aorc.pipeline import (  # noqa: E402
    AWAITING_CONFIG_LABEL,
    HELD_LABEL,
    branch_name,
    current_pipeline_label,
)

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("AORC_IT_GITHUB_TOKEN")
REPO = (
    os.environ.get("AORC_REPO")
    or os.environ.get("AORC_IT_GITHUB_REPO")
    or "rajatsxngh/aorc-sandbox"
)

if not TOKEN:
    raise SystemExit(
        "No GitHub token found. Set GITHUB_TOKEN (or AORC_IT_GITHUB_TOKEN in "
        "the repo-root .env) before starting the bridge."
    )

github = SdkGitHubClient(TOKEN, REPO)

# The orchestrator's own hold/block comments carry these markers (driver.py);
# the reason text follows them in a fixed phrasing.
_HELD_REASON_RE = re.compile(
    r"Holding at the post-design checkpoint: (.*?)(?:\. The merge-time|$)",
    re.DOTALL,
)
_BLOCKED_REASON_RE = re.compile(r"Reason:\n(.*)", re.DOTALL)

app = FastAPI(title="AORC dashboard bridge", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
)


def _stage_for(issue) -> str:
    if issue.state == "closed":
        return "done"
    if HELD_LABEL in issue.labels or AWAITING_CONFIG_LABEL in issue.labels:
        return "aorc-held"
    return current_pipeline_label(issue.labels) or "backlog"


def _held_reason(issue, stage: str) -> str | None:
    """Reason text for attention states, read from the orchestrator's own
    marker comments on the issue. One extra API call, so only made for issues
    that are actually held/blocked."""
    if AWAITING_CONFIG_LABEL in issue.labels:
        return "waiting for the .aorc.yml config PR to merge"
    if stage not in ("aorc-held", "agent-blocked"):
        return None
    marker, pattern = (
        (HELD_PING_MARKER, _HELD_REASON_RE)
        if stage == "aorc-held"
        else (BLOCKED_PING_MARKER, _BLOCKED_REASON_RE)
    )
    for comment in reversed(github.list_comments(issue.number)):
        if marker in comment.body:
            match = pattern.search(comment.body)
            if match:
                return " ".join(match.group(1).split())[:300]
            return None
    return None


@app.get("/api/issues")
def api_issues():
    try:
        issues = github.list_issues(state="open") + github.list_issues(state="closed")
        prs = github.list_pull_requests(state="open") + github.list_pull_requests(
            state="closed"
        )
    except Exception as exc:  # surface GitHub/auth failures as a clear 502
        raise HTTPException(status_code=502, detail=f"GitHub read failed: {exc}")

    # Map each issue to its AORC branch's PR (open one wins over closed).
    pr_by_head = {}
    for pr in reversed(prs):  # earlier in list = open = takes precedence
        pr_by_head[pr.head] = pr

    out = []
    for issue in issues:
        stage = _stage_for(issue)
        pr = pr_by_head.get(branch_name(issue.number))
        held_reason = None
        try:
            held_reason = _held_reason(issue, stage)
        except Exception:
            pass  # a failed comment read never hides the issue itself
        out.append(
            {
                "number": issue.number,
                "title": issue.title,
                "stage": stage,
                "held_reason": held_reason,
                "pr_number": pr.number if pr else None,
                "pr_url": f"https://github.com/{REPO}/pull/{pr.number}" if pr else None,
            }
        )
    return {"repo": REPO, "issues": out}


# Serve the dashboard itself from the same port: http://localhost:8000/
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
