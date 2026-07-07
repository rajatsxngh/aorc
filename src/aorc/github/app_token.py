"""S23 -- the real App-JWT -> installation-token exchange.

Conforms to `CredentialBroker`'s injectable `minter` seam
(`minter(private_key, repo, permissions) -> token string`, see
`credentials.py`): `build_app_token_minter(app_id)` returns a minter that

1. signs a short-lived RS256 JWT (`iss` = app id, ~10 minute `exp`, a small
   clock-drift backdate on `iat`),
2. resolves the installation for the target repo
   (`GET /repos/{owner}/{repo}/installation`), and
3. exchanges it for a single-repo, permission-narrowed installation token
   (`POST /app/installations/{id}/access_tokens`).

JWT signing (PyJWT + cryptography) and the HTTP calls are both lazily
imported/constructed -- only a real mint touches them, so the orchestrator
core and its unit tests stay zero-third-party-dep (invariant #1; PyGithub's
own SDK boundary is `sdk_adapter.py`, this is the separate App-auth half).
`sign_jwt` and `transport` are injectable the same way, so unit tests can
exercise the exchange (claims, endpoint sequence, permission narrowing, error
handling) against fakes without PyJWT/cryptography installed at all -- the
real signer/transport are only exercised by the credential-gated integration
test.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

# ~10 minutes: short-lived per the exchange's own security model, plus a
# small backdate on `iat` to tolerate clock drift against GitHub's clock.
_JWT_TTL_SECONDS = 600
_JWT_CLOCK_DRIFT_SECONDS = 60

_API_ROOT = "https://api.github.com"


class GithubAppAuthError(Exception):
    """The App-JWT -> installation-token exchange failed (bad installation
    lookup, non-2xx token response, ...). Never carries the private key --
    only the repo and the HTTP status are in the message."""


def _default_sign_jwt(claims: dict, private_key: str) -> str:
    import jwt  # lazy: PyJWT + cryptography, only touched for a real mint

    return jwt.encode(claims, private_key, algorithm="RS256")


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


def build_app_token_minter(
    app_id: str,
    *,
    sign_jwt: Callable[[dict, str], str] = _default_sign_jwt,
    transport: Callable[[str, str, dict, dict | None], tuple[int, dict]] = _default_transport,
    clock: Callable[[], float] = time.time,
) -> Callable[[str, str, dict], str]:
    """Build a minter closed over `app_id` (and, for tests, fake `sign_jwt`/
    `transport`/`clock`). Drops straight into
    `CredentialBroker(private_key=..., minter=build_app_token_minter(app_id))`."""

    def minter(private_key: str, repo: str, permissions: dict) -> str:
        owner, name = repo.split("/", 1)
        now = int(clock())
        claims = {
            "iat": now - _JWT_CLOCK_DRIFT_SECONDS,
            "exp": now + _JWT_TTL_SECONDS,
            "iss": app_id,
        }
        jwt_token = sign_jwt(claims, private_key)
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        status, installation = transport(
            "GET", f"{_API_ROOT}/repos/{owner}/{name}/installation", headers, None
        )
        if status != 200:
            raise GithubAppAuthError(
                f"failed to resolve the App installation for {repo!r}: HTTP {status}"
            )
        installation_id = installation["id"]

        status, token_response = transport(
            "POST",
            f"{_API_ROOT}/app/installations/{installation_id}/access_tokens",
            headers,
            {"repositories": [name], "permissions": permissions},
        )
        if status not in (200, 201):
            raise GithubAppAuthError(
                f"failed to mint an installation token for {repo!r}: HTTP {status}"
            )
        return token_response["token"]

    return minter
