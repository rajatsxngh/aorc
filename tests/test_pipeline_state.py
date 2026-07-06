"""S2 — label state machine + derived board column.

Column is *derived* from label (never set directly by agents); a stage only
counts as complete once its artifact is actually committed, so a crash
between "label moved" and "artifact committed" is caught on restart instead
of silently skipping work.
"""

from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue, PullRequest
from aorc.pipeline import (
    ArtifactChecker,
    PipelineStateMachine,
    branch_name,
    derive_column,
    next_label,
)


def test_derive_column_fixed_lookup():
    assert derive_column(Issue(number=1, labels=[])) == "Backlog"
    assert derive_column(Issue(number=1, labels=["needs-clarification"])) == "Needs Clarification"
    assert derive_column(Issue(number=1, labels=["in-design"])) == "In Progress"
    assert derive_column(Issue(number=1, labels=["in-test"])) == "In Progress"
    assert derive_column(Issue(number=1, labels=["in-code"])) == "In Progress"
    assert derive_column(Issue(number=1, labels=["in-review"])) == "In Review"
    assert derive_column(Issue(number=1, labels=["agent-blocked"])) == "Blocked"


def test_closed_issue_is_always_done_regardless_of_label():
    assert derive_column(Issue(number=1, labels=["in-review"], state="closed")) == "Done"
    assert derive_column(Issue(number=1, labels=[], state="closed")) == "Done"


def test_next_label_pure_transitions():
    assert next_label(None) == "in-design"
    assert next_label("in-design") == "in-test"
    assert next_label("in-test") == "in-code"
    assert next_label("in-code") == "in-review"
    assert next_label("in-review") is None  # waits for human merge (S17)


def test_next_label_terminal_states_do_not_auto_advance():
    assert next_label("needs-clarification") == "needs-clarification"
    assert next_label("agent-blocked") == "agent-blocked"


def test_advance_refuses_when_artifact_missing():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=["in-design"])])
    sm = PipelineStateMachine(gh)
    result = sm.advance(1)
    assert result == "in-design"
    assert gh.get_labels(1) == ["in-design"]  # unchanged: label alone never advances state
    assert not any(c[0] == "add_label" for c in gh.calls)


def test_advance_proceeds_once_artifact_committed():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=["in-design"])])
    gh.add_file(branch_name(1), "aorc/issue-1/design.md", "# design")
    sm = PipelineStateMachine(gh)
    result = sm.advance(1)
    assert result == "in-test"
    assert gh.get_labels(1) == ["in-test"]
    assert gh.get_board_column(1) == "In Progress"


def test_advance_from_backlog_starts_design_no_artifact_required():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=[])])
    sm = PipelineStateMachine(gh)
    result = sm.advance(1)
    assert result == "in-design"
    assert gh.get_labels(1) == ["in-design"]
    assert gh.get_board_column(1) == "In Progress"


def test_advance_review_stage_waits_for_pr_merge_no_further_auto_advance():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=["in-review"])])
    gh.pulls[9001] = PullRequest(number=9001, head=branch_name(1), state="open")
    sm = PipelineStateMachine(gh)
    result = sm.advance(1)
    assert result == "in-review"
    assert gh.get_labels(1) == ["in-review"]


def test_advance_refuses_on_terminal_labels():
    for label in ("needs-clarification", "agent-blocked"):
        gh = MockGitHubClient(issues=[Issue(number=1, labels=[label])])
        sm = PipelineStateMachine(gh)
        assert sm.advance(1) == label
        assert gh.get_labels(1) == [label]


def test_artifact_checker_design_doc():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    checker = ArtifactChecker(gh)
    assert checker.artifact_exists(1, "in-design") is False
    gh.add_file(branch_name(1), "aorc/issue-1/design.md", "# design")
    assert checker.artifact_exists(1, "in-design") is True


def test_artifact_checker_tests_committed():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    checker = ArtifactChecker(gh)
    assert checker.artifact_exists(1, "in-test") is False
    gh.add_file(branch_name(1), "aorc/issue-1/tests.marker", "ok")
    assert checker.artifact_exists(1, "in-test") is True


def test_artifact_checker_pr_opened():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    checker = ArtifactChecker(gh)
    assert checker.artifact_exists(1, "in-review") is False
    gh.pulls[9001] = PullRequest(number=9001, head=branch_name(1), state="open")
    assert checker.artifact_exists(1, "in-review") is True


def test_artifact_checker_in_code_has_no_static_artifact_gate():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    checker = ArtifactChecker(gh)
    assert checker.artifact_exists(1, "in-code") is True


def test_resume_stage_reconstructs_state_after_crash_before_artifact_committed():
    # label already moved to "in-test" but the tester container crashed
    # before actually committing the failing-test artifact.
    gh = MockGitHubClient(issues=[Issue(number=1, labels=["in-test"])])
    sm = PipelineStateMachine(gh)
    assert sm.resume_stage(1) == "in-test"  # redo this stage, don't skip ahead


def test_resume_stage_advances_past_completed_stage():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=["in-design"])])
    gh.add_file(branch_name(1), "aorc/issue-1/design.md", "# design")
    sm = PipelineStateMachine(gh)
    assert sm.resume_stage(1) == "in-test"


def test_resume_stage_backlog_and_terminal_pass_through():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=[])])
    sm = PipelineStateMachine(gh)
    assert sm.resume_stage(1) is None

    gh2 = MockGitHubClient(issues=[Issue(number=1, labels=["agent-blocked"])])
    sm2 = PipelineStateMachine(gh2)
    assert sm2.resume_stage(1) == "agent-blocked"
