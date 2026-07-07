"""S23 -- real token minter: App-JWT -> installation-token exchange.

Exercised against fake `sign_jwt`/`transport` callables so this suite needs
neither PyJWT/cryptography nor the network (zero-third-party-dep unit suite,
same discipline as the SDK adapters' lazy imports). The real signer/transport
are only exercised by the credential-gated integration test.
"""

from __future__ import annotations

import pytest

from aorc.github.app_token import GithubAppAuthError, build_app_token_minter


class FakeTransport:
    """Records every call and returns scripted (status, body) pairs in
    order -- one entry per expected HTTP call (installation lookup, then
    token exchange)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self._responses.pop(0)


def fake_sign_jwt(claims, private_key):
    # Records nothing itself -- tests capture `claims` via a closure below
    # when they need to inspect them; this default just fabricates a token
    # string distinct from any real JWT shape.
    return f"fake-jwt-for-{claims['iss']}"


INSTALLATION_OK = (200, {"id": 987654})
TOKEN_OK = (201, {"token": "ghs_" + "m" * 36})


def test_happy_path_mints_token_and_hits_both_endpoints_in_order():
    transport = FakeTransport([INSTALLATION_OK, TOKEN_OK])
    minter = build_app_token_minter("app-123", sign_jwt=fake_sign_jwt, transport=transport)

    token = minter("private-key-pem", "acme/widget", {"contents": "write"})

    assert token == "ghs_" + "m" * 36
    assert [c[0] for c in transport.calls] == ["GET", "POST"]
    assert transport.calls[0][1] == "https://api.github.com/repos/acme/widget/installation"
    assert (
        transport.calls[1][1]
        == "https://api.github.com/app/installations/987654/access_tokens"
    )


def test_token_request_carries_single_repo_name_and_narrowed_permissions():
    transport = FakeTransport([INSTALLATION_OK, TOKEN_OK])
    minter = build_app_token_minter("app-123", sign_jwt=fake_sign_jwt, transport=transport)

    minter("private-key-pem", "acme/widget", {"contents": "write", "issues": "read"})

    _method, _url, _headers, body = transport.calls[1]
    assert body == {
        "repositories": ["widget"],
        "permissions": {"contents": "write", "issues": "read"},
    }


def test_both_requests_carry_the_signed_jwt_as_bearer_auth():
    transport = FakeTransport([INSTALLATION_OK, TOKEN_OK])
    minter = build_app_token_minter("app-123", sign_jwt=fake_sign_jwt, transport=transport)

    minter("private-key-pem", "acme/widget", {"contents": "write"})

    for _method, _url, headers, _body in transport.calls:
        assert headers["Authorization"] == "Bearer fake-jwt-for-app-123"


def test_jwt_claims_are_short_lived_and_backdated_for_clock_drift():
    captured = {}

    def capturing_sign_jwt(claims, private_key):
        captured.update(claims)
        return "signed"

    transport = FakeTransport([INSTALLATION_OK, TOKEN_OK])
    minter = build_app_token_minter(
        "app-123", sign_jwt=capturing_sign_jwt, transport=transport, clock=lambda: 1_000_000.0
    )

    minter("private-key-pem", "acme/widget", {"contents": "write"})

    assert captured["iss"] == "app-123"
    assert captured["iat"] == 1_000_000 - 60  # backdated for clock drift
    assert captured["exp"] == 1_000_000 + 600  # ~10 minutes
    assert captured["exp"] - captured["iat"] <= 660


def test_installation_lookup_non_200_raises_clean_error_without_the_private_key():
    transport = FakeTransport([(404, {"message": "Not Found"})])
    minter = build_app_token_minter("app-123", sign_jwt=fake_sign_jwt, transport=transport)

    with pytest.raises(GithubAppAuthError, match="acme/widget") as excinfo:
        minter("super-secret-private-key", "acme/widget", {"contents": "write"})

    assert "super-secret-private-key" not in str(excinfo.value)
    assert len(transport.calls) == 1  # never reached the token exchange


def test_token_exchange_non_2xx_raises_clean_error_without_the_private_key():
    transport = FakeTransport([INSTALLATION_OK, (403, {"message": "Forbidden"})])
    minter = build_app_token_minter("app-123", sign_jwt=fake_sign_jwt, transport=transport)

    with pytest.raises(GithubAppAuthError, match="acme/widget") as excinfo:
        minter("super-secret-private-key", "acme/widget", {"contents": "write"})

    assert "super-secret-private-key" not in str(excinfo.value)


def test_conforms_to_the_credential_broker_minter_seam():
    from aorc.credentials import CredentialBroker

    transport = FakeTransport([INSTALLATION_OK, TOKEN_OK])
    minter = build_app_token_minter("app-123", sign_jwt=fake_sign_jwt, transport=transport)
    broker = CredentialBroker("private-key-pem", minter, clock=lambda: 1000.0)

    issue_token = broker.mint(42, "acme/widget")

    assert issue_token.token == "ghs_" + "m" * 36
