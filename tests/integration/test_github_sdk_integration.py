"""S19 -- real-adapter smoke: `SdkGitHubClient` (PyGithub + Projects v2 GraphQL)
against a real, throwaway GitHub repository.

Gating env vars (anything missing -> clean skip):

- ``AORC_IT_GITHUB_TOKEN``  -- token with issues/contents/PR write on the repo
  (and project scope for the board tests)
- ``AORC_IT_GITHUB_REPO``   -- ``owner/name`` of a THROWAWAY repo this suite may
  mutate (it creates/closes issues, branches, and PRs)
- ``AORC_IT_GITHUB_PROJECT_OWNER`` + ``AORC_IT_GITHUB_PROJECT_NUMBER``
  (optional ``AORC_IT_GITHUB_PROJECT_COLUMN``, default ``Todo``) -- a real
  configured Projects v2 board for the set/get column round-trip
- ``AORC_IT_GITHUB_ALLOW_CREATE_PROJECT=1`` -- opt-in for the `create_board`
  test, which creates (and then deletes) a whole Projects v2 project on the
  repo owner

Everything the tests create is uniquely named (uuid) and cleaned up in
``finally`` blocks, but the repo should still be disposable.
"""

from __future__ import annotations

import os
import uuid

import pytest

from aorc.install import STANDARD_COLUMNS

pytestmark = pytest.mark.integration


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture(scope="module")
def client():
    pytest.importorskip("github", reason="PyGithub extra not installed")
    token = _require_env("AORC_IT_GITHUB_TOKEN")
    repo = _require_env("AORC_IT_GITHUB_REPO")
    from aorc.github.sdk_adapter import SdkGitHubClient

    return SdkGitHubClient(token, repo)


@pytest.fixture()
def scratch_issue(client):
    """A real issue to poke at, closed again afterwards."""
    issue = client.create_issue(
        f"aorc-it smoke {uuid.uuid4().hex[:8]}",
        "Created by AORC S19 integration tests; safe to delete.",
    )
    yield issue
    client.close_issue(issue.number)


def test_issue_read_and_label_roundtrip(client, scratch_issue):
    label = f"aorc-it-{uuid.uuid4().hex[:8]}"
    client.create_label(label, color="ededed", description="AORC S19 smoke")

    fetched = client.get_issue(scratch_issue.number)
    assert fetched.number == scratch_issue.number
    assert fetched.title == scratch_issue.title
    assert fetched.state == "open"

    client.add_label(scratch_issue.number, label)
    assert label in client.get_labels(scratch_issue.number)

    client.remove_label(scratch_issue.number, label)
    assert label not in client.get_labels(scratch_issue.number)

    assert any(i.number == scratch_issue.number for i in client.list_issues())


def test_file_branch_and_pr_lifecycle(client, scratch_issue):
    """commit_file -> get_file (present + 404->None) -> open PR -> delete_branch,
    all against real GitHub."""
    uid = uuid.uuid4().hex[:8]
    branch = f"aorc-it/{uid}"
    path = f"aorc-it/{uid}.txt"

    # Branch creation is normally git-push territory (LocalGitOps), not part of
    # the GitHubClient seam -- the test uses the SDK directly for this one
    # scaffolding step.
    repo = client._r()
    base = repo.get_branch(repo.default_branch)
    repo.create_git_ref(f"refs/heads/{branch}", base.commit.sha)

    pr_number = None
    try:
        client.commit_file(branch, path, "hello from S19\n", "aorc-it: add smoke file")
        assert client.get_file(path, ref=branch) == "hello from S19\n"
        # And the update path (file now exists, so commit_file must go through
        # update_file rather than create_file).
        client.commit_file(branch, path, "updated\n", "aorc-it: update smoke file")
        assert client.get_file(path, ref=branch) == "updated\n"
        assert client.get_file(f"no-such-{uid}.txt", ref=branch) is None

        pr = client.open_pull_request(
            f"aorc-it smoke PR {uid}",
            "Opened by AORC S19 integration tests; safe to close.",
            head=branch,
            base=repo.default_branch,
        )
        pr_number = pr.number
        assert pr.head == branch
        assert pr.state == "open"
        assert not pr.merged
        assert path in pr.files

        fetched = client.get_pull_request(pr.number)
        assert fetched.number == pr.number
        assert path in fetched.files
        assert any(p.number == pr.number for p in client.list_pull_requests())
    finally:
        if pr_number is not None:
            repo.get_pull(pr_number).edit(state="closed")
        client.delete_branch(branch)

    # delete_branch really deleted it.
    from github import GithubException

    with pytest.raises(GithubException):
        repo.get_git_ref(f"heads/{branch}")


