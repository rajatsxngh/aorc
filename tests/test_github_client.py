"""S1 — `GitHubClient` seam: the in-memory mock records calls and holds state
so every downstream slice is unit-testable without a real repo.
"""

import pytest

from aorc.github.mock import MockGitHubClient, UnknownBranchError
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


def test_pull_request_carries_files_for_collision_checks():
    pr = PullRequest(number=1, head="aorc/issue-9", files=["src/aorc/foo.py"])
    gh = MockGitHubClient(pulls=[pr])

    assert gh.get_pull_request(1).files == ["src/aorc/foo.py"]
    assert gh.list_pull_requests()[0].files == ["src/aorc/foo.py"]


def test_pull_request_files_default_to_empty_list():
    assert PullRequest(number=1).files == []


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


def test_create_issue_assigns_number_and_labels():
    gh = MockGitHubClient(issues=[Issue(number=5)])
    issue = gh.create_issue("Sub-issue", "body text", labels=["depth:1"])

    assert issue.number == 6
    assert gh.get_issue(6).title == "Sub-issue"
    assert gh.get_issue(6).labels == ["depth:1"]
    assert ("create_issue", 6, "Sub-issue") in gh.calls


def test_commit_file_then_readable_via_get_file():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    assert gh.get_file("aorc/issue-1/design.md", "aorc/issue-1") is None

    gh.create_branch("aorc/issue-1")
    gh.commit_file("aorc/issue-1", "aorc/issue-1/design.md", "content", message="design: #1")

    assert gh.get_file("aorc/issue-1/design.md", "aorc/issue-1") == "content"
    assert ("commit_file", "aorc/issue-1", "aorc/issue-1/design.md", "design: #1") in gh.calls


# ---- S29: commit to a branch nobody created must fail like real GitHub ----- #
# Live run-issue finding: the contents API 404s on the first commit to a fresh
# `aorc/issue-<n>` because nothing created the branch. The mock recorded such
# commits regardless of branch existence, hiding the whole bug class.


def test_commit_file_to_nonexistent_branch_raises_like_real_github():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    with pytest.raises(UnknownBranchError):
        gh.commit_file("aorc/issue-1", "aorc/issue-1/design.md", "content", message="design: #1")


def test_commit_file_to_default_branch_needs_no_create():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.commit_file("main", "README.md", "hi", message="docs")
    assert gh.get_file("README.md", "main") == "hi"


def test_create_branch_records_resolved_base_and_is_idempotent():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.create_branch("aorc/issue-1")
    gh.create_branch("aorc/issue-1")  # re-dispatch: no-op, never raises
    assert ("create_branch", "aorc/issue-1", "main") in gh.calls


def test_create_branch_from_unknown_base_raises():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    with pytest.raises(UnknownBranchError):
        gh.create_branch("aorc/issue-1", from_ref="no-such-base")


def test_add_file_helper_registers_its_ref_as_existing_branch():
    # Simulating an already-committed artifact on a branch implies the branch
    # exists -- resume-style tests keep working without extra setup.
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.add_file("aorc/issue-1", "aorc/issue-1/design.md", "doc")
    gh.commit_file("aorc/issue-1", "aorc/issue-1/tests.md", "t", message="tests")
    assert gh.get_file("aorc/issue-1/tests.md", "aorc/issue-1") == "t"


def test_delete_branch_unregisters_it():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.create_branch("aorc/issue-1")
    gh.delete_branch("aorc/issue-1")
    with pytest.raises(UnknownBranchError):
        gh.commit_file("aorc/issue-1", "f.md", "c", message="m")


# ---- S29: SdkGitHubClient.create_branch against a fake git-refs API -------- #


def _sdk_client_with_fake_repo(fake_repo):
    from aorc.github.sdk_adapter import SdkGitHubClient

    client = SdkGitHubClient(token="t", repo="owner/repo")
    client._repo = fake_repo
    return client


class _FakeGitRef:
    def __init__(self, sha):
        self.object = type("O", (), {"sha": sha})()


class _FakeRefsRepo:
    default_branch = "main"

    def __init__(self):
        self.refs = {"heads/main": _FakeGitRef("abc123")}
        self.created = []

    def get_git_ref(self, ref):
        from github import GithubException

        if ref in self.refs:
            return self.refs[ref]
        raise GithubException(404, {"message": "Not Found"}, {})

    def create_git_ref(self, ref, sha):
        self.created.append((ref, sha))
        self.refs[ref.removeprefix("refs/")] = _FakeGitRef(sha)


def test_sdk_create_branch_creates_ref_from_default_branch_head():
    pytest.importorskip("github")
    fake = _FakeRefsRepo()
    client = _sdk_client_with_fake_repo(fake)

    client.create_branch("aorc/issue-9")

    assert fake.created == [("refs/heads/aorc/issue-9", "abc123")]


def test_sdk_create_branch_noops_when_ref_already_exists():
    pytest.importorskip("github")
    fake = _FakeRefsRepo()
    fake.refs["heads/aorc/issue-9"] = _FakeGitRef("def456")
    client = _sdk_client_with_fake_repo(fake)

    client.create_branch("aorc/issue-9")

    assert fake.created == []


# ---- S28: forbidden board creation degrades to label-only ------------------ #
# Live rung-1 finding: fine-grained PATs can't create Projects v2 (GraphQL
# createProjectV2 -> 400 FORBIDDEN "Resource not accessible by personal access
# token"). The board is a derived, display-only projection of the labels (S2),
# so that refusal must degrade to label-only operation, never crash.

class _FakeForbidden(Exception):
    """Duck-types PyGithub's GithubException for the auth-refusal shape,
    keeping the unit suite free of third-party imports."""

    status = 400
    data = {
        "errors": [
            {
                "type": "FORBIDDEN",
                "message": "Resource not accessible by personal access token",
            }
        ]
    }


def _forbidden_sdk_client():
    from aorc.github.sdk_adapter import SdkGitHubClient

    client = SdkGitHubClient(token="t", repo="owner/repo")

    def refuse(query, variables):
        raise _FakeForbidden("400 FORBIDDEN")

    client._graphql = refuse
    return client


def test_forbidden_create_board_degrades_to_label_only(caplog):
    client = _forbidden_sdk_client()

    client.create_board(["Todo", "In progress", "Done"])  # must not raise

    assert any(
        "labels only" in record.getMessage() for record in caplog.records
    ), "degrade must be logged as board unavailable / proceeding with labels only"
    # Degraded client: board ops are the existing project=None no-ops.
    assert client.get_board_column(1) is None
    client.set_board_column(1, "Todo")  # no-op, no GraphQL call


def test_non_auth_create_board_error_still_raises():
    from aorc.github.sdk_adapter import SdkGitHubClient

    client = SdkGitHubClient(token="t", repo="owner/repo")

    class Boom(Exception):
        pass

    def explode(query, variables):
        raise Boom("network down")

    client._graphql = explode

    try:
        client.create_board(["Todo"])
    except Boom:
        pass
    else:
        raise AssertionError("non-auth errors must propagate unchanged")
