"""S14 — failure escalation + backoff.

Covers the escalation ladder (primary xN -> escalation xM -> agent-blocked +
detailed comment), the provider-error backoff on a separate counter from bad
output, and the GitHub rate-limit backoff that never counts as a failure.
"""

import pytest

from aorc.config import parse_config
from aorc.escalation import (
    BACKOFF_SCHEDULE,
    AttemptOutcome,
    BackoffLLMClient,
    EscalationLadder,
    FAILURE_MARKER,
    RateLimitedGitHubClient,
)
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import (
    FailFastProviderError,
    GitHubRateLimitError,
    Issue,
    ProviderError,
)
from aorc.llm.mock import MockLLMClient


def _issue(number=1):
    return Issue(number=number, title="t", body="b")


class RecordingSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


# --------------------------------------------------------------------------- #
# BackoffLLMClient: provider-error backoff on the same model
# --------------------------------------------------------------------------- #


def test_backoff_retries_same_model_with_schedule():
    inner = MockLLMClient(
        [ProviderError("429"), ProviderError("500"), ProviderError("timeout"), "ok"],
        model="model-a",
    )
    sleep = RecordingSleep()
    client = BackoffLLMClient(inner, sleep=sleep)

    completion = client.complete([])

    assert completion.text == "ok"
    assert completion.model == "model-a"  # same model, never switched
    assert sleep.delays == list(BACKOFF_SCHEDULE) == [2.0, 8.0, 30.0]
    assert len(inner.calls) == 4  # initial call + one per backoff step


def test_backoff_exhaustion_raises_provider_error():
    inner = MockLLMClient([ProviderError("down")] * 10, model="model-a")
    sleep = RecordingSleep()
    client = BackoffLLMClient(inner, sleep=sleep)

    with pytest.raises(ProviderError):
        client.complete([])

    assert sleep.delays == [2.0, 8.0, 30.0]
    assert len(inner.calls) == 4


def test_fail_fast_provider_error_is_never_retried():
    # Local-LLM constraint: connection-refused to a local base_url on a cloud
    # runner is not transient (S18 raises this) — no backoff, no retry.
    inner = MockLLMClient([FailFastProviderError("local base_url unreachable")])
    sleep = RecordingSleep()
    client = BackoffLLMClient(inner, sleep=sleep)

    with pytest.raises(FailFastProviderError):
        client.complete([])

    assert sleep.delays == []
    assert len(inner.calls) == 1


# --------------------------------------------------------------------------- #
# EscalationLadder: primary xN -> escalation xM -> agent-blocked + comment
# --------------------------------------------------------------------------- #


def _failing_attempt(error="tests still red", test_output="1 failed"):
    def attempt(llm):
        return AttemptOutcome(ok=False, error=error, test_output=test_output)

    return attempt


def test_ladder_success_on_first_attempt_makes_no_github_calls():
    github = MockGitHubClient([_issue()])
    ladder = EscalationLadder(github, primary=MockLLMClient(model="model-a"))

    result = ladder.run(1, lambda llm: AttemptOutcome(ok=True, result="artifact"))

    assert result.status == "success"
    assert result.result == "artifact"
    assert github.calls == []


def test_ladder_primary_exhausts_then_escalation_succeeds():
    github = MockGitHubClient([_issue()])
    models_seen = []

    def attempt(llm):
        models_seen.append(llm.model)
        return AttemptOutcome(ok=llm.model == "model-b")

    ladder = EscalationLadder(
        github,
        primary=MockLLMClient(model="model-a"),
        escalation=MockLLMClient(model="model-b"),
        primary_attempts=3,
        escalation_attempts=1,
    )
    result = ladder.run(1, attempt)

    assert result.status == "success"
    assert models_seen == ["model-a", "model-a", "model-a", "model-b"]
    assert "agent-blocked" not in github.get_labels(1)


def test_ladder_exhaustion_blocks_and_posts_detailed_comment():
    github = MockGitHubClient([_issue()])
    ladder = EscalationLadder(
        github,
        primary=MockLLMClient(model="model-a"),
        escalation=MockLLMClient(model="model-b"),
        primary_attempts=2,
        escalation_attempts=2,
    )
    result = ladder.run(
        1,
        _failing_attempt(
            error="AssertionError: expected 3, got 2\n  full traceback here",
            test_output="=== 1 failed, 4 passed ===",
        ),
    )

    assert result.status == "agent-blocked"
    assert len(result.attempts) == 4
    assert "agent-blocked" in github.get_labels(1)
    assert github.board[1] == "Blocked"

    comment = github.comments[1][-1].body
    assert comment.startswith(FAILURE_MARKER)
    # Full error, last test output, and what was attempted — all present.
    assert "AssertionError: expected 3, got 2" in comment
    assert "full traceback here" in comment
    assert "=== 1 failed, 4 passed ===" in comment
    assert "model-a" in comment and "model-b" in comment


