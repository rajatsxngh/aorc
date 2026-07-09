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
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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

from aorc.clarification import QUESTION_MARKER  # noqa: E402
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
# A clarification comment is `QUESTION_MARKER\n<the question>` (clarification.py).
_QUESTION_RE = re.compile(r"-->\s*\n(.*)", re.DOTALL)

app = FastAPI(title="AORC dashboard bridge", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
)


def _stage_for(issue) -> str:
    if issue.state == "closed":
        return "done"
    if HELD_LABEL in issue.labels or AWAITING_CONFIG_LABEL in issue.labels:
        return "aorc-held"
    # A terminal label (waiting on a human) can coexist with the stage label
    # it interrupted -- e.g. `in-design` + `needs-clarification`. The terminal
    # state is what matters to the person looking at the dashboard.
    for label in ("needs-clarification", "agent-blocked"):
        if label in issue.labels:
            return label
    return current_pipeline_label(issue.labels) or "backlog"


def _held_reason(issue, stage: str) -> str | None:
    """Reason text for attention states, read from the orchestrator's own
    marker comments on the issue. One extra API call, so only made for issues
    that are actually held/blocked."""
    if AWAITING_CONFIG_LABEL in issue.labels:
        return "waiting for the .aorc.yml config PR to merge"
    if stage not in ("aorc-held", "agent-blocked", "needs-clarification"):
        return None
    marker, pattern = {
        "aorc-held": (HELD_PING_MARKER, _HELD_REASON_RE),
        "agent-blocked": (BLOCKED_PING_MARKER, _BLOCKED_REASON_RE),
        "needs-clarification": (QUESTION_MARKER, _QUESTION_RE),
    }[stage]
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


# --------------------------------------------------------------------------- #
# Slice 2: control buttons -- run AORC's *existing* CLI and stream its output.
# --------------------------------------------------------------------------- #
# The only thing these endpoints can ever execute is the same command the
# user runs by hand in a terminal:
#
#   python -m aorc --dev-pat-minter --config sandbox.aorc.yml \
#       --repo <repo> {run-issue N | backfill}
#
# The argv is assembled from this fixed template (no shell, no caller-supplied
# strings beyond an integer issue number that FastAPI has already validated),
# so there is no path to arbitrary command execution. No pipeline logic lives
# here -- the buttons are a remote control for the tested CLI.

AORC_CONFIG = os.environ.get("AORC_UI_CONFIG", "sandbox.aorc.yml")
PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python")


def _aorc_argv(*subcommand: str) -> list[str]:
    # `-u` = unbuffered stdout, so lines stream out as they happen instead of
    # arriving in one lump at the end. It changes nothing else about the run.
    return [
        PYTHON,
        "-u",
        "-m",
        "aorc",
        "--dev-pat-minter",
        "--config",
        AORC_CONFIG,
        "--repo",
        REPO,
        *subcommand,
    ]


class Job:
    """One running CLI invocation: the process, plus every output line seen so
    far. A background thread pumps the process's combined stdout/stderr into
    `lines`; any number of stream readers can follow along (and a reader
    disconnecting -- user closes the panel -- never kills the run)."""

    def __init__(self, key: str, title: str, argv: list[str]) -> None:
        self.key = key
        self.title = title
        self.lines: list[str] = []
        self.done = False
        self.exit_code: int | None = None
        self._cond = threading.Condition()
        env = dict(os.environ)
        env.setdefault("GITHUB_TOKEN", TOKEN)
        env["PYTHONUNBUFFERED"] = "1"
        self._proc = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            with self._cond:
                self.lines.append(line)
                self._cond.notify_all()
        code = self._proc.wait()
        with self._cond:
            self.exit_code = code
            self.lines.append(f"\n--- finished (exit code {code}) ---\n")
            self.done = True
            self._cond.notify_all()
        _jobs.finished(self.key)

    def follow(self):
        """Yield output lines from the beginning until the process exits."""
        i = 0
        while True:
            with self._cond:
                while i >= len(self.lines) and not self.done:
                    self._cond.wait(timeout=1.0)
                chunk, i = self.lines[i:], len(self.lines)
                finished = self.done and i >= len(self.lines)
            yield from chunk
            if finished:
                return


class JobRegistry:
    """Which runs are active right now. One run per issue at a time, and
    backfill (which itself dispatches issues) never overlaps with anything."""

    def __init__(self) -> None:
        self._active: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, key: str, title: str, argv: list[str]) -> Job:
        with self._lock:
            if key in self._active:
                raise HTTPException(status_code=409, detail=f"{title} is already running")
            if "backfill" in self._active or (key == "backfill" and self._active):
                raise HTTPException(
                    status_code=409,
                    detail="another run is in progress; backfill and issue runs don't overlap",
                )
            job = Job(key, title, argv)
            self._active[key] = job
            return job

    def finished(self, key: str) -> None:
        with self._lock:
            self._active.pop(key, None)


_jobs = JobRegistry()


def _stream(job: Job) -> StreamingResponse:
    return StreamingResponse(
        job.follow(), media_type="text/plain; charset=utf-8", headers={"X-Accel-Buffering": "no"}
    )


@app.post("/api/run-issue/{number}")
def api_run_issue(number: int):
    return _stream(
        _jobs.start(f"issue-{number}", f"run-issue {number}", _aorc_argv("run-issue", str(number)))
    )


@app.post("/api/release/{number}")
def api_release(number: int):
    # Releasing a held issue = re-dispatching it, which is exactly the
    # existing `run-issue` command; only the button label differs.
    return _stream(
        _jobs.start(f"issue-{number}", f"release {number}", _aorc_argv("run-issue", str(number)))
    )


@app.post("/api/backfill")
def api_backfill():
    return _stream(_jobs.start("backfill", "backfill", _aorc_argv("backfill")))


# Serve the dashboard itself from the same port: http://localhost:8000/
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
