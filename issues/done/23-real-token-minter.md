# S23 — Real token minter (App-JWT → installation access-token exchange)

The readiness review found that `CredentialBroker.minter` — the injectable
App-JWT → installation-token exchange the S15 docstring explicitly deferred
("the real exchange is S18/S19's job") — has **no real implementation
anywhere in `src/`**. The only minter in the repo is the test suite's
`RecordingMinter`. Every live run so far must bypass the entire S15
short-lived-token model with a PAT stand-in (S21's interim minter). This
ticket makes minting real.

## What to build

A minter callable matching the existing seam signature
`minter(private_key, repo, permissions) -> token string`, implementing the
standard GitHub App exchange:

1. Sign a short-lived RS256 JWT with the App private key
   (`iss` = App ID, `exp` ≈ 10 minutes, small clock-drift backdate on `iat`).
2. Resolve the installation for the target repo
   (`GET /repos/{owner}/{repo}/installation`).
3. `POST /app/installations/{id}/access_tokens` with the `repositories` and
   `permissions` fields set from the arguments — the single-repo scope and
   the minimal permission set are enforced **server-side by GitHub**, not
   only by the broker's `PermissionCeilingError` check.
4. Return the token string; the broker owns TTL bookkeeping as today.

Placement per invariant #1: this touches the GitHub HTTP surface and a JWT
library (PyGithub's `GithubIntegration`/`Auth.AppAuth`, or PyJWT +
cryptography), so it lives behind the adapter boundary (`src/aorc/github/`),
imported lazily like `sdk_adapter`, never by orchestrator core. App ID and
private-key **path** come from config/env (invariant #2); the key material is
read at the composition root and handed only to `CredentialBroker`.

Out of scope: registering the App itself (manifest flow) remains a manual /
documented step — this ticket documents it (App ID, key download, install on
the sandbox repo) but automates only the exchange.

## Acceptance criteria

- [ ] Real minter conforms to the seam signature and drops into
      `CredentialBroker` unchanged; S21's PAT stand-in is deleted or demoted
      to an explicit `--dev` escape hatch
- [ ] Token request carries `repositories: [repo]` and the narrowed
      `permissions` map — server-side scoping, verified in tests against the
      request body
- [ ] JWT is short-lived (~10 min) and the private key never appears in any
      log, error message, or container env (existing `CredentialLeakError`
      paths still pin this)
- [ ] Orchestrator core still imports zero SDKs
      (`tests/test_no_sdk_imports.py` green); JWT/HTTP deps are a new optional
      extra alongside `github`
- [ ] Unit tests cover the exchange against a fake transport (JWT claims,
      endpoint sequence, permission narrowing, non-200 → clean error)
- [ ] Credential-gated integration test (S19 pattern: skip clean when
      `AORC_IT_GITHUB_APP_ID` / key / repo env vars absent) mints a real
      token and performs one real API read with it
- [ ] README documents the one-time manual App registration + installation
      steps this ticket does not automate

## Blocked by

- S21 (a composition root must exist for the minter to be injected into) and
  S22 (a live pipeline is what gives per-issue tokens something to do).

## Outcome

Implemented as `build_app_token_minter` in `src/aorc/github/app_token.py`
(new file, adapter-boundary, lazily imports PyJWT for signing and uses stdlib
`urllib` for the two HTTP calls — no new eager SDK import). It closes over
`app_id` and returns a `minter(private_key, repo, permissions) -> token`
matching the existing seam exactly; `sign_jwt`/`transport`/`clock` are all
injectable so the unit suite (`tests/test_app_token.py`, 7 tests) exercises
JWT claims (iss/iat/exp, short-lived + clock-drift-backdated), the
GET-installation → POST-access-token endpoint sequence, permission
narrowing in the request body, and clean `GithubAppAuthError`s on a non-200
from either call (private key never appears in the error) — all without
PyJWT or a network actually installed/reachable.

`__main__.py`'s `compose()` now builds this real minter by default, reading
`AORC_GITHUB_APP_ID` + `AORC_GITHUB_APP_PRIVATE_KEY_PATH` at the composition
root and handing only the key *material* (never the path) to
`CredentialBroker`. S21's PAT stand-in (`pat_passthrough_minter`) is kept,
not deleted, but demoted to an explicit `--dev-pat-minter` CLI flag —
useful for a dev loop with no GitHub App registered, never for a live run.
New/changed wiring tests in `tests/test_main.py` (5 new) cover: missing
`AORC_GITHUB_APP_ID`/`AORC_GITHUB_APP_PRIVATE_KEY_PATH` fail closed with
`StartupError`; the default path reads the key file and wires the real
minter (checked by module identity, not by actually minting — construction
must stay dependency-free); `--dev-pat-minter` wires the passthrough minter
instead and the flag threads from `run()` through to `compose()`.

New optional extra `apptoken = ["PyJWT[crypto]>=2.8"]` in `pyproject.toml`,
alongside (not replacing) `github`, since the HTTP half of the exchange is
plain `urllib`, not PyGithub. `tests/test_no_sdk_imports.py` stays green —
`app_token.py` never imports `jwt` at module scope.

Credential-gated integration test added:
`tests/integration/test_github_app_token_integration.py`, gated on
`AORC_IT_GITHUB_APP_ID`/`AORC_IT_GITHUB_APP_PRIVATE_KEY`/`AORC_IT_GITHUB_REPO`
plus an `importorskip("jwt")` for the `apptoken` extra; mints a real token
via the real exchange and performs one real `list_issues()` read through
`SdkGitHubClient` with it. Confirmed this skips cleanly in this environment
(no App registered, PyJWT not installed) — **not run against a real GitHub
App in this iteration**; the exchange's correctness against the real GitHub
API is unverified beyond the fake-transport unit tests.

Manual App registration steps (App creation, permissions matching
`MINIMAL_PERMISSIONS`, webhook subscriptions for S24, private key download,
installing on the target repo, installing the `apptoken` extra) documented
in `CLAUDE.md` under "One-time GitHub App registration (S23)" — this repo
has no top-level README (per S21's precedent, docs live in CLAUDE.md).

Tests: 423 passed (411 prior + 12 new: 7 in test_app_token.py, 5 in
test_main.py), 10 deselected (integration, incl. the 1 new one — all skip
clean). Next: S24 (webhook receiver) and S26/S27 remain open and unblocked;
S25 stays blocked until S24 also lands.
