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
from typing import Callable

from .coder import CoderStage
from .design import (
    DesignDoc,
    DesignStage,
    checkpoint_report,
    design_doc_path,
    mentioned_files,
    parse_design_response,
    resolve_design_files,
)
from .guards import BLOCKED_LABEL
from .harness import CheckpointReport, WorktreeManager, read_worktree_file
from .interfaces import GitHubClient, PullRequest
from .pipeline import (
    HELD_LABEL,
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

# S31: blocking posts an explanatory comment (same pattern as
# `harness.enforce_wall_clock`'s wall-clock ping) -- greppable marker first.
BLOCKED_PING_MARKER = "<!-- aorc:blocked-ping -->"

# S43: a checkpoint hold posts one explanatory comment; deduped by marker so
# the sweep's release -> re-hold cycle (every wake, until the collision
# clears) doesn't spam the issue.
HELD_PING_MARKER = "<!-- aorc:checkpoint-hold -->"


@dataclass
class DriverResult:
    status: str  # "proceed" | "needs-clarification" | "agent-blocked" | "held" | a terminal label
    stage: str | None = None
    pr: PullRequest | None = None
    # S31: the blocking stage's own account of what failed -- empty unless
    # status is "agent-blocked".
    reason: str = ""


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
        checkpoint: Callable[[CheckpointReport], str] | None = None,
    ) -> None:
        self._github = github
        self._worktrees = worktrees
        self._design = design
        self._tester = tester
        self._coder = coder
        self._reviewer = reviewer
        self._artifacts = artifacts or ArtifactChecker(github)
        self._state_machine = state_machine or PipelineStateMachine(github, self._artifacts)
        # S43: the S4/S10 post-design collision gate. MUST be the shared
        # harness's `ContainerHarness.checkpoint` in production (compose wires
        # it) so every driver run records claims into -- and checks against --
        # the same `InFlightRegistry`; a driver-private checkpoint would look
        # at an empty registry and never detect anything (live issues 24/25).
        self._checkpoint = checkpoint

    def run(self, issue_number: int, *, qa: list[str] | None = None) -> DriverResult:
        issue = self._github.get_issue(issue_number)
        label = current_pipeline_label(issue.labels)
        if label in TERMINAL_LABELS:
            return DriverResult(status=label, stage=label)

        cwd = self._worktrees.ensure(issue_number)
        # S29: `ensure` creates the branch only in the local clone; the API
        # commits every stage makes land on the remote, where the contents
        # API never auto-creates a branch (first commit_file would 404).
        self._github.create_branch(branch_name(issue_number))
        design_doc: DesignDoc | None = None

        if label is None:
            label = self._state_machine.advance(issue_number)  # None -> "in-design"

        if label == "in-design":
            if self._artifacts.design_doc_exists(issue_number):
                label = self._state_machine.advance(issue_number)  # -> "in-test"
            else:
                # S44, design half: put the current contents of every
                # existing file the issue text names in front of the design
                # agent, so the design accounts for interfaces already there.
                context_files = self._worktree_contents(
                    mentioned_files(issue.body or "", cwd), cwd
                )
                result = self._design.run(
                    issue_number, issue.body, qa=qa, repo_files=context_files
                )
                if result.status == "needs-clarification":
                    self._github.add_label(issue_number, CLARIFICATION_LABEL)
                    self._github.set_board_column(issue_number, LABEL_COLUMN[CLARIFICATION_LABEL])
                    return DriverResult(status="needs-clarification", stage="in-design")
                if result.status == "agent-blocked":
                    reason = f"design stage returned agent-blocked after {result.attempts} attempts"
                    self._block(issue_number, "in-design", reason)
                    return DriverResult(status="agent-blocked", stage="in-design", reason=reason)
                design_doc = result.doc
                label = self._state_machine.advance(issue_number)  # -> "in-test"

        if design_doc is None:
            design_doc = self._load_design_doc(issue_number)

        if design_doc is not None:
            # S42: snap the design's file paths to the real worktree tree
            # before ANY stage consumes them -- an unqualified path
            # ("math_utils.py" for src/sandbox/math_utils.py) otherwise
            # poisons module derivation, stub seeding, the generated test's
            # imports, and the coder's writes, all consistently, so nothing
            # fails loudly (live issue 21).
            design_doc.files = resolve_design_files(design_doc.files, cwd)

        if design_doc is not None and self._checkpoint is not None:
            # S43: the post-design checkpoint. Runs on every entry with a
            # design doc (fresh or resumed) -- the collision picture can
            # change between wakes, and the issue's own open PR is excluded
            # from the collision set, so a clean resume always proceeds. On
            # "proceed" the verdict records this issue's claim in the shared
            # registry, which is exactly what the NEXT dispatch checks.
            if self._checkpoint(checkpoint_report(issue_number, design_doc)) == "hold":
                reason = (
                    f"files {sorted(design_doc.files)} overlap another in-flight "
                    "issue's claimed files or an open AORC PR's changed files"
                )
                self._hold(issue_number, reason)
                return DriverResult(status="held", stage="checkpoint", reason=reason)

        if label == "in-test":
            if self._artifacts.tests_committed(issue_number):
                label = self._state_machine.advance(issue_number)  # -> "in-code"
            else:
                result = self._tester.run(issue_number, design_doc, cwd=cwd)
                if result.status != "proceed":
                    self._block(issue_number, "in-test", result.reason)
                    return DriverResult(status="agent-blocked", stage="in-test", reason=result.reason)
                label = self._state_machine.advance(issue_number)  # -> "in-code"

        if label == "in-code":
            # No static artifact for this stage (see module docstring) --
            # always run it rather than trust a bygone-presence check.
            # S44 (live issue 29): the coder writes FULL file contents, so it
            # must see each design file's CURRENT contents -- the worktree is
            # the synced source of truth (S22) and by now holds pre-existing
            # code plus the tester's seeded stubs. Fed nothing, the coder
            # rewrites shared files from a blank slate and deletes every
            # function the design doc didn't mention.
            repo_files = self._worktree_contents(design_doc.files, cwd)
            result = self._coder.run(issue_number, design_doc, repo_files=repo_files, cwd=cwd)
            if result.status != "proceed":
                self._block(issue_number, "in-code", result.reason)
                return DriverResult(status="agent-blocked", stage="in-code", reason=result.reason)
            label = self._state_machine.advance(issue_number)  # -> "in-review"

        if label == "in-review":
            existing_pr = self._open_pr_for(issue_number)
            result = self._reviewer.run(issue_number, design_doc, issue.body, cwd=cwd, pr=existing_pr)
            if result.status != "proceed":
                return DriverResult(status="agent-blocked", stage="in-review")
            return DriverResult(status="proceed", stage="in-review", pr=result.pr)

        return DriverResult(status=label or "unknown", stage=label)

    @staticmethod
    def _worktree_contents(paths: list, cwd: str) -> dict[str, str]:
        contents = {}
        for path in paths:
            if isinstance(path, str):
                content = read_worktree_file(cwd, path)
                if content is not None:
                    contents[path] = content
        return contents

    def _load_design_doc(self, issue_number: int) -> DesignDoc | None:
        raw = self._github.get_file(design_doc_path(issue_number), branch_name(issue_number))
        return parse_design_response(raw) if raw is not None else None

    def _open_pr_for(self, issue_number: int) -> PullRequest | None:
        head = branch_name(issue_number)
        for pr in self._github.list_pull_requests(state="open"):
            if pr.head == head:
                return pr
        return None

    def _hold(self, issue_number: int, reason: str) -> None:
        self._github.add_label(issue_number, HELD_LABEL)
        self._github.set_board_column(issue_number, LABEL_COLUMN[HELD_LABEL])
        comments = self._github.list_comments(issue_number)
        if not any(HELD_PING_MARKER in c.body for c in comments):
            self._github.post_comment(
                issue_number,
                f"{HELD_PING_MARKER}\nHolding at the post-design checkpoint: {reason}. "
                "The merge-time sweep re-evaluates this issue on every wake.",
            )

    def _block(self, issue_number: int, stage: str, reason: str) -> None:
        self._github.add_label(issue_number, BLOCKED_LABEL)
        self._github.set_board_column(issue_number, LABEL_COLUMN[BLOCKED_LABEL])
        self._github.post_comment(
            issue_number,
            f"{BLOCKED_PING_MARKER}\nStopping: stage `{stage}` failed. Labeling "
            f"`{BLOCKED_LABEL}`.\nReason:\n{reason or '(no reason recorded)'}",
        )
