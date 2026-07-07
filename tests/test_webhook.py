"""S24 -- Webhook receiver: HMAC verification + routing through the existing
`install.route_webhook` mapping, over a real (loopback) HTTP server."""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from aorc.credentials import CredentialBroker
from aorc.github.mock import MockGitHubClient
from aorc.harness import MockContainerRuntime
from aorc.install import route_webhook
from aorc.interfaces import Issue, PullRequest
from aorc.merge import MergeTimeHandler, MockGitOps
from aorc.pipeline import DONE_COLUMN, branch_name
from aorc.wake import WakeLoop
from aorc.webhook import SIGNATURE_HEADER, serve, verify_signature

SECRET = "s3kr1t"
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEfakefakefakefakefakefakefake\n"
    "-----END RSA PRIVATE KEY-----"
)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# verify_signature: pure function
# --------------------------------------------------------------------------- #


def test_verify_signature_accepts_correct_hmac():
    body = b'{"a": 1}'
    assert verify_signature(SECRET, body, _sign(SECRET, body)) is True


def test_verify_signature_rejects_missing_header():
    assert verify_signature(SECRET, b"{}", None) is False


def test_verify_signature_rejects_header_without_prefix():
    body = b"{}"
    bare_hex = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(SECRET, body, bare_hex) is False


def test_verify_signature_rejects_wrong_secret():
    body = b"{}"
    assert verify_signature(SECRET, body, _sign("wrong-secret", body)) is False


def test_verify_signature_rejects_almost_right_signature():
    body = b"{}"
    correct = _sign(SECRET, body)
    # Flip the last hex character -- almost right, still wrong.
    flipped_char = "0" if correct[-1] != "0" else "1"
    almost = correct[:-1] + flipped_char
    assert verify_signature(SECRET, body, almost) is False


def test_verify_signature_rejects_tampered_body():
    body = b'{"amount": 1}'
    signature = _sign(SECRET, body)
    assert verify_signature(SECRET, b'{"amount": 999}', signature) is False


# --------------------------------------------------------------------------- #
# Real HTTP server: verify-before-parse, ACK, routing
# --------------------------------------------------------------------------- #


