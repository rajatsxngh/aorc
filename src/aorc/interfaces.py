"""The two abstraction seams every AORC slice depends on.

Architecture invariant #1: orchestrator logic imports *only* from this module
(the `LLMClient` / `GitHubClient` interfaces and their plain data types), never
a provider SDK or the GitHub SDK. Concrete adapters live behind these ABCs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# LLM seam
# --------------------------------------------------------------------------- #


@dataclass
class Message:
    """A single chat turn, provider-agnostic."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class Completion:
    """The result of an `LLMClient.complete` call, provider-agnostic."""

    text: str
    model: str
    finish_reason: str | None = None
    raw: Any = None  # underlying provider response, for debugging only


class LLMClient(ABC):
    """One contract for every provider (Claude, OpenAI, any OpenAI-compatible
    endpoint incl. a local ``base_url``). Model selection is injected from
    config — the interface never names a model."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The model id this client was configured with (from `.aorc.yml`)."""

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        """Produce a completion for the given conversation."""


# --------------------------------------------------------------------------- #
# GitHub seam
# --------------------------------------------------------------------------- #


@dataclass
class Issue:
    number: int
    title: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)
    state: str = "open"  # "open" | "closed"
    author: str = ""
    author_association: str = "NONE"


@dataclass
class Comment:
    id: int
    body: str
    author: str = ""
    author_association: str = "NONE"


@dataclass
class PullRequest:
    number: int
    title: str = ""
    body: str = ""
    head: str = ""
    base: str = "main"
    state: str = "open"  # "open" | "closed"
    merged: bool = False


class GitHubClient(ABC):
    """One contract for all GitHub operations: issues, comments, labels, pull
    requests, the Projects board, and merge. Everything the orchestrator needs
    to reconstruct pipeline state from GitHub alone goes through here."""

    # ---- issues ---------------------------------------------------------- #
    @abstractmethod
    def get_issue(self, number: int) -> Issue: ...

    @abstractmethod
    def list_issues(self, state: str = "open") -> list[Issue]: ...

    @abstractmethod
    def close_issue(self, number: int) -> None: ...

    # ---- comments -------------------------------------------------------- #
    @abstractmethod
    def post_comment(self, issue_number: int, body: str) -> Comment: ...

    @abstractmethod
    def list_comments(self, issue_number: int) -> list[Comment]: ...

    # ---- labels ---------------------------------------------------------- #
    @abstractmethod
    def get_labels(self, issue_number: int) -> list[str]: ...

    @abstractmethod
    def add_label(self, issue_number: int, label: str) -> None: ...

    @abstractmethod
    def remove_label(self, issue_number: int, label: str) -> None: ...

    @abstractmethod
    def set_labels(self, issue_number: int, labels: list[str]) -> None: ...

    @abstractmethod
    def create_label(self, name: str, color: str = "ededed", description: str = "") -> None: ...

    # ---- pull requests --------------------------------------------------- #
    @abstractmethod
    def open_pull_request(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PullRequest: ...

    @abstractmethod
    def get_pull_request(self, number: int) -> PullRequest: ...

    @abstractmethod
    def list_pull_requests(self, state: str = "open") -> list[PullRequest]: ...

    @abstractmethod
    def merge_pull_request(self, number: int, method: str = "merge") -> None: ...

    @abstractmethod
    def delete_branch(self, branch: str) -> None:
        """Delete a branch on the remote. Used by the three fixed
        branch-cleanup cases (S4): merged -> delete, everything else keeps
        the branch."""

    # ---- repo contents ----------------------------------------------------- #
    # Used to check whether a stage's artifact (design doc, test file, ...) is
    # actually committed to a branch -- the label alone is never sufficient.
    @abstractmethod
    def get_file(self, path: str, ref: str) -> str | None:
        """Return the file's content at `path` on `ref`, or `None` if absent."""

    @abstractmethod
    def commit_file(self, branch: str, path: str, content: str, message: str) -> None:
        """Create or update `path` on `branch` with `content`. How a stage
        (e.g. S5's design doc) persists its artifact as the resumable record
        `get_file` later checks for."""

    # ---- projects board -------------------------------------------------- #
    # The column is *derived* from the label by the state machine (S2); the
    # client only exposes the raw set/get so agents never compute it inline.
    @abstractmethod
    def set_board_column(self, issue_number: int, column: str) -> None: ...

    @abstractmethod
    def get_board_column(self, issue_number: int) -> str | None: ...
