"""S2 — label state machine + derived board column.

Pipeline state lives exclusively in GitHub labels; the Projects board column
is *derived* from the label via the fixed lookup below, never set directly.
A stage only counts as complete once its artifact is actually committed, so
a crash between "label moved" and "artifact committed" is caught on restart
(`PipelineStateMachine.resume_stage`) instead of silently skipping work.
"""

from __future__ import annotations

from .interfaces import GitHubClient, Issue

LABEL_COLUMN: dict[str, str] = {
    "needs-clarification": "Needs Clarification",
    "in-design": "In Progress",
    "in-test": "In Progress",
    "in-code": "In Progress",
    "in-review": "In Review",
    "agent-blocked": "Blocked",
}
BACKLOG_COLUMN = "Backlog"
DONE_COLUMN = "Done"

# The label progression a healthy issue walks through, in order.
STAGE_ORDER = ["in-design", "in-test", "in-code", "in-review"]

# Waiting on a human; the machine never auto-advances out of these.
TERMINAL_LABELS = {"needs-clarification", "agent-blocked"}

PIPELINE_LABELS = set(STAGE_ORDER) | TERMINAL_LABELS


def branch_name(issue_number: int) -> str:
    return f"aorc/issue-{issue_number}"


def derive_column(issue: Issue) -> str:
    """Pure label -> column lookup. A closed issue is always Done regardless
    of whatever label it happened to carry when it was closed."""
    if issue.state == "closed":
        return DONE_COLUMN
    for label in issue.labels:
        if label in LABEL_COLUMN:
            return LABEL_COLUMN[label]
    return BACKLOG_COLUMN


def current_pipeline_label(labels: list[str]) -> str | None:
    """The single stage/terminal label governing this issue, if any."""
    for label in labels:
        if label in PIPELINE_LABELS:
            return label
    return None


def next_label(label: str | None) -> str | None:
    """Pure label -> label transition along the happy path. Terminal labels
    don't auto-advance (a human/other slice must clear them first); the
    final stage (`in-review`) has no further label -- it waits for a human
    to merge the PR (S17)."""
    if label in TERMINAL_LABELS:
        return label
    if label is None:
        return STAGE_ORDER[0]
    idx = STAGE_ORDER.index(label)
    if idx + 1 < len(STAGE_ORDER):
        return STAGE_ORDER[idx + 1]
    return None


class ArtifactChecker:
    """Whether a stage's artifact is actually committed -- checked only
    through the `GitHubClient` seam (architecture invariant #1), never by
    touching git directly."""

    def __init__(self, github: GitHubClient) -> None:
        self._github = github

    def design_doc_exists(self, issue_number: int) -> bool:
        path = f"aorc/issue-{issue_number}/design.md"
        return self._github.get_file(path, branch_name(issue_number)) is not None

    def tests_committed(self, issue_number: int) -> bool:
        path = f"aorc/issue-{issue_number}/tests.marker"
        return self._github.get_file(path, branch_name(issue_number)) is not None

    def pr_opened(self, issue_number: int) -> bool:
        head = branch_name(issue_number)
        return any(pr.head == head for pr in self._github.list_pull_requests(state="open"))

    def artifact_exists(self, issue_number: int, stage: str) -> bool:
        check = {
            "in-design": self.design_doc_exists,
            "in-test": self.tests_committed,
            "in-review": self.pr_opened,
        }.get(stage)
        # "in-code" has no static artifact of its own in this slice -- the
        # coder's output is verified by running tests (S7), not by presence.
        return check(issue_number) if check else True


class PipelineStateMachine:
    """Label event -> next stage + derived column, gated by artifact
    presence so the label alone can never advance pipeline state."""

    def __init__(self, github: GitHubClient, artifacts: ArtifactChecker | None = None) -> None:
        self._github = github
        self._artifacts = artifacts or ArtifactChecker(github)

    def resume_stage(self, issue_number: int) -> str | None:
        """Crash recovery: reconstruct the real stage to resume at from
        labels + artifact presence. If the label says a stage is active but
        its artifact isn't actually there, resume at that stage (redo it);
        if the artifact is there, the stage is genuinely done."""
        issue = self._github.get_issue(issue_number)
        label = current_pipeline_label(issue.labels)
        if label is None or label in TERMINAL_LABELS:
            return label
        if self._artifacts.artifact_exists(issue_number, label):
            return next_label(label)
        return label

    def advance(self, issue_number: int) -> str | None:
        """Attempt to move an issue to its next stage. Refuses (no-op,
        returns the current label) if the current stage's artifact isn't
        committed yet, or if the label is terminal."""
        issue = self._github.get_issue(issue_number)
        label = current_pipeline_label(issue.labels)
        if label in TERMINAL_LABELS:
            return label
        if label is not None and not self._artifacts.artifact_exists(issue_number, label):
            return label
        new_label = next_label(label)
        if new_label is None or new_label == label:
            return label
        if label is not None:
            self._github.remove_label(issue_number, label)
        self._github.add_label(issue_number, new_label)
        self._github.set_board_column(issue_number, LABEL_COLUMN[new_label])
        return new_label
