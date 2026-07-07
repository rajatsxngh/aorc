"""S25 -- `ActionsContainerRuntime`: the second `ContainerRuntime` (S4's
harness seam), dispatching a per-issue build as a GitHub Actions
`workflow_dispatch` run instead of a local Docker container.

Secret delivery: `workflow_dispatch` inputs are visible in run logs and echo
back on every runs-list API call, so the S15 broker's per-issue `env`
(GITHUB_TOKEN + AORC_LLM_API_KEY) never rides there -- that would recreate
exactly the leak `DockerContainerRuntime`'s env-file discipline exists to
avoid. Instead each value is sealed with libsodium's sealed-box scheme
against the repository's own Actions public key (the encryption the Secrets
API itself requires) and written as a repository secret named
`AORC_ISSUE_<n>_<KEY>`; the dispatched workflow reads it back via
`secrets.*`, never `inputs.*`. `teardown` deletes every secret it wrote for
that issue, mirroring `DockerContainerRuntime.start` unlinking its env-file
the moment it's no longer needed -- a per-issue credential should not
outlive the issue.

Sealing is lazily imported (PyNaCl) the same way `app_token.py` lazily
imports PyJWT: only a real `start()` call touches it, so the unit suite
(fake `seal`/`transport`) never needs the dependency installed at all. HTTP
goes over stdlib `urllib`, same as `app_token.py` -- no new eager dependency,
so `tests/test_no_sdk_imports.py` stays green.

The dispatch/cancel token is a *second*, orchestrator-side App token scoped
to `actions: write` -- wider than `credentials.MINIMAL_PERMISSIONS`, the
per-issue container ceiling -- because it authenticates the orchestrator's
own Actions API calls, never a container. Minted once at the S21
composition root (`__main__.compose`), not per issue.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Callable

from ..harness import ContainerHandle, ContainerRuntime

_API_ROOT = "https://api.github.com"


class ActionsDispatchError(Exception):
    """A workflow-dispatch/secret-write/cancel call failed. Never carries a
    secret value or the orchestrator token -- only the repo/run/HTTP status."""


def _default_transport(
    method: str, url: str, headers: dict, body: dict | None
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, (json.loads(payload) if payload else {})


def _default_seal(secret_value: str, public_key_b64: str) -> str:
    """Real libsodium sealed-box encryption against the repo's Actions
    public key -- lazily imported (PyNaCl), only touched by a real
    dispatch."""
    from nacl import encoding, public  # lazy: PyNaCl, the `actions` extra

    sealed_box = public.SealedBox(public.PublicKey(public_key_b64, encoding.Base64Encoder()))
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


class ActionsContainerRuntime(ContainerRuntime):
    """`start` dispatches `workflow_file` on `branch` via `workflow_dispatch`
    and resolves the resulting run id (the dispatch call itself returns no
    body, so the run is looked up by branch+event right after). `teardown`
    cancels the run and deletes any repo secrets `start` wrote for that
    issue."""

    def __init__(
        self,
        repo: str,
        workflow_file: str,
        token: str,
        *,
        transport: Callable[[str, str, dict, dict | None], tuple[int, dict]] = _default_transport,
        seal: Callable[[str, str], str] = _default_seal,
    ) -> None:
        self._repo = repo
        self._workflow_file = workflow_file
        self._token = token
        self._transport = transport
        self._seal = seal
        self._secret_names: dict[int, list[str]] = {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _write_secret(self, name: str, value: str) -> None:
        status, key_response = self._transport(
            "GET", f"{_API_ROOT}/repos/{self._repo}/actions/secrets/public-key", self._headers(), None
        )
        if status != 200:
            raise ActionsDispatchError(
                f"failed to fetch the Actions public key for {self._repo!r}: HTTP {status}"
            )
        encrypted = self._seal(value, key_response["key"])
        status, _ = self._transport(
            "PUT",
            f"{_API_ROOT}/repos/{self._repo}/actions/secrets/{name}",
            self._headers(),
            {"encrypted_value": encrypted, "key_id": key_response["key_id"]},
        )
        if status not in (201, 204):
            raise ActionsDispatchError(f"failed to write secret {name!r}: HTTP {status}")

    def _delete_secret(self, name: str) -> None:
        self._transport(
            "DELETE",
            f"{_API_ROOT}/repos/{self._repo}/actions/secrets/{name}",
            self._headers(),
            None,
        )

    def start(
        self,
        issue_number: int,
        branch: str,
        worktree_path: str,
        env: dict[str, str] | None = None,
    ) -> ContainerHandle:
        secret_names = []
        for key in sorted((env or {}).keys()):
            secret_name = f"AORC_ISSUE_{issue_number}_{key}"
            self._write_secret(secret_name, env[key])
            secret_names.append(secret_name)
        self._secret_names[issue_number] = secret_names

        status, _ = self._transport(
            "POST",
            f"{_API_ROOT}/repos/{self._repo}/actions/workflows/{self._workflow_file}/dispatches",
            self._headers(),
            {"ref": branch, "inputs": {"issue_number": str(issue_number)}},
        )
        if status != 204:
            raise ActionsDispatchError(f"failed to dispatch {self._workflow_file!r}: HTTP {status}")

        status, runs = self._transport(
            "GET",
            f"{_API_ROOT}/repos/{self._repo}/actions/workflows/{self._workflow_file}/runs"
            f"?event=workflow_dispatch&branch={branch}",
            self._headers(),
            None,
        )
        workflow_runs = runs.get("workflow_runs") if status == 200 else []
        if not workflow_runs:
            raise ActionsDispatchError(
                f"dispatched {self._workflow_file!r} but could not resolve the run id: HTTP {status}"
            )

        return ContainerHandle(
            issue_number=issue_number,
            branch=branch,
            worktree_path=worktree_path,
            container_id=str(workflow_runs[0]["id"]),
        )

    def teardown(self, handle: ContainerHandle) -> None:
        status, _ = self._transport(
            "POST",
            f"{_API_ROOT}/repos/{self._repo}/actions/runs/{handle.container_id}/cancel",
            self._headers(),
            None,
        )
        if status not in (200, 202):
            raise ActionsDispatchError(
                f"failed to cancel run {handle.container_id}: HTTP {status}"
            )
        for name in self._secret_names.pop(handle.issue_number, []):
            self._delete_secret(name)
        handle.status = "stopped"
