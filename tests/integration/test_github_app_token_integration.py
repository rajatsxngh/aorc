"""S23 -- real-adapter smoke: `build_app_token_minter` against the real
GitHub App-JWT -> installation-token exchange, then one real API read with
the minted token.

Gating env vars (anything missing -> clean skip):

- ``AORC_IT_GITHUB_APP_ID``           -- the registered App's numeric ID
- ``AORC_IT_GITHUB_APP_PRIVATE_KEY``  -- path to the App's PEM private key
- ``AORC_IT_GITHUB_REPO``             -- ``owner/name`` of a repo the App is
  installed on

Needs the ``apptoken`` extra (PyJWT[crypto]) installed; skips cleanly via
``importorskip`` when absent, same as ``test_github_sdk_integration.py``
skips on a missing PyGithub.
"""

from __future__ import annotations

import os

import pytest

from aorc.credentials import MINIMAL_PERMISSIONS, CredentialBroker

pytestmark = pytest.mark.integration


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


def test_real_exchange_mints_a_token_and_performs_one_real_read():
    pytest.importorskip("jwt", reason="apptoken extra (PyJWT[crypto]) not installed")
    app_id = _require_env("AORC_IT_GITHUB_APP_ID")
    key_path = _require_env("AORC_IT_GITHUB_APP_PRIVATE_KEY")
    repo = _require_env("AORC_IT_GITHUB_REPO")
    private_key = open(key_path).read()

    from aorc.github.app_token import build_app_token_minter

    broker = CredentialBroker(private_key, build_app_token_minter(app_id))
    issue_token = broker.mint(1, repo, MINIMAL_PERMISSIONS)

    assert issue_token.token
    assert issue_token.repo == repo

    from aorc.github.sdk_adapter import SdkGitHubClient

    client = SdkGitHubClient(issue_token.token, repo)
    issues = client.list_issues()  # one real, read-only API call
    assert isinstance(issues, list)
