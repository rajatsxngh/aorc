"""S4 -- Container harness + checkpoint spine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aorc.github.mock import MockGitHubClient
from aorc.graphify import MockGraphifyClient
from aorc.guards import ComputeGuard
from aorc.harness import (
    Checkpoint,
    CheckpointReport,
    ContainerHarness,
    InFlightRegistry,
    MockContainerRuntime,
    WorktreeManager,
    cleanup_branch,
    container_name_for,
    issue_number_from_worktree_path,
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


def test_enforce_wall_clock_continues_under_limit(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    runtime = MockContainerRuntime()
    gh = MockGitHubClient(issues=[Issue(number=5)])
    harness = ContainerHarness(
        runtime, worktrees, gh, compute_guard=ComputeGuard(wall_clock_minutes=30.0)
    )
    handle = harness.dispatch(5)

    verdict = harness.enforce_wall_clock(handle, elapsed_minutes=10.0)

    assert verdict.action == "continue"
    assert handle.status == "running"
    assert gh.issues[5].labels == []


def test_enforce_wall_clock_kills_labels_and_preserves_branch(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    runtime = MockContainerRuntime()
    gh = MockGitHubClient(issues=[Issue(number=5)])
    registry = InFlightRegistry()
    checkpoint = Checkpoint(github=gh, registry=registry)
    harness = ContainerHarness(
        runtime,
        worktrees,
        gh,
        checkpoint=checkpoint,
        compute_guard=ComputeGuard(wall_clock_minutes=30.0),
    )
    handle = harness.dispatch(5)
    harness.checkpoint(CheckpointReport(issue_number=5, files=["a.py"]))

    verdict = harness.enforce_wall_clock(handle, elapsed_minutes=30.0)

    assert verdict.action == "kill"
    assert handle.status == "stopped"
    assert "agent-blocked" in gh.issues[5].labels
    assert any("wall-clock" in c.body.lower() for c in gh.list_comments(5))
    # branch preserved (kept, like any other agent-blocked outcome)
    assert ("delete_branch", branch_name(5)) not in gh.calls
    # checkpoint claim cleared, same as a normal teardown
    assert registry.claimed_by_others(999) == {}


# ---- S10: real collision verdict ------------------------------------------ #


def test_verdict_holds_on_exact_file_intersection_with_another_in_flight_issue():
    registry = InFlightRegistry()
    registry.record(1, ["src/aorc/foo.py"])
    checkpoint = Checkpoint(registry=registry)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/foo.py"]))

    assert verdict == "hold"


def test_verdict_proceeds_when_file_sets_are_disjoint_and_no_blast_radius_link():
    registry = InFlightRegistry()
    registry.record(1, ["src/aorc/foo.py"])
    graphify = MockGraphifyClient(edges={})
    checkpoint = Checkpoint(registry=registry, graphify=graphify)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/bar.py"]))

    assert verdict == "proceed"


def test_verdict_holds_on_forward_blast_radius_overlap():
    # issue 2's file is depended on by src/aorc/foo.py, which issue 1 claims.
    registry = InFlightRegistry()
    registry.record(1, ["src/aorc/foo.py"])
    graphify = MockGraphifyClient(edges={"src/aorc/bar.py": {"src/aorc/foo.py"}})
    checkpoint = Checkpoint(registry=registry, graphify=graphify)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/bar.py"]))

    assert verdict == "hold"


def test_verdict_holds_on_backward_blast_radius_overlap():
    # issue 1's claimed file is depended on by issue 2's reported file.
    registry = InFlightRegistry()
    registry.record(1, ["src/aorc/foo.py"])
    graphify = MockGraphifyClient(edges={"src/aorc/foo.py": {"src/aorc/bar.py"}})
    checkpoint = Checkpoint(registry=registry, graphify=graphify)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/bar.py"]))

    assert verdict == "hold"


def test_verdict_holds_conservatively_on_graphify_query_failure():
    registry = InFlightRegistry()
    registry.record(1, ["src/aorc/foo.py"])
    graphify = MockGraphifyClient(edges={})
    graphify.fail_next = True
    checkpoint = Checkpoint(registry=registry, graphify=graphify)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/bar.py"]))

    assert verdict == "hold"


def test_verdict_includes_open_unmerged_prs_in_collision_set():
    gh = MockGitHubClient(
        pulls=[PullRequest(number=1, head="aorc/issue-9", files=["src/aorc/foo.py"])]
    )
    checkpoint = Checkpoint(github=gh)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/foo.py"]))

    assert verdict == "hold"


def test_verdict_ignores_own_open_pr_and_closed_prs():
    gh = MockGitHubClient(
        pulls=[
            PullRequest(number=1, head="aorc/issue-2", files=["src/aorc/foo.py"]),
            PullRequest(number=2, head="aorc/issue-3", state="closed", files=["src/aorc/bar.py"]),
        ]
    )
    checkpoint = Checkpoint(github=gh)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/foo.py"]))

    assert verdict == "proceed"


def test_verdict_proceeds_with_no_graphify_and_disjoint_files_path_intersect_only():
    registry = InFlightRegistry()
    registry.record(1, ["src/aorc/foo.py"])
    checkpoint = Checkpoint(registry=registry)  # no graphify configured

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=["src/aorc/bar.py"]))

    assert verdict == "proceed"


def test_shared_registry_across_two_harnesses_makes_cross_issue_collision_actually_fire(tmp_path):
    """The AC this locks in: a *private* per-harness default registry would
    let two concurrent issues silently both get "proceed" on the same file --
    collision detection would never fire across harnesses. Sharing one
    Checkpoint (and thus one InFlightRegistry) between two ContainerHarness
    instances is what makes cross-issue collision real."""
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    gh = MockGitHubClient(issues=[Issue(number=1), Issue(number=2)])
    shared_checkpoint = Checkpoint(github=gh, registry=InFlightRegistry())

    harness_a = ContainerHarness(MockContainerRuntime(), worktrees, gh, checkpoint=shared_checkpoint)
    harness_b = ContainerHarness(MockContainerRuntime(), worktrees, gh, checkpoint=shared_checkpoint)

    first = harness_a.checkpoint(CheckpointReport(issue_number=1, files=["src/aorc/foo.py"]))
    second = harness_b.checkpoint(CheckpointReport(issue_number=2, files=["src/aorc/foo.py"]))

    assert first == "proceed"
    assert second == "hold"


def test_private_default_registries_would_miss_the_collision_two_unshared_harnesses(tmp_path):
    """Contrast case: two harnesses each falling back to their own default
    Checkpoint/registry never see each other's claims -- the exact failure
    mode the shared-registry requirement guards against."""
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    gh = MockGitHubClient(issues=[Issue(number=1), Issue(number=2)])

    harness_a = ContainerHarness(MockContainerRuntime(), worktrees, gh)
    harness_b = ContainerHarness(MockContainerRuntime(), worktrees, gh)

    first = harness_a.checkpoint(CheckpointReport(issue_number=1, files=["src/aorc/foo.py"]))
    second = harness_b.checkpoint(CheckpointReport(issue_number=2, files=["src/aorc/foo.py"]))

    assert first == "proceed"
    assert second == "proceed"  # documents the gap: no PR yet, private registries


@pytest.mark.parametrize(
    "reported",
    ["./src/aorc/foo.py", "src//aorc/foo.py", "/src/aorc/foo.py", "src/aorc/./foo.py"],
)
def test_verdict_holds_when_same_file_is_written_differently(reported):
    """The same file spelled two ways is still the same file -- a path-format
    mismatch between sources (container report vs registry vs PR list) must
    not silently defeat the collision check."""
    registry = InFlightRegistry()
    registry.record(1, ["src/aorc/foo.py"])
    checkpoint = Checkpoint(registry=registry)

    verdict = checkpoint.verdict(CheckpointReport(issue_number=2, files=[reported]))

    assert verdict == "hold"


def test_harness_threads_graphify_into_default_checkpoint(tmp_path):
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    gh = MockGitHubClient(issues=[Issue(number=1), Issue(number=2)])
    graphify = MockGraphifyClient(edges={"src/aorc/bar.py": {"src/aorc/foo.py"}})
    harness = ContainerHarness(MockContainerRuntime(), worktrees, gh, graphify=graphify)

    first = harness.checkpoint(CheckpointReport(issue_number=1, files=["src/aorc/foo.py"]))
    second = harness.checkpoint(CheckpointReport(issue_number=2, files=["src/aorc/bar.py"]))

    assert first == "proceed"
    assert second == "hold"


# --- S19: docker secrets must never appear in host-visible argv ------------- #
# `docker run -e KEY=value` puts the value in the host process table (`ps`);
# the runtime must deliver env through a 0600 --env-file that is removed as
# soon as `docker run` returns (S15/S16 known limitation, closed in S19).


class _FakeDockerRun:
    """Stands in for subprocess.run: captures argv and snapshots the
    --env-file (contents + mode) *at call time*, since the file must be
    gone by the time `start` returns."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.argv: list[str] | None = None
        self.env_file_path: str | None = None
        self.env_file_contents: str | None = None
        self.env_file_mode: int | None = None

    def __call__(self, argv, **kwargs):
        import os as _os
        import stat as _stat

        self.argv = list(argv)
        if "--env-file" in self.argv:
            path = self.argv[self.argv.index("--env-file") + 1]
            self.env_file_path = path
            with open(path) as fh:
                self.env_file_contents = fh.read()
            self.env_file_mode = _stat.S_IMODE(_os.stat(path).st_mode)
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_docker_start_keeps_secrets_out_of_argv(monkeypatch):
    import os

    from aorc.harness import DockerContainerRuntime

    fake = _FakeDockerRun()
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)
    runtime = DockerContainerRuntime("base:img")

    handle = runtime.start(
        7, "aorc/issue-7", "/tmp/wt", env={"GITHUB_TOKEN": "ghs_secret123"}
    )

    assert "ghs_secret123" not in " ".join(fake.argv)
    assert "-e" not in fake.argv
    assert "GITHUB_TOKEN=ghs_secret123\n" in fake.env_file_contents
    assert fake.env_file_mode == 0o600
    assert not os.path.exists(fake.env_file_path)  # deleted once run returned
    assert handle.container_id == "aorc-issue-7"


