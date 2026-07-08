"""S22 -- Build-pipeline driver.

Sequences `DesignStage -> TesterStage -> CoderStage -> ReviewerStage` for one
dispatched issue, threading a single shared worktree
(`WorktreeManager.ensure`) as the `cwd` every stage's `TestRunner` executes
in. The driver's own sequencing (which stage runs next, retries, labels)
stays orchestrator-side (v1 decision, per
`issues/22-pipeline-driver-worktree-sync.md`) -- that part of the "full
in-agent execution path" is still out of scope. But each stage's `TestRunner`
calls -- the actual `setup`/`test`/`lint`/coverage/smoke commands, the part
that runs untrusted LLM-generated code -- do run inside the issue's own
container as of S27 (`tester.ContainerTestRunner`, wired in by `__main__.py`'s
`compose()` whenever `.aorc.yml` selects `container.runtime: docker`, the
default); the container is no longer a cosmetic isolation boundary for that
part.

Label transitions move through the existing S2 `PipelineStateMachine`
(`pipeline.advance`) at every stage boundary -- never a hand-rolled
`add_label`/`remove_label` pair. Re-entry is idempotent via
`ArtifactChecker`, checked per stage before deciding whether to run it or
skip straight to advancing:

- in-design / in-test / in-review each have a real committed artifact
  (design doc / tests marker / open PR) -- if it's already there, the stage
  is skipped and the label is advanced past it.
- in-code has *no* static artifact (`ArtifactChecker.artifact_exists`
  returns `True` unconditionally for it, by original S2 design -- the
  coder's output is verified by running tests, not by presence). Trusting
  that blanket `True` here would skip the coder whenever a resumed issue
  happens to be labeled `in-code`, so this stage is always (re)run rather
  than probed for a bygone artifact.
- in-review resumes by re-reviewing an already-open PR (`ReviewerStage`'s
  existing `pr=` parameter, the S17 stale-PR seam) rather than opening a
  second one.

Stage failure routes through the same `agent-blocked` labeling every other
stage/guard in this codebase uses (`guards.BLOCKED_LABEL` +
`pipeline.LABEL_COLUMN`) -- no new failure channel. Nothing here deletes a
branch on failure, so "branch-preserving" falls out of simply not touching
branches rather than a dedicated teardown call (the driver does not own a
`ContainerHandle` to tear down; that remains the dispatcher's job).
"""

from __future__ import annotations

from dataclasses import dataclass

from .coder import CoderStage
from .design import DesignDoc, DesignStage, design_doc_path, parse_design_response
from .guards import BLOCKED_LABEL
from .harness import WorktreeManager
from .interfaces import GitHubClient, PullRequest
from .pipeline import (
    TERMINAL_LABELS,
    ArtifactChecker,
    LABEL_COLUMN,
    PipelineStateMachine,
    branch_name,
    current_pipeline_label,
)
from .reviewer import ReviewerStage
from .tester import TesterStage

CLARIFICATION_LABEL = "needs-clarification"


@dataclass
class DriverResult:
    status: str  # "proceed" | "needs-clarification" | "agent-blocked" | a terminal label
    stage: str | None = None
    pr: PullRequest | None = None


class PipelineDriver:
    """One shared worktree per issue; the four stages run in sequence
    against it, resuming from wherever the S2 labels + artifact presence
    say the issue actually is."""

    def __init__(
        self,
        github: GitHubClient,
        worktrees: WorktreeManager,
        design: DesignStage,
        tester: TesterStage,
        coder: CoderStage,
        reviewer: ReviewerStage,
        *,
        state_machine: PipelineStateMachine | None = None,
        artifacts: ArtifactChecker | None = None,
    ) -> None:
        self._github = github
        self._worktrees = worktrees
        self._design = design
        self._tester = tester
        self._coder = coder
        self._reviewer = reviewer
        self._artifacts = artifacts or ArtifactChecker(github)
        self._state_machine = state_machine or PipelineStateMachine(github, self._artifacts)

    def run(self, issue_number: int, *, qa: list[str] | None = None) -> DriverResult:
        issue = self._github.get_issue(issue_number)
        label = current_pipeline_label(issue.labels)
        if label in TERMINAL_LABELS:
            return DriverResult(status=label, stage=label)

        cwd = self._worktrees.ensure(issue_number)
        design_doc: DesignDoc | None = None

        if label is None:
            label = self._state_machine.advance(issue_number)  # None -> "in-design"

        if label == "in-design":
            if self._artifacts.design_doc_exists(issue_number):
                label = self._state_machine.advance(issue_number)  # -> "in-test"
            else:
                result = self._design.run(issue_number, issue.body, qa=qa)
                if result.status == "needs-clarification":
                    self._github.add_label(issue_number, CLARIFICATION_LABEL)
                    self._github.set_board_column(issue_number, LABEL_COLUMN[CLARIFICATION_LABEL])
                    return DriverResult(status="needs-clarification", stage="in-design")
                if result.status == "agent-blocked":
                    self._block(issue_number)
                    return DriverResult(status="agent-blocked", stage="in-design")
                design_doc = result.doc
                label = self._state_machine.advance(issue_number)  # -> "in-test"

        if design_doc is None:
            design_doc = self._load_design_doc(issue_number)

        if label == "in-test":
            if self._artifacts.tests_committed(issue_number):
                label = self._state_machine.advance(issue_number)  # -> "in-code"
            else:
                result = self._tester.run(issue_number, design_doc, cwd=cwd)
                if result.status != "proceed":
                    self._block(issue_number)
                    return DriverResult(status="agent-blocked", stage="in-test")
                label = self._state_machine.advance(issue_number)  # -> "in-code"

        if label == "in-code":
            # No static artifact for this stage (see module docstring) --
            # always run it rather than trust a bygone-presence check.
            result = self._coder.run(issue_number, design_doc, cwd=cwd)
            if result.status != "proceed":
                self._block(issue_number)
                return DriverResult(status="agent-blocked", stage="in-code")
            label = self._state_machine.advance(issue_number)  # -> "in-review"

        if label == "in-review":
            existing_pr = self._open_pr_for(issue_number)
            result = self._reviewer.run(issue_number, design_doc, issue.body, cwd=cwd, pr=existing_pr)
            if result.status != "proceed":
                return DriverResult(status="agent-blocked", stage="in-review")
            return DriverResult(status="proceed", stage="in-review", pr=result.pr)

        return DriverResult(status=label or "unknown", stage=label)

    def _load_design_doc(self, issue_number: int) -> DesignDoc | None:
        raw = self._github.get_file(design_doc_path(issue_number), branch_name(issue_number))
        return parse_design_response(raw) if raw is not None else None

    def _open_pr_for(self, issue_number: int) -> PullRequest | None:
        head = branch_name(issue_number)
        for pr in self._github.list_pull_requests(state="open"):
            if pr.head == head:
                return pr
        return None

    def _block(self, issue_number: int) -> None:
        self._github.add_label(issue_number, BLOCKED_LABEL)
        self._github.set_board_column(issue_number, LABEL_COLUMN[BLOCKED_LABEL])
