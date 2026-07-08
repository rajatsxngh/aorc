"""S27 -- real-adapter smoke: `ContainerTestRunner` against a real Docker
daemon. Skips cleanly when no daemon is reachable (same gate
`test_docker_integration.py` uses).

`DockerContainerRuntime.start` never overrides a base image's default
command, so a bare `alpine` container run through it exits immediately
(nothing to `docker exec` into) -- this test instead starts its own
long-running container directly (`sleep`, overriding the default command),
named with the exact same `container_name_for` convention
`DockerContainerRuntime.start` uses, and mounts a throwaway directory at
`/workspace` the same way. That's the real thing being proven here: a
command run through `ContainerTestRunner` against that convention actually
lands in the mounted worktree, and a file written to the worktree from the
host (the S22 `write_worktree_file` mirror) is visible from inside the
container -- without ever standing up the full per-issue pipeline.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from aorc.harness import container_name_for
from aorc.tester import ContainerTestRunner

pytestmark = pytest.mark.integration

# An issue number no real pipeline run would use, so the fixed
# `aorc-issue-<n>` container name cannot collide with live containers.
_ISSUE = 990027


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def test_container_test_runner_execs_against_the_mounted_worktree(tmp_path):
    if not _docker_available():
        pytest.skip("no Docker daemon reachable")
    image = os.environ.get("AORC_IT_DOCKER_IMAGE") or "alpine:3.20"
    name = container_name_for(_ISSUE)
    # `WorktreeManager.path_for`'s exact naming convention --
    # `ContainerTestRunner` resolves the container name from this shape.
    worktree = tmp_path / f"issue-{_ISSUE}"
    worktree.mkdir()

    subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # leftover from an aborted run
    subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-v", f"{worktree}:/workspace",
            "-w", "/workspace",
            image, "sleep", "300",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        # host -> container: the S22 `write_worktree_file` mirror is visible
        # from inside the container the toolchain actually runs in.
        (worktree / "generated_test.py").write_text("def test_x():\n    assert True\n")
        runner = ContainerTestRunner()

        read_back = runner.run(str(worktree), "cat generated_test.py")
        assert read_back.returncode == 0
        assert "def test_x" in read_back.stdout

        # container -> host: a command's effects land back in the mounted
        # worktree, where the orchestrator-side driver/coder read from next.
        write_result = runner.run(str(worktree), "echo ran > toolchain_output.txt")
        assert write_result.returncode == 0
        assert (worktree / "toolchain_output.txt").read_text().strip() == "ran"

        # a real failure surfaces as the same TestRunResult shape the fix
        # loop already consumes -- not an exception, not a truncated stream.
        failure = runner.run(str(worktree), "exit 7")
        assert failure.returncode == 7
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