def test_docker_start_env_file_removed_even_when_docker_fails(monkeypatch):
    import os

    from aorc.harness import DockerContainerRuntime

    fake = _FakeDockerRun(fail=True)
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)
    runtime = DockerContainerRuntime("base:img")

    with pytest.raises(subprocess.CalledProcessError):
        runtime.start(7, "aorc/issue-7", "/tmp/wt", env={"KEY": "hunter2"})

    assert fake.env_file_path is not None
    assert not os.path.exists(fake.env_file_path)


def test_docker_start_mounts_worktree_at_absolute_path(monkeypatch):
    """S30: Docker's `-v` requires an absolute host source; a relative
    worktree path (what `WorktreeManager` yields under the default relative
    `.aorc-worktrees`) must be resolved before it reaches argv, or the real
    `docker run` fails with exit status 125."""
    import os

    from aorc.harness import DockerContainerRuntime

    fake = _FakeDockerRun()
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)
    runtime = DockerContainerRuntime("base:img")

    runtime.start(7, "aorc/issue-7", ".aorc-worktrees/issue-7", env=None)

    mount = fake.argv[fake.argv.index("-v") + 1]
    source, destination = mount.rsplit(":", 1)
    assert destination == "/workspace"
    assert os.path.isabs(source)
    assert source == os.path.abspath(".aorc-worktrees/issue-7")


