"""`GitHubClient` adapter over the GitHub SDK (PyGithub).

The SDK is imported lazily so the orchestrator core and its tests run with zero
third-party deps; only constructing/using this adapter touches `github`.

This adapter is exercised by `tests/integration/test_github_sdk_integration.py`
against a real repo (credential-gated, separate from the unit suite, which
uses `MockGitHubClient`).
"""

from __future__ import annotations

from ..interfaces import Comment, GitHubClient, Issue, PullRequest

# --- Projects v2 (GraphQL) --------------------------------------------------- #
# Classic Projects (columns/cards) were sunset by GitHub; the live board API is
# Projects v2, which is GraphQL-only. The board "column" is the value of a
# single-select field (conventionally named "Status"). These are only used when
# a project is configured on the client; otherwise board ops are a no-op.

_PROJECT_FIELD_QUERY = """
query($owner:String!, $number:Int!, $field:String!) {
  repositoryOwner(login:$owner) {
    ... on User { projectV2(number:$number) { id field(name:$field) {
      ... on ProjectV2SingleSelectField { id options { id name } } } } }
    ... on Organization { projectV2(number:$number) { id field(name:$field) {
      ... on ProjectV2SingleSelectField { id options { id name } } } } }
  }
}
"""

_PROJECT_ID_QUERY = """
query($owner:String!, $number:Int!) {
  repositoryOwner(login:$owner) {
    ... on User { projectV2(number:$number) { id } }
    ... on Organization { projectV2(number:$number) { id } }
  }
}
"""

