"""S25 -- `ActionsContainerRuntime`: the second `ContainerRuntime`, dispatching
a per-issue build as a GitHub Actions `workflow_dispatch` run instead of a
local Docker container.

Exercised against a fake `transport`/`seal` (same injectable-callable style
as `app_token.py`'s `_default_transport`/`_default_sign_jwt`) so this suite
needs neither PyNaCl nor the network -- the real sealed-box encryption and
HTTP calls are only exercised by the credential-gated integration test.
"""

from __future__ import annotations

import json

import pytest

from aorc.github.actions_runtime import ActionsContainerRuntime, ActionsDispatchError
from aorc.harness import ContainerHandle, ContainerRuntime


class FakeTransport:
    """Records every call and returns scripted (status, body) pairs in
    order, same idiom as `test_app_token.py`'s `FakeTransport`."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self._responses.pop(0)


def fake_seal(value: str, public_key_b64: str) -> str:
    return f"sealed({public_key_b64}):{value}"


PUBLIC_KEY_OK = (200, {"key": "pk-base64", "key_id": "key-1"})
SECRET_WRITE_OK = (204, {})
DISPATCH_OK = (204, {})
RUNS_OK = (200, {"workflow_runs": [{"id": 555}]})
CANCEL_OK = (202, {})
SECRET_DELETE_OK = (204, {})


def _runtime(transport):
    return ActionsContainerRuntime(
        "acme/widget",
        "aorc-build.yml",
        "ghs_" + "t" * 36,
        transport=transport,
        seal=fake_seal,
    )


def test_conforms_to_the_container_runtime_abc():
    assert issubclass(ActionsContainerRuntime, ContainerRuntime)


def test_start_without_env_dispatches_and_resolves_the_run_id():
    transport = FakeTransport([DISPATCH_OK, RUNS_OK])
    runtime = _runtime(transport)

    handle = runtime.start(7, "aorc/issue-7", "/worktrees/issue-7")

    assert [c[0] for c in transport.calls] == ["POST", "GET"]
    assert transport.calls[0][1] == (
        "https://api.github.com/repos/acme/widget/actions/workflows/aorc-build.yml/dispatches"
    )
    assert transport.calls[0][3] == {"ref": "aorc/issue-7", "inputs": {"issue_number": "7"}}
    assert "runs?" in transport.calls[1][1]
    assert handle == ContainerHandle(
        issue_number=7,
        branch="aorc/issue-7",
        worktree_path="/worktrees/issue-7",
        container_id="555",
    )


def test_start_seals_each_env_value_as_a_repo_secret_before_dispatch():
    transport = FakeTransport([PUBLIC_KEY_OK, SECRET_WRITE_OK, DISPATCH_OK, RUNS_OK])
    runtime = _runtime(transport)

    runtime.start(7, "aorc/issue-7", "/worktrees/issue-7", env={"GITHUB_TOKEN": "ghs_secret123"})

    method, url, _headers, body = transport.calls[1]
    assert method == "PUT"
    assert url == (
        "https://api.github.com/repos/acme/widget/actions/secrets/AORC_ISSUE_7_GITHUB_TOKEN"
    )
    assert body == {
        "encrypted_value": "sealed(pk-base64):ghs_secret123",
        "key_id": "key-1",
    }


def test_secrets_never_ride_in_dispatch_inputs():
    transport = FakeTransport([PUBLIC_KEY_OK, SECRET_WRITE_OK, DISPATCH_OK, RUNS_OK])
    runtime = _runtime(transport)

    runtime.start(7, "aorc/issue-7", "/worktrees/issue-7", env={"GITHUB_TOKEN": "ghs_secret123"})

    dispatch_call = transport.calls[2]
    assert dispatch_call[0] == "POST" and "dispatches" in dispatch_call[1]
    assert dispatch_call[3] == {"ref": "aorc/issue-7", "inputs": {"issue_number": "7"}}
    assert "ghs_secret123" not in json.dumps(dispatch_call[3])
    # ...nor anywhere else in the whole call log except the one secret-write
    # body, where only the *sealed* form (never the plaintext) appears.
    for method, _url, _headers, body in transport.calls:
        if method == "PUT":
            continue
        assert "ghs_secret123" not in json.dumps(body)


def test_dispatch_non_204_raises_clean_error():
    transport = FakeTransport([(422, {"message": "bad ref"})])
    runtime = _runtime(transport)

    with pytest.raises(ActionsDispatchError, match="dispatch"):
        runtime.start(7, "aorc/issue-7", "/worktrees/issue-7")


def test_run_resolution_failure_raises_clean_error():
    transport = FakeTransport([DISPATCH_OK, (200, {"workflow_runs": []})])
    runtime = _runtime(transport)

    with pytest.raises(ActionsDispatchError, match="run id"):
        runtime.start(7, "aorc/issue-7", "/worktrees/issue-7")


def test_secret_write_failure_raises_clean_error_without_the_plaintext():
    transport = FakeTransport([PUBLIC_KEY_OK, (403, {"message": "Forbidden"})])
    runtime = _runtime(transport)

    with pytest.raises(ActionsDispatchError) as excinfo:
        runtime.start(7, "aorc/issue-7", "/worktrees/issue-7", env={"GITHUB_TOKEN": "ghs_x"})

    assert "ghs_x" not in str(excinfo.value)


def test_teardown_cancels_the_run_and_deletes_the_issues_secrets():
    transport = FakeTransport(
        [PUBLIC_KEY_OK, SECRET_WRITE_OK, DISPATCH_OK, RUNS_OK, CANCEL_OK, SECRET_DELETE_OK]
    )
    runtime = _runtime(transport)
    handle = runtime.start(7, "aorc/issue-7", "/worktrees/issue-7", env={"GITHUB_TOKEN": "ghs_x"})

    runtime.teardown(handle)

    cancel_call = transport.calls[4]
    assert cancel_call[0] == "POST"
    assert cancel_call[1] == "https://api.github.com/repos/acme/widget/actions/runs/555/cancel"
    delete_call = transport.calls[5]
    assert delete_call[0] == "DELETE"
    assert delete_call[1] == (
        "https://api.github.com/repos/acme/widget/actions/secrets/AORC_ISSUE_7_GITHUB_TOKEN"
    )
    assert handle.status == "stopped"


def test_teardown_without_prior_secrets_only_cancels():
    transport = FakeTransport([DISPATCH_OK, RUNS_OK, CANCEL_OK])
    runtime = _runtime(transport)
    handle = runtime.start(7, "aorc/issue-7", "/worktrees/issue-7")

    runtime.teardown(handle)

    assert len(transport.calls) == 3  # dispatch, runs, cancel -- no secret calls


def test_cancel_non_2xx_raises_clean_error():
    transport = FakeTransport([DISPATCH_OK, RUNS_OK, (500, {})])
    runtime = _runtime(transport)
    handle = runtime.start(7, "aorc/issue-7", "/worktrees/issue-7")

    with pytest.raises(ActionsDispatchError, match="cancel"):
        runtime.teardown(handle)