# --- S35: worktrees must come from a clone of the TARGET repo --------------- #
# Live bug: compose() defaulted repo_dir="." (the orchestrator's own cwd), so
# per-issue worktrees were checkouts of AORC itself and the container ran
# pytest against AORC's own test suite instead of the sandbox project's.


def _git_out(args, cwd):
    import subprocess as sp

    return sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo_with_origin(path, url):
    path.mkdir(parents=True, exist_ok=True)
    _git_out(["init", "-q"], cwd=path)
    _git_out(["remote", "add", "origin", url], cwd=path)
    return path


def test_origin_repo_slug_parses_common_remote_url_forms(tmp_path):
    from aorc.harness import origin_repo_slug

    https = _repo_with_origin(tmp_path / "a", "https://github.com/acme/widget.git")
    ssh = _repo_with_origin(tmp_path / "b", "git@github.com:acme/widget.git")
    bare_https = _repo_with_origin(tmp_path / "c", "https://github.com/acme/widget")

    assert origin_repo_slug(str(https)) == "acme/widget"
    assert origin_repo_slug(str(ssh)) == "acme/widget"
    assert origin_repo_slug(str(bare_https)) == "acme/widget"


def test_origin_repo_slug_is_none_for_a_non_repo_dir(tmp_path):
    from aorc.harness import origin_repo_slug

    assert origin_repo_slug(str(tmp_path)) is None