def test_ladder_without_escalation_slot_blocks_after_primary():
    github = MockGitHubClient([_issue()])
    ladder = EscalationLadder(
        github, primary=MockLLMClient(model="model-a"), primary_attempts=2
    )

    result = ladder.run(1, _failing_attempt())

    assert result.status == "agent-blocked"
    assert len(result.attempts) == 2
    assert "agent-blocked" in github.get_labels(1)


def test_provider_blip_does_not_consume_a_ladder_attempt():
    # Separate counters: a transient provider error is absorbed by backoff on
    # the same model; only bad output burns a ladder attempt.
    github = MockGitHubClient([_issue()])
    inner = MockLLMClient([ProviderError("429"), "bad", "bad"], model="model-a")
    sleep = RecordingSleep()
    wrapped = BackoffLLMClient(inner, sleep=sleep)

    def attempt(llm):
        completion = llm.complete([])
        return AttemptOutcome(
            ok=completion.text == "good", error="bad output", test_output="1 failed"
        )

    ladder = EscalationLadder(github, primary=wrapped, primary_attempts=2)
    result = ladder.run(1, attempt)

    assert result.status == "agent-blocked"
    assert len(result.attempts) == 2  # the blip did not count as an attempt
    assert sleep.delays == [2.0]  # backoff absorbed it
    assert len(inner.calls) == 3  # 1 blip + 1 retry + 1 second attempt


def test_backoff_exhaustion_counts_as_exactly_one_ladder_failure():
    github = MockGitHubClient([_issue()])
    calls = []

    def attempt(llm):
        calls.append(llm.model)
        if len(calls) == 1:
            raise ProviderError("provider down after full backoff")
        return AttemptOutcome(ok=True)

    ladder = EscalationLadder(
        github, primary=MockLLMClient(model="model-a"), primary_attempts=3
    )
    result = ladder.run(1, attempt)

    assert result.status == "success"
    assert len(calls) == 2  # exhaustion burned one attempt, next one ran


def test_provider_error_exhausting_all_attempts_reports_it_in_comment():
    github = MockGitHubClient([_issue()])

    def attempt(llm):
        raise ProviderError("connection reset by peer")

    ladder = EscalationLadder(
        github, primary=MockLLMClient(model="model-a"), primary_attempts=2
    )
    result = ladder.run(1, attempt)

    assert result.status == "agent-blocked"
    assert "connection reset by peer" in github.comments[1][-1].body


def test_fail_fast_error_blocks_immediately_without_burning_the_ladder():
    github = MockGitHubClient([_issue()])
    calls = []

    def attempt(llm):
        calls.append(llm.model)
        raise FailFastProviderError("local base_url configured on a cloud runner")

    ladder = EscalationLadder(
        github,
        primary=MockLLMClient(model="model-a"),
        escalation=MockLLMClient(model="model-b"),
        primary_attempts=3,
        escalation_attempts=2,
    )
    result = ladder.run(1, attempt)

    assert result.status == "agent-blocked"
    assert calls == ["model-a"]  # no retries, no escalation grind
    assert "local base_url configured on a cloud runner" in github.comments[1][-1].body


def test_ladder_wires_from_config_slots_no_hardcoded_names():
    cfg = parse_config(
        {
            "llm": {
                "primary": {"provider": "openai", "model": "cfg-primary"},
                "escalation": {"provider": "claude", "model": "cfg-escalation"},
            },
            "failure": {"primary_attempts": 2, "escalation_attempts": 1},
        }
    )
    github = MockGitHubClient([_issue()])
    models_seen = []

    def attempt(llm):
        models_seen.append(llm.model)
        return AttemptOutcome(ok=False, error="red")

    ladder = EscalationLadder(
        github,
        primary=MockLLMClient(model=cfg.primary.model),
        escalation=MockLLMClient(model=cfg.escalation.model),
        primary_attempts=cfg.primary_attempts,
        escalation_attempts=cfg.escalation_attempts,
    )
    ladder.run(1, attempt)

    assert models_seen == ["cfg-primary", "cfg-primary", "cfg-escalation"]


# --------------------------------------------------------------------------- #
# RateLimitedGitHubClient: 403/429 backoff, never a failure
# --------------------------------------------------------------------------- #


