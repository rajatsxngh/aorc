"""S1 — `GitHubClient` seam: the in-memory mock records calls and holds state
so every downstream slice is unit-testable without a real repo.
"""

from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Comment, GitHubClient, Issue, PullRequest


def test_mock_satisfies_contract():
    assert isinstance(MockGitHubClient(), GitHubClient)


def test_read_issues():
    gh = MockGitHubClient(issues=[Issue(number=7, title="t", body="b", labels=["epic"])])
    issue = gh.get_issue(7)
    assert issue.title == "t"
    assert "epic" in issue.labels
    assert [i.number for i in gh.list_issues()] == [7]


def test_comments_roundtrip_and_recorded():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    c = gh.post_comment(1, "please clarify")
    assert isinstance(c, Comment)
    assert gh.list_comments(1)[0].body == "please clarify"
    assert ("post_comment", 1, "please clarify") in gh.calls


def test_label_operations():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.add_label(1, "in-design")
    assert gh.get_labels(1) == ["in-design"]
    gh.add_label(1, "in-test")
    gh.remove_label(1, "in-design")
    assert gh.get_labels(1) == ["in-test"]
    gh.set_labels(1, ["in-review"])
    assert gh.get_labels(1) == ["in-review"]
    gh.create_label("agent-blocked", color="ff0000")
    assert "agent-blocked" in gh.created_labels


def test_pull_request_open_and_merge():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    pr = gh.open_pull_request(title="Fix", body="d", head="aorc/issue-1", base="main")
    assert isinstance(pr, PullRequest)
    assert pr.head == "aorc/issue-1"
    gh.merge_pull_request(pr.number)
    assert gh.get_pull_request(pr.number).merged is True


def test_projects_board_column_derived_not_direct_but_settable_via_client():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.set_board_column(1, "In Progress")
    assert gh.get_board_column(1) == "In Progress"


def test_board_is_noop_when_no_project_configured():
    # mirrors SdkGitHubClient(project=None): board is a derived, display-only
    # projection of the label, so an unconfigured board never records or crashes.
    gh = MockGitHubClient(issues=[Issue(number=1)], project=None)
    gh.set_board_column(1, "In Progress")
    assert gh.get_board_column(1) is None
    assert not any(c[0] == "set_board_column" for c in gh.calls)


def test_close_issue():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.close_issue(1)
    assert gh.get_issue(1).state == "closed"