def test_ensure_target_clone_uses_repo_dir_when_origin_matches(tmp_path):
    from aorc.harness import ensure_target_clone

    repo_dir = _repo_with_origin(tmp_path / "checkout", "https://github.com/acme/widget.git")

    resolved = ensure_target_clone(
        str(repo_dir), "acme/widget", clone_dir=str(tmp_path / "clone")
    )

    assert resolved == str(repo_dir)
    assert not (tmp_path / "clone").exists()


def test_ensure_target_clone_clones_the_target_on_mismatch(tmp_path, capsys):
    """repo_dir is a checkout of some OTHER repo (the live bug: AORC itself)
    -- the clone must be materialized and the mismatch surfaced."""
    import subprocess as sp

    from aorc.harness import ensure_target_clone

    # A local bare repo stands in for github.com/acme/widget (clone_url seam).
    upstream = tmp_path / "upstream.git"
    seed = _repo_with_origin(tmp_path / "seed", "https://github.com/acme/widget.git")
    (seed / "README.md").write_text("sandbox\n")
    _git_out(["add", "."], cwd=seed)
    _git_out(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed"], cwd=seed)
    sp.run(["git", "clone", "-q", "--bare", str(seed), str(upstream)], check=True, capture_output=True)

    repo_dir = _repo_with_origin(tmp_path / "aorc-itself", "https://github.com/rajat/aorc.git")
    clone_dir = tmp_path / "clone"

    resolved = ensure_target_clone(
        str(repo_dir), "acme/widget", clone_dir=str(clone_dir), clone_url=str(upstream)
    )

    assert resolved == str(clone_dir)
    assert (clone_dir / "README.md").exists()
    err = capsys.readouterr().err
    assert "rajat/aorc" in err and "acme/widget" in err  # the Fix-1 mismatch notice


def test_ensure_target_clone_refuses_a_clone_dir_owned_by_another_repo(tmp_path):
    import pytest

    from aorc.harness import TargetRepoError, ensure_target_clone

    _repo_with_origin(tmp_path / "clone", "https://github.com/some/other.git")

    with pytest.raises(TargetRepoError, match="acme/widget"):
        ensure_target_clone(
            str(tmp_path / "not-a-repo"), "acme/widget", clone_dir=str(tmp_path / "clone")
        )


def test_ensure_target_clone_fails_closed_when_the_clone_fails(tmp_path):
    import pytest

    from aorc.harness import TargetRepoError, ensure_target_clone

    with pytest.raises(TargetRepoError, match="acme/widget"):
        ensure_target_clone(
            str(tmp_path / "not-a-repo"),
            "acme/widget",
            clone_dir=str(tmp_path / "clone"),
            clone_url=str(tmp_path / "no-such-upstream"),
        )


class _RecordingGit:
    """Routes fake results for the git invocations ensure_target_clone makes;
    records (argv, env) so token-hygiene can be asserted."""

    def __init__(self, geturl: dict[str, str] | None = None) -> None:
        self.geturl = geturl or {}
        self.calls: list[tuple[list, dict | None]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("env")))
        if "get-url" in argv:
            repo_dir = argv[argv.index("-C") + 1]
            url = self.geturl.get(repo_dir)
            rc = 0 if url else 128
            return subprocess.CompletedProcess(argv, rc, url or "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_ensure_target_clone_keeps_the_token_out_of_argv(tmp_path, monkeypatch):
    from aorc.harness import ensure_target_clone

    fake = _RecordingGit()
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)

    ensure_target_clone(
        str(tmp_path / "not-a-repo"),
        "acme/widget",
        token="ghs_supersecret",
        clone_dir=str(tmp_path / "clone"),
    )

    clone_calls = [(argv, env) for argv, env in fake.calls if "clone" in argv]
    assert clone_calls, "expected a git clone invocation"
    argv, env = clone_calls[0]
    assert "ghs_supersecret" not in " ".join(argv)
    assert env is not None and env.get("GIT_CONFIG_COUNT") == "1"
    assert "ghs_supersecret" not in env["GIT_CONFIG_VALUE_0"]  # basic-auth encoded, not raw


