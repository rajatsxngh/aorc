"""In-memory `GitHubClient` for unit tests.

Holds issue/comment/label/PR/board state and records every mutating call in
`self.calls` so tests read as specifications: given this orchestrator action,
expect these GitHub API calls and this label state.
"""

from __future__ import annotations

from ..interfaces import Comment, GitHubClient, Issue, PullRequest


class MockGitHubClient(GitHubClient):
    def __init__(
        self,
        issues: list[Issue] | None = None,
        pulls: list[PullRequest] | None = None,
    ) -> None:
        self.issues: dict[int, Issue] = {i.number: i for i in (issues or [])}
        self.pulls: dict[int, PullRequest] = {p.number: p for p in (pulls or [])}
        self.comments: dict[int, list[Comment]] = {}
        self.board: dict[int, str] = {}
        self.created_labels: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self._next_comment_id = 1
        self._next_pr_number = max([p.number for p in self.pulls.values()], default=1000)

    # ---- issues ---------------------------------------------------------- #
    def get_issue(self, number: int) -> Issue:
        return self.issues[number]

    def list_issues(self, state: str = "open") -> list[Issue]:
        return [i for i in self.issues.values() if state == "all" or i.state == state]

    def close_issue(self, number: int) -> None:
        self.calls.append(("close_issue", number))
        self.issues[number].state = "closed"

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

    def merge_pull_request(self, number: int, method: str = "merge") -> None:
        self.calls.append(("merge_pull_request", number, method))
        pr = self.pulls[number]
        pr.merged = True
        pr.state = "closed"

    # ---- projects board -------------------------------------------------- #
    def set_board_column(self, issue_number: int, column: str) -> None:
        self.calls.append(("set_board_column", issue_number, column))
        self.board[issue_number] = column

    def get_board_column(self, issue_number: int) -> str | None:
        return self.board.get(issue_number)
