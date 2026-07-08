"""S8 -- Reviewer + coverage + smoke + PR open.

The final pipeline stage: completes the spine (one actionable issue -> a
review-approved PR).

- The reviewer agent evaluates a real diff (old content on `main` vs. new
  content on the issue branch, both read through the existing
  `GitHubClient.get_file` seam -- no new interface method needed) against
  the design doc's `interface`/`task_list` and the original issue body. It
  is constructed from a *distinct* `LLMClient` instance from the coder's
  (the escalation-tier slot in real wiring), mirroring S6's
  tester/critic-distinctness contract.
- Gate order matches the PRD's container flow (Coder -> smoke -> coverage
  -> Reviewer -> PR): smoke examples and the line-coverage floor run
  *before* the reviewer agent is even asked, since those are mechanical
  checks and cheaper than an LLM call. Both are configured, never guessed:
  the smoke gate needs both `smoke_examples` (from `.aorc.yml`'s `smoke:`
  block) and a `smoke_command` template (`{input}`/`{expect}` are
  substituted) -- without a command there is no generic way to invoke an
  arbitrary app, so the gate is skipped rather than inventing one; missing
  `smoke:` entirely also skips it, per the acceptance criteria (permanent
  auto-merge disqualification is S18's job, not this stage's). The
  coverage gate is skipped the same way when no `coverage_command` is
  configured -- the command itself is responsible for scoping to changed
  lines (e.g. a `diff-cover` invocation in `.aorc.yml`); this stage only
  parses the last `NN%` in its output and compares to the floor.
- Any gate failure (smoke, coverage, or the reviewer agent's "reject") is
  treated identically: it re-enters the *same* bounded fix loop S7 already
  built, by calling the injected `CoderStage` with the failure text as
  `review_feedback` -- this is why the coder's fix loop was extended with
  that parameter instead of building a second, parallel retry mechanism.
  Only after every gate passes in the same attempt is the PR opened; the
  full history of that issue's gate attempts (rejections and the final
  approval) is posted as PR comments in one batch, so a human reviewing
  the merge sees the whole trail even though the PR didn't exist yet
  during earlier rejections ("blocks the PR ... before the PR is
  surfaced").
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coder import CoderStage
from .design import DesignDoc
from .guards import BLOCKED_LABEL
from .interfaces import GitHubClient, LLMClient, Message, PullRequest, strip_code_fences
from .pipeline import LABEL_COLUMN, branch_name
from .tester import TestRunner

if TYPE_CHECKING:  # annotation only -- merge.py imports this module at runtime
    from .merge import GitOps

DEFAULT_MAX_RETRIES = 3
DEFAULT_COVERAGE_FLOOR = 80.0
BASE_REF = "main"

MERGE_CONFLICT_MARKER = "<!-- aorc:merge-conflict -->"

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")

_REVIEWER_SYSTEM_PROMPT = (
    "You are the reviewer agent for a software issue. You did not write the "
    "code. Evaluate the branch's diff against the design doc's `interface` "
    "and `task_list`, and against the original issue -- flag any deviation "
    "or anything the diff fails to address. Reply with a single JSON "
    'object: {"verdict": "approve" | "reject", "reason": "..."}. Reply with '
    "the JSON object only, no surrounding prose."
)


@dataclass
class ReviewVerdict:
    verdict: str  # "approve" | "reject"
    reason: str = ""


@dataclass
class ReviewerResult:
    status: str  # "proceed" | "agent-blocked"
    pr: PullRequest | None = None
    attempts: int = 0


def parse_reviewer_response(text: str) -> ReviewVerdict | None:
    """Pure schema check: `None` unless `verdict` is exactly "approve" or
    "reject" -- same shape as S6's `parse_critic_response`."""
    try:
        data = json.loads(strip_code_fences(text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in ("approve", "reject"):
        return None
    return ReviewVerdict(verdict=verdict, reason=str(data.get("reason", "")))


def parse_coverage_percent(output: str) -> float | None:
    """Pure parse, no LLM judgment: the last `NN%`/`NN.N%` occurrence in a
    coverage tool's output -- matches both `coverage report`'s TOTAL line
    and a diff-coverage tool's summary line."""
    matches = _PERCENT_RE.findall(output)
    return float(matches[-1]) if matches else None


def coverage_gate(percent: float | None, floor: float) -> bool:
    return percent is not None and percent >= floor


class ReviewerStage:
    """Smoke -> coverage -> reviewer-agent gates, in that order; any failure
    re-enters the coder's bounded fix loop with the failure as feedback.
    Only once every gate passes in the same attempt is the PR opened."""

    def __init__(
        self,
        reviewer_llm: LLMClient,
        coder: CoderStage,
        github: GitHubClient,
        test_runner: TestRunner,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        coverage_command: str | None = None,
        coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
        smoke_command: str | None = None,
        smoke_examples: list[dict] | None = None,
        gitops: GitOps | None = None,
    ) -> None:
        self._reviewer_llm = reviewer_llm
        self._coder = coder
        self._github = github
        self._test_runner = test_runner
        self._max_retries = max_retries
        self._coverage_command = coverage_command
        self._coverage_floor = coverage_floor
        self._smoke_command = smoke_command
        self._smoke_examples = list(smoke_examples or [])
        self._gitops = gitops

    def run(
        self,
        issue_number: int,
        design: DesignDoc,
        issue_body: str,
        *,
        cwd: str = ".",
        pr: PullRequest | None = None,
    ) -> ReviewerResult:
        """With `pr` given (S17 stale-PR re-review), the gates and reviewer
        run against the existing PR -- no new PR is opened, the attempt trail
        is posted on it, and the caller owns any rebase (the PR-open rebase
        check is skipped)."""
        history: list[str] = []
        attempts = 0
        while attempts < self._max_retries:
            attempts += 1

            gate_failure = self._run_mechanical_gates(cwd)
            if gate_failure is not None:
                history.append(f"attempt {attempts}: {gate_failure}")
                if not self._fix(issue_number, design, gate_failure, cwd):
                    return ReviewerResult(status="agent-blocked", attempts=attempts)
                continue

            completion = self._reviewer_llm.complete(
                self._messages(issue_body, design, self._diff(issue_number, design))
            )
            verdict = parse_reviewer_response(completion.text)
            if verdict is None:
                history.append(f"attempt {attempts}: reviewer format miss")
                continue  # retry the reviewer call itself, no code changed

            history.append(f"attempt {attempts}: {verdict.verdict} -- {verdict.reason}")
            if verdict.verdict == "reject":
                if not self._fix(issue_number, design, verdict.reason, cwd):
                    return ReviewerResult(status="agent-blocked", attempts=attempts)
                continue

            # Merge conflict at PR-open (S17, PRD B17): main may have moved
            # while the pipeline ran. Clean rebase -> re-run every gate and
            # the reviewer against the rebased result (bounded by the same
            # attempt cap); true conflict -> agent-blocked, the coder never
            # freelances a resolution. Mechanical discriminator: git says.
            if pr is None and self._gitops is not None:
                rebase = self._gitops.rebase(branch_name(issue_number), BASE_REF)
                if rebase.status == "conflict":
                    self._github.add_label(issue_number, BLOCKED_LABEL)
                    self._github.set_board_column(issue_number, LABEL_COLUMN[BLOCKED_LABEL])
                    self._github.post_comment(
                        issue_number,
                        f"{MERGE_CONFLICT_MARKER}\nStopping: true merge conflict "
                        f"with {BASE_REF} at PR-open. Labeling `{BLOCKED_LABEL}` "
                        "(reason: merge conflict with main).",
                    )
                    return ReviewerResult(status="agent-blocked", attempts=attempts)
                if rebase.status == "clean":
                    history.append(
                        f"attempt {attempts}: clean rebase onto {BASE_REF} -- "
                        "re-running gates + reviewer against the rebased result"
                    )
                    continue

            opened = pr if pr is not None else self._open_pr(issue_number, design)
            for entry in history:
                self._github.post_comment(opened.number, entry)
            return ReviewerResult(status="proceed", pr=opened, attempts=attempts)

        return ReviewerResult(status="agent-blocked", attempts=attempts)

    def _fix(self, issue_number: int, design: DesignDoc, feedback: str, cwd: str) -> bool:
        result = self._coder.run(
            issue_number,
            design,
            repo_files=self._current_files(issue_number, design),
            cwd=cwd,
            review_feedback=feedback,
        )
        return result.status == "proceed"

    def _run_mechanical_gates(self, cwd: str) -> str | None:
        if self._smoke_examples and self._smoke_command:
            for example in self._smoke_examples:
                command = self._smoke_command.format(
                    input=example.get("input", ""), expect=example.get("expect", "")
                )
                result = self._test_runner.run(cwd, command)
                if result.returncode != 0:
                    return (
                        f"smoke example failed ({example.get('input')}): "
                        f"{result.stdout}\n{result.stderr}".strip()
                    )

        if self._coverage_command:
            result = self._test_runner.run(cwd, self._coverage_command)
            if result.returncode != 0:
                return f"coverage command failed: {result.stdout}\n{result.stderr}".strip()
            percent = parse_coverage_percent(result.stdout + result.stderr)
            if not coverage_gate(percent, self._coverage_floor):
                return f"coverage floor not met: {percent}% < {self._coverage_floor}%"

        return None

    def _current_files(self, issue_number: int, design: DesignDoc) -> dict[str, str]:
        branch = branch_name(issue_number)
        return {path: self._github.get_file(path, branch) or "" for path in design.files}

    def _diff(self, issue_number: int, design: DesignDoc) -> dict[str, str]:
        branch = branch_name(issue_number)
        diffs = {}
        for path in design.files:
            old = self._github.get_file(path, BASE_REF) or ""
            new = self._github.get_file(path, branch) or ""
            diffs[path] = "\n".join(
                difflib.unified_diff(
                    old.splitlines(),
                    new.splitlines(),
                    fromfile=f"{BASE_REF}:{path}",
                    tofile=f"{branch}:{path}",
                    lineterm="",
                )
            )
        return diffs

    def _messages(self, issue_body: str, design: DesignDoc, diff: dict[str, str]) -> list[Message]:
        parts = [
            f"Original issue:\n{issue_body}",
            f"interface: {json.dumps(design.interface)}",
            f"task_list: {json.dumps(design.task_list)}",
        ]
        for path, text in diff.items():
            parts.append(f"Diff for {path}:\n{text}")
        return [Message("system", _REVIEWER_SYSTEM_PROMPT), Message("user", "\n\n".join(parts))]

    def _open_pr(self, issue_number: int, design: DesignDoc) -> PullRequest:
        body = "Implements:\n" + "\n".join(f"- {task}" for task in design.task_list)
        return self._github.open_pull_request(
            title=f"AORC: issue #{issue_number}",
            body=body,
            head=branch_name(issue_number),
            base=BASE_REF,
        )
