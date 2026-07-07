"""S19 -- real-adapter smoke: `DockerContainerRuntime` against a real daemon.

Skips cleanly when no Docker daemon is reachable. The image defaults to a
tiny public one and can be overridden with ``AORC_IT_DOCKER_IMAGE`` (e.g. the
real pre-baked AORC base image in CI).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from aorc.harness import DockerContainerRuntime

pytestmark = pytest.mark.integration

# An issue number no real pipeline run would use, so the fixed
# `aorc-issue-<n>` container name cannot collide with live containers.
_ISSUE = 990019


def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"], check=True, capture_output=True, timeout=30
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _inspect(container_id: str, fmt: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "inspect", "-f", fmt, container_id],
        capture_output=True,
        text=True,
    )


def test_start_and_teardown_real_container(tmp_path):
    if not _docker_available():
        pytest.skip("no Docker daemon reachable")
    # `or` (not a .get default): CI passes unset vars through as "".
    image = os.environ.get("AORC_IT_DOCKER_IMAGE") or "alpine:3.20"
    # Clear any leftover from an earlier aborted run of this same test.
    subprocess.run(
        ["docker", "rm", "-f", f"aorc-issue-{_ISSUE}"], capture_output=True
    )
    runtime = DockerContainerRuntime(image)

    handle = runtime.start(
        _ISSUE,
        f"aorc/issue-{_ISSUE}",
        str(tmp_path),
        env={"AORC_SMOKE": "smoke-value"},
    )
    try:
        assert handle.container_id == f"aorc-issue-{_ISSUE}"
        # The container exists and the env-file delivery actually reached it.
        env_out = _inspect(handle.container_id, "{{.Config.Env}}")
        assert env_out.returncode == 0
        assert "AORC_SMOKE=smoke-value" in env_out.stdout
        # The worktree is mounted at /workspace.
        mounts = _inspect(handle.container_id, "{{json .Mounts}}")
        assert "/workspace" in mounts.stdout
    finally:
        runtime.teardown(handle)

    assert handle.status == "stopped"
    gone = _inspect(handle.container_id, "{{.Id}}")
    assert gone.returncode != 0  # rm -f actually removed it
