"""S4 -- Container harness + checkpoint spine."""

from __future__ import annotations

import subprocess

import pytest

from aorc.github.mock import MockGitHubClient
from aorc.harness import (
    Checkpoint,
    CheckpointReport,
    ContainerHarness,
    InFlightRegistry,
    MockContainerRuntime,
    WorktreeManager,
    cleanup_branch,
)
from aorc.interfaces import Issue, PullRequest
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


# ---- S20: injected Checkpoint + in-flight file-claim registry ------------- #


def test_checkpoint_defaults_to_empty_registry_and_no_github():
    checkpoint = Checkpoint()

    assert isinstance(checkpoint.registry, InFlightRegistry)
    assert checkpoint.github is None


def test_checkpoint_accepts_github_and_registry_collaborators():
    gh = MockGitHubClient(pulls=[PullRequest(number=1, head="aorc/issue-9")])
    registry = InFlightRegistry()

    checkpoint = Checkpoint(github=gh, registry=registry)

    assert checkpoint.github is gh
    assert checkpoint.registry is registry


def test_checkpoint_verdict_still_trivially_proceeds_with_collaborators():
    gh = MockGitHubClient()
    checkpoint = Checkpoint(github=gh, registry=InFlightRegistry())
    report = CheckpointReport(issue_number=1, files=["src/aorc/foo.py"])

    assert checkpoint.verdict(report) == "proceed"


def test_checkpoint_verdict_records_files_in_registry():
    checkpoint = Checkpoint()

    checkpoint.verdict(CheckpointReport(issue_number=1, files=["a.py", "b.py"]))

    assert checkpoint.registry.claimed_by_others(2) == {1: ["a.py", "b.py"]}


def test_registry_claimed_by_others_excludes_own_issue():
    registry = InFlightRegistry()
    registry.record(1, ["a.py"])
    registry.record(2, ["b.py"])

    assert registry.claimed_by_others(1) == {2: ["b.py"]}
    assert registry.claimed_by_others(2) == {1: ["a.py"]}


def test_registry_clear_removes_only_that_issues_claim():
    registry = InFlightRegistry()
    registry.record(1, ["a.py"])
    registry.record(2, ["b.py"])

    registry.clear(1)

    assert registry.claimed_by_others(2) == {}
    assert registry.claimed_by_others(1) == {2: ["b.py"]}


def test_harness_default_checkpoint_reproduces_trivial_proceed_behaviour(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    harness = ContainerHarness(MockContainerRuntime(), worktrees, MockGitHubClient())

    verdict = harness.checkpoint(CheckpointReport(issue_number=5, files=["a.py"]))

    assert verdict == "proceed"


def test_harness_accepts_injected_checkpoint(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    gh = MockGitHubClient()
    registry = InFlightRegistry()
    checkpoint = Checkpoint(github=gh, registry=registry)
    harness = ContainerHarness(MockContainerRuntime(), worktrees, gh, checkpoint=checkpoint)

    harness.checkpoint(CheckpointReport(issue_number=5, files=["a.py"]))

    assert registry.claimed_by_others(999) == {5: ["a.py"]}


def test_harness_teardown_clears_the_issues_checkpoint_claim(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    runtime = MockContainerRuntime()
    gh = MockGitHubClient(issues=[Issue(number=5)])
    registry = InFlightRegistry()
    checkpoint = Checkpoint(github=gh, registry=registry)
    harness = ContainerHarness(runtime, worktrees, gh, checkpoint=checkpoint)
    handle = harness.dispatch(5)
    harness.checkpoint(CheckpointReport(issue_number=5, files=["a.py"]))

    harness.teardown(handle, outcome="held")

    assert registry.claimed_by_others(999) == {}
