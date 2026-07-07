"""S24 -- Webhook receiver with HMAC signature verification.

The gap this closes: `install.route_webhook` (S18) is a pure payload ->
entry-point mapping with nothing that listens for a real HTTP delivery, and
there was no `X-Hub-Signature-256` verification anywhere. Until this landed,
AORC was poll-only (`backfill`/`wake` via S21) -- every webhook the App
manifest subscribes to was delivered to nothing.

Two pieces, both stdlib-only (no new eager dependency, `tests/
test_no_sdk_imports.py` stays green):

- `verify_signature`: constant-time (`hmac.compare_digest`) HMAC-SHA256 check
  of the raw request body against the `X-Hub-Signature-256` header. Pure
  function so it is trivially unit-testable against synthetic bodies,
  including missing headers and an almost-right signature.
- `make_request_handler` / `serve`: a small `http.server` handler that
  verifies before it ever parses the body -- a bad signature gets a 401 and
  the JSON is never touched, no handler invoked. A verified delivery is
  ACKed (200) immediately, then routed through the caller-supplied `route`
  callable (in real wiring, `functools.partial(install.route_webhook,
  handler=..., loop=..., installer=...)` -- this module adds no routing
  logic of its own; the existing mapping table owns that). At-least-once
  redelivery is already harmless: `wake.claim_event`'s `(issue, stage,
  head_sha)` dedup (S16) is the idempotency layer routed calls land on, not
  this receiver.

The webhook secret is read once at the composition root (`__main__.py`,
`AORC_WEBHOOK_SECRET`) and passed in here as a plain string -- never logged,
never echoed in a response.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
_SIGNATURE_PREFIX = "sha256="


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """True iff `signature` is a valid `sha256=<hexdigest>` HMAC of `body`
    keyed by `secret`. False on a missing header, a header without the
    `sha256=` prefix, or any digest mismatch (including an almost-right
    one) -- compared in constant time so a timing side-channel can't leak
    how many prefix bytes matched."""
    if not signature or not signature.startswith(_SIGNATURE_PREFIX):
        return False
    supplied = signature[len(_SIGNATURE_PREFIX) :]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


def make_request_handler(
    secret: str, route: Callable[[str, dict], object]
) -> type[BaseHTTPRequestHandler]:
    """Build a `BaseHTTPRequestHandler` bound to `secret`/`route`. Verifies
    before parsing; ACKs before routing so a slow `route` never risks
    GitHub's ~10s delivery timeout (each connection also runs on its own
    thread under `ThreadingHTTPServer`, so one slow delivery doesn't stall
    the next)."""

    class WebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass  # never let request bodies/headers (may carry the secret's target) hit stderr

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            if not verify_signature(secret, body, self.headers.get(SIGNATURE_HEADER)):
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.flush()
            event = self.headers.get(EVENT_HEADER, "")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return
            route(event, payload)

    return WebhookHandler


def serve(
    secret: str,
    route: Callable[[str, dict], object],
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Build and return a bound (not yet `serve_forever`-ing) server -- the
    caller controls the run loop (`python -m aorc serve`) and can shut it
    down cleanly in tests."""
    return ThreadingHTTPServer((host, port), make_request_handler(secret, route))
