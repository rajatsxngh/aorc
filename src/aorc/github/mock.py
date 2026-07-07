"""In-memory `GitHubClient` for unit tests.

Holds issue/comment/label/PR/board state and records every mutating call in
`self.calls` so tests read as specifications: given this orchestrator action,
expect these GitHub API calls and this label state.
"""

from __future__ import annotations

from ..interfaces import Comment, GitHubClient, Issue, PullRequest

# Sentinel: by default the mock models a *configured* board (records columns),
# matching its role as the idealized in-memory reference. Pass `project=None`
# to model the real adapter's unconfigured no-op board instead.
_CONFIGURED = object()


class MockGitHubClient(GitHubClient):
    def __init__(
        self,
        issues: list[Issue] | None = None,
        pulls: list[PullRequest] | None = None,
        *,
        project: object = _CONFIGURED,
    ) -> None:
        # None => unconfigured board (board ops no-op, mirroring SdkGitHubClient);
        # anything else => configured board that records columns.
        self._project = None if project is None else project
        self.issues: dict[int, Issue] = {i.number: i for i in (issues or [])}
        self.pulls: dict[int, PullRequest] = {p.number: p for p in (pulls or [])}
        self.comments: dict[int, list[Comment]] = {}
        self.board: dict[int, str] = {}
        self.board_columns: list[str] | None = None
        self.created_labels: dict[str, dict] = {}
        self.files: dict[tuple[str, str], str] = {}  # (ref, path) -> content
        self.calls: list[tuple] = []
        self._next_comment_id = 1
        self._next_pr_number = max([p.number for p in self.pulls.values()], default=1000)
        self._next_issue_number = max([i.number for i in self.issues.values()], default=0) + 1

    # ---- issues ---------------------------------------------------------- #
    def get_issue(self, number: int) -> Issue:
        return self.issues[number]

    def list_issues(self, state: str = "open") -> list[Issue]:
        return [i for i in self.issues.values() if state == "all" or i.state == state]

    def close_issue(self, number: int) -> None:
        self.calls.append(("close_issue", number))
        self.issues[number].state = "closed"

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> Issue:
        issue = Issue(number=self._next_issue_number, title=title, body=body, labels=list(labels or []))
        self.issues[issue.number] = issue
        self._next_issue_number += 1
        self.calls.append(("create_issue", issue.number, title))
        return issue

    # ---- comments -------------------------------------------------------- #
    def post_comment(self, issue_number: int, body: str) -> Comment:
        self.calls.append(("post_comment", issue_number, body))
        comment = Comment(id=self._next_comment_id, body=body, author="aorc[bot]")
        self._next_comment_id += 1
        self.comments.setdefault(issue_number, []).append(comment)
        return comment

    def list_comments(self, issue_number: int) -> list[Comment]:
        return list(self.comments.get(issue_number, []))

    # ---- labels ---------------------------------------------------------- #
    def get_labels(self, issue_number: int) -> list[str]:
        return list(self.issues[issue_number].labels)

    def add_label(self, issue_number: int, label: str) -> None:
        self.calls.append(("add_label", issue_number, label))
        labels = self.issues[issue_number].labels
        if label not in labels:
            labels.append(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.calls.append(("remove_label", issue_number, label))
        labels = self.issues[issue_number].labels
        if label in labels:
            labels.remove(label)

    def set_labels(self, issue_number: int, labels: list[str]) -> None:
        self.calls.append(("set_labels", issue_number, list(labels)))
        self.issues[issue_number].labels = list(labels)

    def create_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        self.calls.append(("create_label", name, color, description))
        self.created_labels[name] = {"color": color, "description": description}

    # ---- pull requests --------------------------------------------------- #
    def open_pull_request(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PullRequest:
        self._next_pr_number += 1
        pr = PullRequest(
            number=self._next_pr_number, title=title, body=body, head=head, base=base
        )
        self.pulls[pr.number] = pr
        self.calls.append(("open_pull_request", pr.number, head, base))
        return pr

    def get_pull_request(self, number: int) -> PullRequest:
        return self.pulls[number]

    def list_pull_requests(self, state: str = "open") -> list[PullRequest]:
        return [p for p in self.pulls.values() if state == "all" or p.state == state]

    def merge_pull_request(self, number: int, method: str = "merge") -> None:
        self.calls.append(("merge_pull_request", number, method))
        pr = self.pulls[number]
        pr.merged = True
        pr.state = "closed"

    def delete_branch(self, branch: str) -> None:
        self.calls.append(("delete_branch", branch))

    # ---- repo contents ----------------------------------------------------- #
    def get_file(self, path: str, ref: str) -> str | None:
        return self.files.get((ref, path))

    def commit_file(self, branch: str, path: str, content: str, message: str) -> None:
        self.calls.append(("commit_file", branch, path, message))
        self.files[(branch, path)] = content

    def add_file(self, ref: str, path: str, content: str) -> None:
        """Test helper: simulate a file committed to `ref` at `path`."""
        self.files[(ref, path)] = content

    # ---- projects board -------------------------------------------------- #
    def create_board(self, columns: list[str]) -> None:
        self.calls.append(("create_board", list(columns)))
        self.board_columns = list(columns)
        self._project = _CONFIGURED  # an install-created board is configured

    def set_board_column(self, issue_number: int, column: str) -> None:
        if self._project is None:
            return  # unconfigured board: no-op, mirroring SdkGitHubClient
        self.calls.append(("set_board_column", issue_number, column))
        self.board[issue_number] = column

    def get_board_column(self, issue_number: int) -> str | None:
        if self._project is None:
            return None
        return self.board.get(issue_number)