_ISSUE_ITEMS_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    issue(number:$number) {
      id
      projectItems(first:50) { nodes { id project { id } } }
    }
  }
}
"""

_ISSUE_STATUS_QUERY = """
query($owner:String!, $repo:String!, $number:Int!, $field:String!) {
  repository(owner:$owner, name:$repo) {
    issue(number:$number) {
      projectItems(first:50) { nodes {
        project { id }
        fieldValueByName(name:$field) {
          ... on ProjectV2ItemFieldSingleSelectValue { name }
        }
      } }
    }
  }
}
"""

_SET_FIELD_MUTATION = """
mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
  updateProjectV2ItemFieldValue(input:{
    projectId:$project, itemId:$item, fieldId:$field,
    value:{ singleSelectOptionId:$option }
  }) { projectV2Item { id } }
}
"""

_ADD_ITEM_MUTATION = """
mutation($project:ID!, $content:ID!) {
  addProjectV2ItemById(input:{ projectId:$project, contentId:$content }) {
    item { id }
  }
}
"""

_OWNER_ID_QUERY = """
query($owner:String!) { repositoryOwner(login:$owner) { id } }
"""

_CREATE_PROJECT_MUTATION = """
mutation($owner:ID!, $title:String!) {
  createProjectV2(input:{ ownerId:$owner, title:$title }) {
    projectV2 { id number }
  }
}
"""

_PROJECT_STATUS_FIELD_QUERY = """
query($project:ID!, $field:String!) {
  node(id:$project) {
    ... on ProjectV2 { field(name:$field) {
      ... on ProjectV2SingleSelectField { id } } }
  }
}
"""

_SET_FIELD_OPTIONS_MUTATION = """
mutation($field:ID!, $options:[ProjectV2SingleSelectFieldOptionInput!]!) {
  updateProjectV2Field(input:{ fieldId:$field, singleSelectOptions:$options }) {
    projectV2Field { ... on ProjectV2SingleSelectField { id } }
  }
}
"""


def _to_issue(gh_issue) -> Issue:
    return Issue(
        number=gh_issue.number,
        title=gh_issue.title or "",
        body=gh_issue.body or "",
        labels=[lbl.name for lbl in gh_issue.labels],
        state=gh_issue.state,
        author=gh_issue.user.login if gh_issue.user else "",
    )


def _to_pr(pr) -> PullRequest:
    # `get_files()` is one extra real API call per PR -- needed so S10's
    # collision check can treat an open PR's changed files exactly like a
    # live container's reported file list (not yet exercised by the unit
    # suite; real behavior belongs to S19's integration coverage).
    return PullRequest(
        number=pr.number,
        title=pr.title or "",
        body=pr.body or "",
        head=pr.head.ref,
        base=pr.base.ref,
        state=pr.state,
        merged=pr.merged,
        files=[f.filename for f in pr.get_files()],
    )


class SdkGitHubClient(GitHubClient):
    """Wraps a single repository. `token` is a short-lived, single-repo
    installation token minted per issue by the orchestrator (S15)."""

    def __init__(self, token: str, repo: str, *, project: dict | None = None) -> None:
        self._token = token
        self._repo_name = repo
        self._repo = None  # constructed lazily on first use
        self._gh = None  # the Github client, constructed lazily on first use
        # Optional Projects v2 board: {"owner", "number", optional "status_field"}.
        # When None, board ops are a no-op -- the label is the source of truth and
        # the board is a derived, display-only projection of it (S2).
        self._project = project

    def _client(self):
        if self._gh is None:
            from github import Github  # lazy: only when a real call is made

            self._gh = Github(self._token)
        return self._gh

    def _r(self):
        if self._repo is None:
            self._repo = self._client().get_repo(self._repo_name)
        return self._repo

    def _graphql(self, query: str, variables: dict) -> dict:
        """Run a GraphQL query/mutation and return its `data` payload."""
        _headers, payload = self._client().requester.graphql_query(query, variables)
        return payload["data"]

    # ---- issues ---------------------------------------------------------- #
    def get_issue(self, number: int) -> Issue:
        return _to_issue(self._r().get_issue(number))

    def list_issues(self, state: str = "open") -> list[Issue]:
        return [_to_issue(i) for i in self._r().get_issues(state=state) if i.pull_request is None]

    def close_issue(self, number: int) -> None:
        self._r().get_issue(number).edit(state="closed")

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> Issue:
        return _to_issue(self._r().create_issue(title=title, body=body, labels=list(labels or [])))

    # ---- comments -------------------------------------------------------- #
    def post_comment(self, issue_number: int, body: str) -> Comment:
        c = self._r().get_issue(issue_number).create_comment(body)
        return Comment(
            id=c.id,
            body=c.body or "",
            author=c.user.login if c.user else "",
            author_association=getattr(c, "author_association", "NONE"),
        )

    def list_comments(self, issue_number: int) -> list[Comment]:
        return [
            Comment(
                id=c.id,
                body=c.body or "",
                author=c.user.login if c.user else "",
                author_association=getattr(c, "author_association", "NONE"),
            )
            for c in self._r().get_issue(issue_number).get_comments()
        ]

    # ---- labels ---------------------------------------------------------- #
    def get_labels(self, issue_number: int) -> list[str]:
        return [lbl.name for lbl in self._r().get_issue(issue_number).labels]

    def add_label(self, issue_number: int, label: str) -> None:
        self._r().get_issue(issue_number).add_to_labels(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self._r().get_issue(issue_number).remove_from_labels(label)

    def set_labels(self, issue_number: int, labels: list[str]) -> None:
        self._r().get_issue(issue_number).set_labels(*labels)

    def create_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        self._r().create_label(name=name, color=color, description=description)

    # ---- pull requests --------------------------------------------------- #
    def open_pull_request(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PullRequest:
        return _to_pr(self._r().create_pull(title=title, body=body, head=head, base=base))

    def get_pull_request(self, number: int) -> PullRequest:
        return _to_pr(self._r().get_pull(number))

    def list_pull_requests(self, state: str = "open") -> list[PullRequest]:
        return [_to_pr(p) for p in self._r().get_pulls(state=state)]

    def merge_pull_request(self, number: int, method: str = "merge") -> None:
        self._r().get_pull(number).merge(merge_method=method)

    def delete_branch(self, branch: str) -> None:
        self._r().get_git_ref(f"heads/{branch}").delete()

    # ---- repo contents ----------------------------------------------------- #
    def get_file(self, path: str, ref: str) -> str | None:
        from github import GithubException  # lazy: only when a real call is made

        try:
            content = self._r().get_contents(path, ref=ref)
        except GithubException as e:
            if e.status == 404:
                return None
            raise
        return content.decoded_content.decode("utf-8")

    def commit_file(self, branch: str, path: str, content: str, message: str) -> None:
        from github import GithubException  # lazy: only when a real call is made

        try:
            existing = self._r().get_contents(path, ref=branch)
        except GithubException as e:
            if e.status != 404:
                raise
            self._r().create_file(path, message, content, branch=branch)
            return
        self._r().update_file(path, message, content, existing.sha, branch=branch)

    # ---- projects board -------------------------------------------------- #
    # The column is a *derived, display-only* projection of the label (S2); the
    # label is the source of truth. If no Projects v2 board is configured on this
    # client, board ops are a no-op so the label-driven pipeline never depends on
    # (or crashes over) a board. When a project *is* configured, the column maps
    # to a single-select "Status" field value, set via the Projects v2 GraphQL
    # API (PyGithub has no first-class Projects v2 support).
    #
    # The configured GraphQL path is covered by the S19 integration suite
    # (tests/integration/test_github_sdk_integration.py, credential-gated);
    # the no-op path is the one the unit suite exercises.
    def _project_owner_number_field(self) -> tuple[str, int, str]:
        return (
            self._project["owner"],
            int(self._project["number"]),
            self._project.get("status_field", "Status"),
        )

    def _project_item_id(self, issue_number: int, project_id: str, *, create: bool) -> str | None:
        repo_owner, repo_name = self._repo_name.split("/", 1)
        data = self._graphql(
            _ISSUE_ITEMS_QUERY,
            {"owner": repo_owner, "repo": repo_name, "number": issue_number},
        )
        issue = data["repository"]["issue"]
        for node in issue["projectItems"]["nodes"]:
            if node["project"]["id"] == project_id:
                return node["id"]
        if not create:
            return None
        added = self._graphql(
            _ADD_ITEM_MUTATION, {"project": project_id, "content": issue["id"]}
        )
        return added["addProjectV2ItemById"]["item"]["id"]

    def create_board(self, columns: list[str]) -> None:
        """Install-time board creation (S18): create a Projects v2 project on
        the repo owner (or adopt the configured one) and set its single-select
        status field to exactly `columns`. New Projects v2 projects ship with
        a default "Status" field, so the option set is *updated*, never a
        second field created. Covered (opt-in, it creates a real project) by
        the S19 integration suite."""
        owner = (self._project or {}).get("owner") or self._repo_name.split("/", 1)[0]
        field_name = (self._project or {}).get("status_field", "Status")
        if self._project is None:
            owner_id = self._graphql(_OWNER_ID_QUERY, {"owner": owner})[
                "repositoryOwner"
            ]["id"]
            created = self._graphql(
                _CREATE_PROJECT_MUTATION, {"owner": owner_id, "title": "AORC"}
            )["createProjectV2"]["projectV2"]
            project_id = created["id"]
            # Adopt the new project: board ops on this client are live now.
            self._project = {
                "owner": owner,
                "number": created["number"],
                "status_field": field_name,
            }
        else:
            owner_login, number, field_name = self._project_owner_number_field()
            project_id = self._graphql(
                _PROJECT_ID_QUERY, {"owner": owner_login, "number": number}
            )["repositoryOwner"]["projectV2"]["id"]
        field_id = self._graphql(
            _PROJECT_STATUS_FIELD_QUERY, {"project": project_id, "field": field_name}
        )["node"]["field"]["id"]
        self._graphql(
            _SET_FIELD_OPTIONS_MUTATION,
            {
                "field": field_id,
                "options": [
                    {"name": column, "color": "GRAY", "description": ""}
                    for column in columns
                ],
            },
        )

    def set_board_column(self, issue_number: int, column: str) -> None:
        if self._project is None:
            return  # board is derived from the label; nothing to sync
        owner, number, field_name = self._project_owner_number_field()
        pv2 = self._graphql(
            _PROJECT_FIELD_QUERY, {"owner": owner, "number": number, "field": field_name}
        )["repositoryOwner"]["projectV2"]
        project_id = pv2["id"]
        field = pv2["field"]
        option_id = next((o["id"] for o in field["options"] if o["name"] == column), None)
        if option_id is None:
            raise ValueError(
                f"Projects v2 field {field_name!r} has no option named {column!r}"
            )
        item_id = self._project_item_id(issue_number, project_id, create=True)
        self._graphql(
            _SET_FIELD_MUTATION,
            {"project": project_id, "item": item_id, "field": field["id"], "option": option_id},
        )

    def get_board_column(self, issue_number: int) -> str | None:
        if self._project is None:
            return None
        owner, number, field_name = self._project_owner_number_field()
        project_id = self._graphql(
            _PROJECT_ID_QUERY, {"owner": owner, "number": number}
        )["repositoryOwner"]["projectV2"]["id"]
        repo_owner, repo_name = self._repo_name.split("/", 1)
        data = self._graphql(
            _ISSUE_STATUS_QUERY,
            {"owner": repo_owner, "repo": repo_name, "number": issue_number, "field": field_name},
        )
        for node in data["repository"]["issue"]["projectItems"]["nodes"]:
            if node["project"]["id"] == project_id:
                value = node.get("fieldValueByName")
                return value["name"] if value else None
        return None