class RateLimitingGitHub(MockGitHubClient):
    """Raises the queued exceptions (one per call) before delegating."""

    def __init__(self, *args, errors=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._errors = list(errors or [])

    def post_comment(self, issue_number, body):
        if self._errors:
            raise self._errors.pop(0)
        return super().post_comment(issue_number, body)


class FakeSdkError(Exception):
    """Duck-types PyGithub's GithubException: .status + .headers."""

    def __init__(self, status, headers=None):
        super().__init__(f"http {status}")
        self.status = status
        self.headers = headers or {}


def test_rate_limit_retry_after_honored_then_succeeds():
    inner = RateLimitingGitHub(
        [_issue()], errors=[GitHubRateLimitError("secondary limit", retry_after=12.0)]
    )
    sleep = RecordingSleep()
    client = RateLimitedGitHubClient(inner, sleep=sleep)

    comment = client.post_comment(1, "hello")

    assert comment.body == "hello"
    assert sleep.delays == [12.0]
    assert "agent-blocked" not in inner.get_labels(1)  # never counted as failure


def test_rate_limit_without_retry_after_uses_backoff_schedule():
    inner = RateLimitingGitHub(
        [_issue()], errors=[GitHubRateLimitError("burst"), GitHubRateLimitError("burst")]
    )
    sleep = RecordingSleep()
    client = RateLimitedGitHubClient(inner, sleep=sleep)

    client.post_comment(1, "hello")

    assert sleep.delays == [2.0, 8.0]


def test_sdk_429_and_403_with_retry_after_duck_typed_as_rate_limits():
    inner = RateLimitingGitHub(
        [_issue()],
        errors=[
            FakeSdkError(429, {"Retry-After": "3"}),
            FakeSdkError(403, {"retry-after": "7"}),
        ],
    )
    sleep = RecordingSleep()
    client = RateLimitedGitHubClient(inner, sleep=sleep)

    client.post_comment(1, "hello")

    assert sleep.delays == [3.0, 7.0]
    assert inner.comments[1][-1].body == "hello"


def test_403_without_retry_after_is_not_a_rate_limit():
    # A plain 403 is a permissions failure, not a secondary rate limit — the
    # Retry-After header is the mechanical discriminator. Never retried.
    inner = RateLimitingGitHub([_issue()], errors=[FakeSdkError(403)])
    sleep = RecordingSleep()
    client = RateLimitedGitHubClient(inner, sleep=sleep)

    with pytest.raises(FakeSdkError):
        client.post_comment(1, "hello")

    assert sleep.delays == []


def test_rate_limit_exhaustion_reraises_and_never_blocks():
    inner = RateLimitingGitHub(
        [_issue()], errors=[GitHubRateLimitError("still limited")] * 10
    )
    sleep = RecordingSleep()
    client = RateLimitedGitHubClient(inner, sleep=sleep)

    with pytest.raises(GitHubRateLimitError):
        client.post_comment(1, "hello")

    assert sleep.delays == [2.0, 8.0, 30.0]
    assert "agent-blocked" not in inner.get_labels(1)
    assert not any(c[0] == "add_label" for c in inner.calls)


def test_rate_limited_client_delegates_all_seam_methods():
    # The wrapper is a full GitHubClient: every seam method passes through.
    inner = MockGitHubClient([_issue()])
    client = RateLimitedGitHubClient(inner, sleep=RecordingSleep())

    assert client.get_issue(1).number == 1
    client.add_label(1, "in-progress")
    assert client.get_labels(1) == ["in-progress"]
    client.remove_label(1, "in-progress")
    client.set_labels(1, ["triaged"])
    client.create_label("x")
    pr = client.open_pull_request("t", "b", "head")
    assert client.get_pull_request(pr.number).number == pr.number
    assert client.list_pull_requests() != []
    client.merge_pull_request(pr.number)
    client.delete_branch("head")
    client.create_branch("br")
    client.commit_file("br", "f.py", "content", "msg")
    assert client.get_file("f.py", "br") == "content"
    client.set_board_column(1, "In Progress")
    assert client.get_board_column(1) == "In Progress"
    created = client.create_issue("t2", "b2")
    assert client.list_issues("all")[-1].number == created.number
    client.close_issue(created.number)
    assert client.list_comments(1) == []


# --------------------------------------------------------------------------- #
# ProviderError stays importable from aorc.coder (moved to interfaces in S14)
# --------------------------------------------------------------------------- #


def test_provider_error_shared_between_coder_and_interfaces():
    from aorc.coder import ProviderError as CoderProviderError

    assert CoderProviderError is ProviderError
