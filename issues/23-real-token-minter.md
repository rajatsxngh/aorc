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
