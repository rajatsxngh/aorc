"""`GitHubClient` adapter over the GitHub SDK (PyGithub).

The SDK is imported lazily so the orchestrator core and its tests run with zero
third-party deps; only constructing/using this adapter touches `github`.

This adapter is exercised by integration tests against a real repo, not the
unit suite (which uses `MockGitHubClient`).
"""

from __future__ import annotations

from ..interfaces import Comment, GitHubClient, Issue, PullRequest


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
    return PullRequest(
        number=pr.number,
        title=pr.title or "",
        body=pr.body or "",
        head=pr.head.ref,
        base=pr.base.ref,
        state=pr.state,
        merged=pr.merged,
    )


class SdkGitHubClient(GitHubClient):
    """Wraps a single repository. `token` is a short-lived, single-repo
    installation token minted per issue by the orchestrator (S15)."""

    def __init__(self, token: str, repo: str) -> None:
        self._token = token
        self._repo_name = repo
        self._repo = None  # constructed lazily on first use

    def _r(self):
        if self._repo is None:
            from github import Github  # lazy: only when a real call is made

            self._repo = Github(self._token).get_repo(self._repo_name)
        return self._repo

    # ---- issues ---------------------------------------------------------- #
    def get_issue(self, number: int) -> Issue:
        return _to_issue(self._r().get_issue(number))

    def list_issues(self, state: str = "open") -> list[Issue]:
        return [_to_issue(i) for i in self._r().get_issues(state=state) if i.pull_request is None]

    def close_issue(self, number: int) -> None:
        self._r().get_issue(number).edit(state="closed")

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

    # ---- projects board -------------------------------------------------- #
    # Projects (v2) board updates go through the GraphQL API; wired up in S2
    # alongside the label→column state machine. Kept explicit here so the seam
    # is complete and the missing piece is legible, not silently absent.
    def set_board_column(self, issue_number: int, column: str) -> None:
        raise NotImplementedError("Projects board updates are wired in S2 (label→column machine)")

    def get_board_column(self, issue_number: int) -> str | None:
        raise NotImplementedError("Projects board reads are wired in S2 (label→column machine)")
