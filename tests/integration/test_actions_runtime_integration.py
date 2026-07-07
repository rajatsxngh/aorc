"""S25 -- real-adapter smoke: `ActionsContainerRuntime` against the real
GitHub Actions API. Dispatches one real `workflow_dispatch` run and
immediately cancels it -- no build actually needs to succeed, this only
proves the dispatch/resolve/cancel round-trip against real GitHub.

Gating env vars (anything missing -> clean skip):

- ``AORC_IT_GITHUB_TOKEN``          -- a token with `actions: write` on the
  target repo (the same throwaway-repo token `test_github_sdk_integration.py`
  uses works if it carries that scope)
- ``AORC_IT_GITHUB_REPO``           -- ``owner/name`` of that repo
- ``AORC_IT_GITHUB_WORKFLOW_FILE``  -- filename (under
  ``.github/workflows/``) of a `workflow_dispatch`-enabled workflow already
  present on the repo's default branch

Needs the ``actions`` extra (PyNaCl) installed; skips cleanly via
``importorskip`` when absent, same as the App-token integration test skips
on a missing PyJWT.

NOT run against a real repo/App this iteration -- confirmed only that it
skips cleanly here.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


def test_dispatch_and_cancel_a_real_workflow_run():
    pytest.importorskip("nacl", reason="actions extra (PyNaCl) not installed")
    token = _require_env("AORC_IT_GITHUB_TOKEN")
    repo = _require_env("AORC_IT_GITHUB_REPO")
    workflow_file = _require_env("AORC_IT_GITHUB_WORKFLOW_FILE")

    from aorc.github.actions_runtime import ActionsContainerRuntime

    runtime = ActionsContainerRuntime(repo, workflow_file, token)

    handle = runtime.start(990025, "main", "/tmp/unused", env={"AORC_SMOKE": "smoke-value"})
    try:
        assert handle.container_id
    finally:
        runtime.teardown(handle)

    assert handle.status == "stopped"
