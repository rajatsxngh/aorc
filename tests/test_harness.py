"""S4 -- Container harness + checkpoint spine."""

from __future__ import annotations

import subprocess

import pytest

from aorc.github.mock import MockGitHubClient
from aorc.harness import (
    Checkpoint,
    CheckpointReport,
    ContainerHarness,
    MockContainerRuntime,
    WorktreeManager,
    cleanup_branch,
)
from aorc.interfaces import Issue
from aorc.pipeline import branch_name


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@a.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_worktree_created_on_first_dispatch(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))

    path = worktrees.ensure(42)

    assert path.endswith("issue-42")
    result = subprocess.run(
        ["git", "branch", "--list", branch_name(42)], cwd=repo, capture_output=True, text=True
    )
    assert branch_name(42) in result.stdout


def test_worktree_reused_on_redispatch(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))

    first = worktrees.ensure(7)
    second = worktrees.ensure(7)

    assert first == second
    # only one worktree entry was ever created for issue 7
    result = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
    )
    entries = [line for line in result.stdout.splitlines() if line.startswith(first)]
    assert len(entries) == 1


def test_dispatch_creates_exactly_one_container_for_the_issue(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    runtime = MockContainerRuntime()
    gh = MockGitHubClient(issues=[Issue(number=5)])
    harness = ContainerHarness(runtime, worktrees, gh)

    handle = harness.dispatch(5)

    assert handle.issue_number == 5
    assert handle.branch == branch_name(5)
    assert handle.status == "running"
    assert [c for c in runtime.calls if c[0] == "start"] == [("start", 5, branch_name(5))]


def test_checkpoint_trivially_proceeds_regardless_of_files():
    checkpoint = Checkpoint()
    report = CheckpointReport(issue_number=1, files=["src/aorc/foo.py", "tests/test_foo.py"])

    assert checkpoint.verdict(report) == "proceed"


def test_harness_checkpoint_delegates_to_checkpoint_verdict(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    harness = ContainerHarness(MockContainerRuntime(), worktrees, MockGitHubClient())

    verdict = harness.checkpoint(CheckpointReport(issue_number=5, files=["a.py"]))

    assert verdict == "proceed"


@pytest.mark.parametrize("outcome", ["merged", "agent-blocked", "held"])
def test_teardown_always_stops_the_container(tmp_path, outcome):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    runtime = MockContainerRuntime()
    gh = MockGitHubClient(issues=[Issue(number=5)])
    harness = ContainerHarness(runtime, worktrees, gh)
    handle = harness.dispatch(5)

    harness.teardown(handle, outcome=outcome)

    assert handle.status == "stopped"
    assert ("teardown", 5) in runtime.calls


def test_branch_cleanup_merged_deletes_branch():
    gh = MockGitHubClient(issues=[Issue(number=1)])

    cleanup_branch(gh, 1, "merged")

    assert ("delete_branch", branch_name(1)) in gh.calls


@pytest.mark.parametrize("outcome", ["agent-blocked", "held"])
def test_branch_cleanup_keeps_branch_for_blocked_or_held(outcome):
    gh = MockGitHubClient(issues=[Issue(number=1)])

    cleanup_branch(gh, 1, outcome)

    assert not any(c[0] == "delete_branch" for c in gh.calls)


def test_branch_cleanup_rejects_unknown_outcome():
    gh = MockGitHubClient(issues=[Issue(number=1)])

    with pytest.raises(ValueError):
        cleanup_branch(gh, 1, "bogus")