def test_ensure_target_clone_fetches_an_existing_matching_clone(tmp_path, monkeypatch):
    from aorc.harness import ensure_target_clone

    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    fake = _RecordingGit(geturl={str(clone_dir): "https://github.com/acme/widget.git\n"})
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)

    resolved = ensure_target_clone(
        str(tmp_path / "not-a-repo"), "acme/widget", clone_dir=str(clone_dir)
    )

    assert resolved == str(clone_dir)
    assert any("fetch" in argv for argv, _ in fake.calls)
    assert not any("clone" in argv for argv, _ in fake.calls)


def test_worktree_manager_anchors_worktrees_dir_at_construction(tmp_path):
    """S35: with repo_dir now a clone elsewhere, a relative worktrees_dir
    must not silently resolve against the clone (git runs cwd=repo_dir) --
    it is anchored absolute at construction time."""
    import os

    from aorc.harness import WorktreeManager

    manager = WorktreeManager(str(tmp_path), ".aorc-worktrees")

    assert os.path.isabs(manager.path_for(3))


def test_docker_start_runs_a_long_lived_command(monkeypatch):
    """S33: `docker run -d <image>` with no command leaves the container's
    lifetime to the image's default CMD -- a stock image exits instantly,
    and every later `docker exec` toolchain run then fails 'container is
    not running'. The runtime must pin a long-lived command so the exec
    target stays alive for the issue's whole build."""
    from aorc.harness import DockerContainerRuntime

    fake = _FakeDockerRun()
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)
    runtime = DockerContainerRuntime("base:img")

    runtime.start(7, "aorc/issue-7", "/tmp/wt", env=None)

    image_at = fake.argv.index("base:img")
    assert fake.argv[image_at + 1 :] == ["tail", "-f", "/dev/null"]


def test_docker_start_without_env_passes_no_env_file(monkeypatch):
    from aorc.harness import DockerContainerRuntime

    fake = _FakeDockerRun()
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)
    runtime = DockerContainerRuntime("base:img")

    runtime.start(8, "aorc/issue-8", "/tmp/wt", env=None)

    assert "--env-file" not in fake.argv
    assert "-e" not in fake.argv


