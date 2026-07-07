# S24 — Webhook receiver with HMAC signature verification

The readiness review found no HTTP receiver anywhere in the repo:
`route_webhook` (S18) is a pure payload → entry-point mapping with nothing
that listens, and there is **no** `X-Hub-Signature-256` verification code at
all. Until this lands, AORC is poll-only (manual `backfill`/`wake` via S21) —
every webhook the App manifest subscribes to is delivered to nothing.

## What to build

A small HTTP receiver process that turns real GitHub deliveries into calls on
the objects S21 composes:

- **Verify before parse.** Compute HMAC-SHA256 of the raw request body with
  the webhook secret and compare against `X-Hub-Signature-256` using a
  constant-time comparison (`hmac.compare_digest`). Missing or wrong
  signature → 401, body never parsed, no handler invoked. The secret comes
  from env/config (invariant #2), set at App registration.
- **Route.** Extract `X-GitHub-Event` + JSON payload and hand them to the
  existing `route_webhook(event, payload, handler=…, loop=…, installer=…)` —
  the mapping table is already built and unit-tested; the receiver adds no
  routing logic of its own. PR merges therefore reach
  `MergeTimeHandler.on_pr_merged` (never the bare sweep), per the S18
  warning.
- **ACK fast.** Respond 200 within GitHub's ~10s delivery timeout; the routed
  work (wake, backfill, merge handling) runs after/decoupled from the ACK.
  At-least-once redelivery is already harmless: `claim_event`'s
  `(issue, stage, head_sha)` dedup (S16) is the idempotency layer, not the
  receiver.
- **Stay thin.** Stdlib `http.server` or one micro-dependency behind its own
  seam — the receiver is transport only, so orchestrator core keeps zero SDK
  imports. Started via an S21 subcommand (e.g. `python -m aorc serve`).
- **Dev story documented:** smee.io / ngrok tunnel instructions for pointing
  a real App's webhook URL at a laptop.

## Acceptance criteria

- [ ] Missing, malformed, or wrong `X-Hub-Signature-256` → 401; payload not
      parsed, nothing routed (unit-tested with synthetically signed bodies,
      including an almost-right signature)
- [ ] Signature comparison is constant-time (`hmac.compare_digest`)
- [ ] Valid deliveries route through `route_webhook` unchanged — one test per
      existing route (install, pr-merged, pr-comment, issue backfill,
      repository_dispatch rollback) asserting the right handler fired
- [ ] Duplicate delivery of the same event is a no-op end-to-end (S16
      `claim_event` exercised through the receiver path)
- [ ] 200 returned promptly; handler work does not block the ACK past
      GitHub's timeout
- [ ] Webhook secret from env/config only; never logged
- [ ] `tests/test_no_sdk_imports.py` still green; receiver runs in the unit
      suite against mocks with zero third-party deps (or its one transport
      dep is optional-extra'd and the tests use stdlib)
- [ ] README documents `serve` + tunnel setup for a dev machine

## Blocked by

- S21 (the receiver needs composed handlers to route into) and S22 (routed
  wakes must be able to dispatch real builds). Real deliveries also assume a
  registered App (manual step documented in S23).