class _RunningServer:
    def __init__(self, secret, route):
        self.server = serve(secret, route, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(self, event, payload, *, secret=SECRET, signature=None):
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
        sig = signature if signature is not None else _sign(secret, body)
        if sig is not False:
            headers[SIGNATURE_HEADER] = sig
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/webhook", data=body, headers=headers, method="POST"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _wait_until(predicate, timeout=2.0, interval=0.01):
    """The receiver ACKs before routing (by design -- S24 acceptance: routed
    work is decoupled from the ACK), so a client's `post()` can return before
    the server's route() call has run. Poll instead of asserting immediately."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def running_server():
    servers = []

    def start(route):
        s = _RunningServer(SECRET, route)
        servers.append(s)
        return s

    yield start
    for s in servers:
        s.close()


def test_missing_signature_is_rejected_and_never_routed(running_server):
    routed = []
    server = running_server(lambda event, payload: routed.append((event, payload)))

    status = server.post("issues", {"action": "opened"}, signature=False)

    assert status == 401
    assert routed == []


def test_wrong_signature_is_rejected_and_never_routed(running_server):
    routed = []
    server = running_server(lambda event, payload: routed.append((event, payload)))

    status = server.post("issues", {"action": "opened"}, secret="not-the-secret")

    assert status == 401
    assert routed == []


def test_almost_right_signature_is_rejected(running_server):
    routed = []
    server = running_server(lambda event, payload: routed.append((event, payload)))
    body = json.dumps({"action": "opened"}).encode()
    correct = _sign(SECRET, body)
    almost = correct[:-1] + ("0" if correct[-1] != "0" else "1")

    status = server.post("issues", {"action": "opened"}, signature=almost)

    assert status == 401
    assert routed == []


def test_valid_delivery_acks_200_and_routes(running_server):
    routed = []
    server = running_server(lambda event, payload: routed.append((event, payload)))

    status = server.post("issues", {"action": "opened", "issue": {"number": 3}})

    assert status == 200
    assert _wait_until(lambda: routed != [])
    assert routed == [("issues", {"action": "opened", "issue": {"number": 3}})]


class SpyMergeHandler:
    def __init__(self):
        self.calls = []

    def on_pr_merged(self, pr_number, head_sha):
        self.calls.append(("pr-merged", pr_number, head_sha))

    def on_pr_comment(self, pr_number, comment_id):
        self.calls.append(("pr-comment", pr_number, comment_id))

    def on_main_broken(self, pr_number):
        self.calls.append(("main-broken", pr_number))


class SpyLoop:
    def __init__(self):
        self.calls = []

    def wake(self):
        self.calls.append("wake")

    def backfill(self):
        self.calls.append("backfill")


class SpyInstaller:
    def __init__(self):
        self.calls = []

    def on_install(self):
        self.calls.append("install")


def _route(handler=None, loop=None, installer=None):
    return functools.partial(
        route_webhook,
        handler=handler or SpyMergeHandler(),
        loop=loop or SpyLoop(),
        installer=installer,
    )


def test_receiver_routes_pr_merged_through_route_webhook(running_server):
    handler = SpyMergeHandler()
    server = running_server(_route(handler=handler))

    status = server.post(
        "pull_request",
        {"action": "closed", "pull_request": {"number": 5, "merged": True, "head": {"sha": "abc"}}},
    )

    assert status == 200
    assert _wait_until(lambda: handler.calls != [])
    assert handler.calls == [("pr-merged", 5, "abc")]


def test_receiver_routes_pr_comment(running_server):
    handler = SpyMergeHandler()
    server = running_server(_route(handler=handler))

    status = server.post(
        "issue_comment",
        {
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "..."}},
            "comment": {"id": 33},
        },
    )

    assert status == 200
    assert _wait_until(lambda: handler.calls != [])
    assert handler.calls == [("pr-comment", 7, 33)]


def test_receiver_routes_repository_dispatch_rollback(running_server):
    handler = SpyMergeHandler()
    server = running_server(_route(handler=handler))

    status = server.post(
        "repository_dispatch",
        {"action": "aorc-main-broken", "client_payload": {"pr_number": 9}},
    )

    assert status == 200
    assert _wait_until(lambda: handler.calls != [])
    assert handler.calls == [("main-broken", 9)]


def test_receiver_routes_issue_backfill(running_server):
    loop = SpyLoop()
    server = running_server(_route(loop=loop))

    status = server.post("issues", {"action": "opened", "issue": {"number": 3}})

    assert status == 200
    assert _wait_until(lambda: loop.calls != [])
    assert loop.calls == ["backfill"]


def test_receiver_routes_install(running_server):
    installer = SpyInstaller()
    server = running_server(_route(installer=installer))

    status = server.post("installation", {"action": "created"})

    assert status == 200
    assert _wait_until(lambda: installer.calls != [])
    assert installer.calls == ["install"]


# --------------------------------------------------------------------------- #
# End-to-end dedup: S16 claim_event exercised through the receiver path
# --------------------------------------------------------------------------- #


class CountingMinter:
    def __call__(self, private_key, repo, permissions):
        return "ghs_" + "a" * 36


class FakeWorktrees:
    def ensure(self, issue_number: int) -> str:
        return f"/worktrees/issue-{issue_number}"


def _real_merge_handler():
    issue = Issue(number=7, labels=["in-review"], body="issue body")
    pr = PullRequest(number=1001, head=branch_name(7), merged=True, state="closed", files=["src/a.py"])
    gh = MockGitHubClient(issues=[issue], pulls=[pr])
    runtime = MockContainerRuntime()
    broker = CredentialBroker(PRIVATE_KEY, CountingMinter(), llm_api_key="sk-" + "p" * 20)
    loop = WakeLoop.compose(gh, runtime, FakeWorktrees(), broker, repo="acme/widget")
    gitops = MockGitOps()
    handler = MergeTimeHandler(loop, gitops)
    return handler, gh


def test_duplicate_pr_merged_delivery_is_a_noop_end_to_end(running_server):
    handler, gh = _real_merge_handler()
    server = running_server(_route(handler=handler, loop=handler._loop))
    payload = {
        "action": "closed",
        "pull_request": {"number": 1001, "merged": True, "head": {"sha": "sha1"}},
    }

    first = server.post("pull_request", payload)
    assert first == 200
    assert _wait_until(lambda: gh.issues[7].state == "closed")

    second = server.post("pull_request", payload)
    assert second == 200
    time.sleep(0.2)  # second delivery is a no-op: nothing to poll-wait *for*

    assert gh.issues[7].state == "closed"
    assert gh.board[7] == DONE_COLUMN
    assert gh.calls.count(("delete_branch", branch_name(7))) == 1
