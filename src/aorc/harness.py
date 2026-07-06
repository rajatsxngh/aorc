"""S4 -- Container harness + checkpoint spine.

The per-issue execution harness that S5-S8 run inside: one actionable issue,
one isolated container, one git worktree. Nothing carries across issues.

Worktree/branch creation shells out to real `git` -- git is the substrate
itself, not a provider SDK, so this doesn't cross architecture invariant #1
(which forbids depending on provider/GitHub SDKs directly). Actual container
dispatch (Docker/Actions) sits behind the `ContainerRuntime` seam, mirroring
the `GitHubClient`/`LLMClient` pattern, so harness orchestration logic is
testable without a real Docker daemon or Actions runner.

The checkpoint verdict is trivially "proceed" in this slice -- real
collision logic against other in-flight issues and open PRs arrives in S10.
"""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .interfaces import GitHubClient
from .pipeline import branch_name

# The three fixed branch-cleanup outcomes -- no agent judgment involved.
_CLEANUP_OUTCOMES = {"merged", "agent-blocked", "held"}


@dataclass
class ContainerHandle:
    issue_number: int
    branch: str
    worktree_path: str
    container_id: str
    status: str = "running"  # "running" | "stopped"


@dataclass
class CheckpointReport:
    """What a container reports at the post-Design checkpoint before it is
    allowed to proceed to Tester/Coder/Reviewer."""

    issue_number: int
    files: list[str] = field(default_factory=list)


class ContainerRuntime(ABC):
    """Dispatch/teardown of the per-issue build container."""

    @abstractmethod
    def start(self, issue_number: int, branch: str, worktree_path: str) -> ContainerHandle: ...

    @abstractmethod
    def teardown(self, handle: ContainerHandle) -> None: ...


class MockContainerRuntime(ContainerRuntime):
    """In-memory `ContainerRuntime` for unit tests; records every call like
    `MockGitHubClient` does."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._next_id = 1

    def start(self, issue_number: int, branch: str, worktree_path: str) -> ContainerHandle:
        handle = ContainerHandle(
            issue_number=issue_number,
            branch=branch,
            worktree_path=worktree_path,
            container_id=f"mock-container-{self._next_id}",
        )
        self._next_id += 1
        self.calls.append(("start", issue_number, branch))
        return handle

    def teardown(self, handle: ContainerHandle) -> None:
        handle.status = "stopped"
        self.calls.append(("teardown", handle.issue_number))


class DockerContainerRuntime(ContainerRuntime):
    """Real per-issue container dispatch via the Docker CLI, from the
    pre-baked base image (Claude Code + skills + MCPs installed). Exercised
    by integration tests against a real Docker daemon, not the unit suite
    (which uses `MockContainerRuntime`)."""

    def __init__(self, base_image: str) -> None:
        self._base_image = base_image

    def start(self, issue_number: int, branch: str, worktree_path: str) -> ContainerHandle:
        name = f"aorc-issue-{issue_number}"
        subprocess.run(
            [
                "docker", "run", "-d", "--name", name,
                "-v", f"{worktree_path}:/workspace",
                "-w", "/workspace",
                self._base_image,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return ContainerHandle(
            issue_number=issue_number,
            branch=branch,
            worktree_path=worktree_path,
            container_id=name,
        )

    def teardown(self, handle: ContainerHandle) -> None:
        subprocess.run(["docker", "rm", "-f", handle.container_id], check=True, capture_output=True, text=True)
        handle.status = "stopped"


class WorktreeManager:
    """One git worktree per issue, on branch `aorc/issue-<n>`, created once
    and reused across re-dispatches."""

    def __init__(self, repo_dir: str, worktrees_dir: str) -> None:
        self._repo_dir = repo_dir
        self._worktrees_dir = worktrees_dir

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self._repo_dir, capture_output=True, text=True, check=True
        )

    def path_for(self, issue_number: int) -> str:
        return os.path.join(self._worktrees_dir, f"issue-{issue_number}")

    def ensure(self, issue_number: int) -> str:
        """Create the branch + worktree if this is the first dispatch;
        reuse the existing worktree on any re-dispatch."""
        path = self.path_for(issue_number)
        if os.path.isdir(path):
            return path
        branch = branch_name(issue_number)
        existing = self._git("branch", "--list", branch).stdout
        if branch in existing:
            self._git("worktree", "add", path, branch)
        else:
            self._git("worktree", "add", "-b", branch, path)
        return path


class Checkpoint:
    """Post-Design checkpoint: the container reports its exact file list and
    waits for a verdict before continuing. Trivially "proceed" here -- only
    one issue is ever in flight; real collision detection arrives in S10."""

    def verdict(self, report: CheckpointReport) -> str:
        return "proceed"


def cleanup_branch(github: GitHubClient, issue_number: int, outcome: str) -> None:
    """Three fixed cases, no agent judgment: merged -> delete the branch;
    agent-blocked / held -> keep it (resumable from the checkpoint)."""
    if outcome not in _CLEANUP_OUTCOMES:
        raise ValueError(f"unknown branch-cleanup outcome: {outcome!r}")
    if outcome == "merged":
        github.delete_branch(branch_name(issue_number))


class ContainerHarness:
    """Ties worktree creation, container dispatch, the checkpoint, and
    teardown+branch-cleanup together. One container per issue; nothing
    carries across issues."""

    def __init__(
        self, runtime: ContainerRuntime, worktrees: WorktreeManager, github: GitHubClient
    ) -> None:
        self._runtime = runtime
        self._worktrees = worktrees
        self._github = github
        self._checkpoint = Checkpoint()

    def dispatch(self, issue_number: int) -> ContainerHandle:
        branch = branch_name(issue_number)
        path = self._worktrees.ensure(issue_number)
        return self._runtime.start(issue_number, branch, path)

    def checkpoint(self, report: CheckpointReport) -> str:
        return self._checkpoint.verdict(report)

    def teardown(self, handle: ContainerHandle, outcome: str) -> None:
        self._runtime.teardown(handle)
        cleanup_branch(self._github, handle.issue_number, outcome)
