# S28 — Board creation must degrade gracefully when the token can't create Projects v2

Rung-1 live testing against a real sandbox repo: `SdkGitHubClient.create_board`
(install-time Projects v2 `createProjectV2` GraphQL mutation) fails with

```
GithubException 400 FORBIDDEN — "Resource not accessible by personal access token"
```

and the exception propagates, crashing the whole install/backfill run. AORC's
mutation is correct; GitHub's token policy refuses it: **fine-grained PATs
cannot create Projects v2 projects at all** — the mutation needs a classic PAT
with the `project` scope (or a token whose account/org grant includes the
Projects permission). Any credential below that ceiling gets FORBIDDEN, no
matter what repository permissions it carries.

That must not be fatal. Per the S2/Option-1 decision the board is a *derived,
display-only projection* of the labels — labels are the source of truth, and
`sdk_adapter.py` already treats an unconfigured project as a no-op for
`set_board_column`/`get_board_column`. A token that can run the entire
label-driven pipeline but can't create a cosmetic board should degrade to
label-only operation, not crash the install.

## What to build

1. In `SdkGitHubClient.create_board`, catch the FORBIDDEN/auth failure from
   the Projects v2 GraphQL calls (GraphQL surfaces it as a `GithubException`
   whose payload carries a `FORBIDDEN` error type / "Resource not accessible"
   message; plain 401/403 auth failures count too). On catch:
   - log one clear line: board unavailable with this token — proceeding with
     labels only;
   - leave the client in the label-only state (project unset, so every later
     board op is the existing no-op) and return normally.
2. Non-auth errors (network, malformed response, genuine bugs) still raise —
   only the permission refusal is downgraded.

## Acceptance criteria

- [ ] `create_board` hitting FORBIDDEN (or any auth error) is caught and
      logged as "board unavailable — proceeding with labels only"; it never
      propagates a crash out of install/backfill
- [ ] After the degrade, board ops on the same client are no-ops (label flow
      continues untouched)
- [ ] A unit test proves a forbidden `create_board` degrades gracefully —
      no crash, label operations still work — with zero third-party deps
      (fake exception through the `_graphql` seam, no PyGithub import)
- [ ] A non-auth `_graphql` failure still raises unchanged

## Blocked by

Nothing — S18 (install) and S21 (composition root) already in place.