# ---- S26 -- fetch remote branch on resume from a fresh worktree ----------- #


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _init_bare_remote(tmp_path):
    """A throwaway bare 'remote' repo, seeded with one commit on `main`,
    reachable only by cloning -- stands in for the real GitHub remote the
    orchestrator's own API commits ultimately land on."""
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], cwd=seed)
    _git(["config", "user.email", "a@a.com"], cwd=seed)
    _git(["config", "user.name", "a"], cwd=seed)
    (seed / "README.md").write_text("hi\n")
    _git(["add", "."], cwd=seed)
    _git(["commit", "-m", "init"], cwd=seed)
    _git(["remote", "add", "origin", str(remote)], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return remote


def _clone(remote, dest):
    _git(["clone", str(remote), str(dest)], cwd=dest.parent)
    _git(["config", "user.email", "a@a.com"], cwd=dest)
    _git(["config", "user.name", "a"], cwd=dest)
    return dest


def test_ensure_fetches_a_branch_committed_only_on_the_remote(tmp_path):
    """Machine B commits straight to the remote branch (standing in for the
    GitHub API commits the orchestrator makes) -- this clone (machine A)
    never sees it locally until `ensure()` fetches. Worktree never existed
    here, so this is the cold-start/fresh-worktree path."""
    remote = _init_bare_remote(tmp_path)
    local_clone = _clone(remote, tmp_path / "local")

    machine_b = _clone(remote, tmp_path / "machine-b")
    branch = branch_name(99)
    _git(["checkout", "-b", branch], cwd=machine_b)
    (machine_b / "generated_test.py").write_text("def test_x():\n    assert True\n")
    _git(["add", "."], cwd=machine_b)
    _git(["commit", "-m", "tests"], cwd=machine_b)
    _git(["push", "origin", branch], cwd=machine_b)

    worktrees = WorktreeManager(str(local_clone), str(tmp_path / "worktrees"))
    path = worktrees.ensure(99)

    assert (Path(path) / "generated_test.py").exists()


def test_ensure_bases_a_new_branch_on_fetched_remote_main_not_stale_local_head(tmp_path):
    """First-ever dispatch for this issue (no remote branch yet), but the
    remote's `main` has moved on since this clone's last fetch -- the new
    worktree must be cut from the fetched `origin/main`, not this clone's
    stale local `main`."""
    remote = _init_bare_remote(tmp_path)
    local_clone = _clone(remote, tmp_path / "local")

    machine_b = _clone(remote, tmp_path / "machine-b")
    (machine_b / "config.txt").write_text("new config\n")
    _git(["add", "."], cwd=machine_b)
    _git(["commit", "-m", "advance main"], cwd=machine_b)
    _git(["push", "origin", "main"], cwd=machine_b)

    worktrees = WorktreeManager(str(local_clone), str(tmp_path / "worktrees"))
    path = worktrees.ensure(123)

    assert (Path(path) / "config.txt").exists()


def test_ensure_without_a_remote_is_unaffected(tmp_path):
    """No `origin` configured (the existing unit-test repos) -- `ensure()`
    must behave exactly as before S26: no fetch attempted, branch off local
    HEAD."""
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))

    path = worktrees.ensure(55)

    assert (Path(path) / "README.md").exists()


# --------------------------------------------------------------------------- #
# S27 -- the container_name_for / issue_number_from_worktree_path naming
# convention `ContainerTestRunner` relies on to find the right container
# without a separate handle registry.
# --------------------------------------------------------------------------- #


def test_container_name_for_matches_docker_start_naming(monkeypatch):
    """`DockerContainerRuntime.start` must name the container exactly what
    `container_name_for` predicts -- that shared convention is the whole
    link `ContainerTestRunner` depends on."""
    from aorc.harness import DockerContainerRuntime

    fake = _FakeDockerRun()
    monkeypatch.setattr("aorc.harness.subprocess.run", fake)
    runtime = DockerContainerRuntime("base:img")

    handle = runtime.start(42, "aorc/issue-42", "/tmp/wt")

    assert handle.container_id == container_name_for(42)


def test_issue_number_from_worktree_path_matches_path_for():
    worktrees = WorktreeManager("/repo", "/worktrees")
    path = worktrees.path_for(17)

    assert issue_number_from_worktree_path(path) == 17


def test_issue_number_from_worktree_path_strips_trailing_slash():
    assert issue_number_from_worktree_path("/worktrees/issue-9/") == 9


def test_issue_number_from_worktree_path_none_for_unrelated_path():
    assert issue_number_from_worktree_path(".") is None
    assert issue_number_from_worktree_path("/tmp/not-a-worktree") is None
