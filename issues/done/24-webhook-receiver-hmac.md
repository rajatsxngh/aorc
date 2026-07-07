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

- [x] Missing, malformed, or wrong `X-Hub-Signature-256` → 401; payload not
      parsed, nothing routed (unit-tested with synthetically signed bodies,
      including an almost-right signature)
- [x] Signature comparison is constant-time (`hmac.compare_digest`)
- [x] Valid deliveries route through `route_webhook` unchanged — one test per
      existing route (install, pr-merged, pr-comment, issue backfill,
      repository_dispatch rollback) asserting the right handler fired
- [x] Duplicate delivery of the same event is a no-op end-to-end (S16
      `claim_event` exercised through the receiver path)
- [x] 200 returned promptly; handler work does not block the ACK past
      GitHub's timeout
- [x] Webhook secret from env/config only; never logged
- [x] `tests/test_no_sdk_imports.py` still green; receiver runs in the unit
      suite against mocks with zero third-party deps (or its one transport
      dep is optional-extra'd and the tests use stdlib)
- [x] README documents `serve` + tunnel setup for a dev machine

## Blocked by

- S21 (the receiver needs composed handlers to route into) and S22 (routed
  wakes must be able to dispatch real builds). Real deliveries also assume a
  registered App (manual step documented in S23).

## Outcome

Implemented as `src/aorc/webhook.py` (stdlib-only: `hmac`/`hashlib`/`json`/
`http.server` — no new dependency, `tests/test_no_sdk_imports.py` stays
green):

- `verify_signature(secret, body, signature)` — constant-time
  (`hmac.compare_digest`) `sha256=<hex>` check; false on a missing header, a
  header without the prefix, a wrong secret, a tampered body, or an
  almost-right digest (last hex char flipped).
- `make_request_handler`/`serve` — a `ThreadingHTTPServer` handler that
  verifies before parsing (bad signature → 401, body never touched), ACKs
  200 immediately, then calls the caller-supplied `route` callable. The
  receiver adds no routing logic of its own; real wiring passes
  `functools.partial(install.route_webhook, handler=, loop=, installer=)`.
- `__main__.py`: new `python -m aorc serve [--host] [--port]` subcommand,
  gated on a new required `AORC_WEBHOOK_SECRET` env var (read once at the
  composition root, handed straight to `webhook.serve`, never logged —
  verified by a test asserting the secret string is absent from both
  captured stdout and stderr). `compose()` now also builds a real
  `MergeTimeHandler` (previously never composed anywhere — S17 only defined
  the class): reuses the same `coder_stage`/`reviewer_stage`/`test_runner`
  the S22 driver block already builds when `setup`/`test` are configured,
  and degrades to `coder=None, reviewer=None, test_command=None` when they
  aren't (issue-close-on-merge still works without a build pipeline; stale
  PR re-review correctly stays unavailable, pinned by a test).
- Tests: `tests/test_webhook.py` (16 tests) — pure `verify_signature` cases
  (correct, missing, no-prefix, wrong secret, almost-right, tampered body);
  real loopback-HTTP tests for 401-and-unrouted (missing/wrong/almost-right
  signature); one HTTP-routed test per existing `route_webhook` route
  (install, pr-merged, pr-comment, issue backfill, repository_dispatch
  rollback); and an end-to-end duplicate-delivery test using a *real*
  `MergeTimeHandler` + `MockGitHubClient` (not spies) sent the same
  pr-merged payload twice over real HTTP, asserting `wake.claim_event`'s
  dedup fires exactly once (`delete_branch` called once, issue closed once).
  `tests/test_main.py` gained 6 tests for the `merge_handler` wiring and the
  `serve` subcommand (fail-closed without the secret, host/port flags,
  secret never printed).
- ACK-before-route is real decoupling, not just documentation: the HTTP
  response is flushed to the socket before `route()` runs, so tests that
  assert on routed side effects poll with a timeout (`_wait_until` in
  `test_webhook.py`) rather than asserting immediately after `post()`
  returns — this is inherent to the design, not test flakiness.
- Dev tunnel story (smee.io / ngrok) documented in `CLAUDE.md` under a new
  "Webhook receiver (S24)" section — this repo still has no top-level
  README (S21 precedent).
- **Not done / honest gaps:** no live exercise against a real GitHub App
  delivery (needs a registered App + real secret, same caveat as S23's
  integration test); `serve` runs single-process/single-worker
  (`ThreadingHTTPServer`, no process supervision, no TLS termination — that
  would sit in front of it in any real deployment, e.g. behind a reverse
  proxy). No new integration test was added (nothing here needs
  credentials/a daemon to gate on — everything is exercised against mocks
  and a real loopback socket).

Tests: 445 passed (423 prior + 16 new in `tests/test_webhook.py` + 6 new in
`tests/test_main.py`), 10 deselected (integration, unchanged). Next: S25
(Actions execution wiring) was blocked on S23 + S24 — both are now done, so
S25 is unblocked. S26 and S27 remain open and independent of S25.