def test_comments_roundtrip(client, scratch_issue):
    body = f"aorc-it comment {uuid.uuid4().hex[:8]}"
    posted = client.post_comment(scratch_issue.number, body)
    assert posted.body == body
    assert any(c.id == posted.id for c in client.list_comments(scratch_issue.number))


def test_board_column_roundtrip_on_configured_project(scratch_issue):
    """Projects v2 GraphQL read/write path against a real configured project."""
    pytest.importorskip("github", reason="PyGithub extra not installed")
    token = _require_env("AORC_IT_GITHUB_TOKEN")
    repo = _require_env("AORC_IT_GITHUB_REPO")
    owner = _require_env("AORC_IT_GITHUB_PROJECT_OWNER")
    number = int(_require_env("AORC_IT_GITHUB_PROJECT_NUMBER"))
    # `or` (not a .get default): CI passes unset vars through as "".
    column = os.environ.get("AORC_IT_GITHUB_PROJECT_COLUMN") or "Todo"
    from aorc.github.sdk_adapter import SdkGitHubClient

    boarded = SdkGitHubClient(
        token, repo, project={"owner": owner, "number": number}
    )

    boarded.set_board_column(scratch_issue.number, column)
    assert boarded.get_board_column(scratch_issue.number) == column


def test_create_board_real_project():
    """S18's `create_board` GraphQL path: create a Projects v2 project, update
    its Status options to the six standard columns, then use the adopted board
    for a set/get round-trip. Opt-in (creates a whole project on the owner);
    the project is deleted again afterwards."""
    pytest.importorskip("github", reason="PyGithub extra not installed")
    if os.environ.get("AORC_IT_GITHUB_ALLOW_CREATE_PROJECT") != "1":
        pytest.skip("AORC_IT_GITHUB_ALLOW_CREATE_PROJECT not set to 1")
    token = _require_env("AORC_IT_GITHUB_TOKEN")
    repo = _require_env("AORC_IT_GITHUB_REPO")
    from aorc.github.sdk_adapter import SdkGitHubClient

    fresh = SdkGitHubClient(token, repo)
    assert fresh.get_board_column(1) is None  # unconfigured -> board is a no-op

    fresh.create_board(list(STANDARD_COLUMNS))
    assert fresh._project is not None  # adopted the project it just created

    issue = fresh.create_issue(
        f"aorc-it board smoke {uuid.uuid4().hex[:8]}",
        "Created by AORC S19 integration tests; safe to delete.",
    )
    try:
        fresh.set_board_column(issue.number, "In Progress")
        assert fresh.get_board_column(issue.number) == "In Progress"
        # A column create_board never wrote must not exist on the field.
        with pytest.raises(ValueError):
            fresh.set_board_column(issue.number, "No Such Column")
    finally:
        fresh.close_issue(issue.number)
        project_id = fresh._graphql(
            """query($owner:String!, $number:Int!) {
                 repositoryOwner(login:$owner) { projectV2(number:$number) { id } }
               }""",
            {
                "owner": fresh._project["owner"],
                "number": int(fresh._project["number"]),
            },
        )["repositoryOwner"]["projectV2"]["id"]
        fresh._graphql(
            """mutation($project:ID!) {
                 deleteProjectV2(input:{ projectId:$project }) { clientMutationId }
               }""",
            {"project": project_id},
        )
