"""S4 -- Container harness + checkpoint spine. S20 -- Checkpoint injection
+ in-flight file-claim registry.

The per-issue execution harness that S5-S8 run inside: one actionable issue,
one isolated container, one git worktree. Nothing carries across issues.

Worktree/branch creation shells out to real `git` -- git is the substrate
itself, not a provider SDK, so this doesn't cross architecture invariant #1
(which forbids depending on provider/GitHub SDKs directly). Actual container
dispatch (Docker/Actions) sits behind the `ContainerRuntime` seam, mirroring
the `GitHubClient`/`LLMClient` pattern, so harness orchestration logic is
testable without a real Docker daemon or Actions runner.

`Checkpoint` is injected into `ContainerHarness` (default: a trivial one
wired to the harness's own `GitHubClient`), and carries the collaborators
real collision detection needs -- the `GitHubClient` (open PRs) and an
`InFlightRegistry` of other in-flight issues' claimed file lists, recorded
per-issue at the checkpoint and cleared on teardown. The verdict itself is
still trivially "proceed" in this slice -- S10 fills in the real collision
rule using these collaborators.
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


class InFlightRegistry:
    """Tracks each dispatched issue's claimed file list as reported at its
    checkpoint, so collision logic can ask "what files are claimed by
    in-flight issues other than mine". An issue's claim is cleared on
    teardown -- nothing outlives the issue it belongs to."""

    def __init__(self) -> None:
        self._claims: dict[int, list[str]] = {}

    def record(self, issue_number: int, files: list[str]) -> None:
        self._claims[issue_number] = list(files)

    def claimed_by_others(self, issue_number: int) -> dict[int, list[str]]:
        return {n: files for n, files in self._claims.items() if n != issue_number}

    def clear(self, issue_number: int) -> None:
        self._claims.pop(issue_number, None)


class Checkpoint:
    """Post-Design checkpoint: the container reports its exact file list and
    waits for a verdict before continuing. Takes the collaborators real
    collision detection needs -- the `GitHubClient` (open PRs) and the
    in-flight file-claim registry -- but the verdict stays trivially
    "proceed" here; S10 fills in the real collision rule."""

    def __init__(
        self, github: GitHubClient | None = None, registry: InFlightRegistry | None = None
    ) -> None:
        self.github = github
        self.registry = registry if registry is not None else InFlightRegistry()

    def verdict(self, report: CheckpointReport) -> str:
        self.registry.record(report.issue_number, report.files)
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
        self,
        runtime: ContainerRuntime,
        worktrees: WorktreeManager,
        github: GitHubClient,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self._runtime = runtime
        self._worktrees = worktrees
        self._github = github
        self._checkpoint = checkpoint if checkpoint is not None else Checkpoint(github=github)

    def dispatch(self, issue_number: int) -> ContainerHandle:
        branch = branch_name(issue_number)
        path = self._worktrees.ensure(issue_number)
        return self._runtime.start(issue_number, branch, path)

    def checkpoint(self, report: CheckpointReport) -> str:
        return self._checkpoint.verdict(report)

    def teardown(self, handle: ContainerHandle, outcome: str) -> None:
        self._runtime.teardown(handle)
        self._checkpoint.registry.clear(handle.issue_number)
        cleanup_branch(self._github, handle.issue_number, outcome)
