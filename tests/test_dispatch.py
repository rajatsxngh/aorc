"""S10 -- Dispatch selector: declared blockers + concurrency ceiling."""

from __future__ import annotations

from aorc.dispatch import (
    DEFAULT_CONCURRENCY,
    declared_blockers,
    is_declared_blocked,
    select_dispatch,
)
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue


def test_declared_blockers_parses_blocked_by_marker():
    issue = Issue(number=1, body="Some text.\n\nBlocked by #42 and other stuff.")
    assert declared_blockers(issue) == [42]


def test_declared_blockers_empty_when_no_marker():
    issue = Issue(number=1, body="Nothing special here.")
    assert declared_blockers(issue) == []


def test_is_declared_blocked_true_while_blocker_open():
    gh = MockGitHubClient(issues=[Issue(number=1, body="Blocked by #2"), Issue(number=2)])
    issue = gh.get_issue(1)

    assert is_declared_blocked(issue, gh) is True


def test_is_declared_blocked_false_once_blocker_closed():
    gh = MockGitHubClient(
        issues=[Issue(number=1, body="Blocked by #2"), Issue(number=2, state="closed")]
    )
    issue = gh.get_issue(1)

    assert is_declared_blocked(issue, gh) is False


def test_is_declared_blocked_false_with_no_marker():
    gh = MockGitHubClient(issues=[Issue(number=1, body="Just build the thing.")])
    issue = gh.get_issue(1)

    assert is_declared_blocked(issue, gh) is False


def test_is_declared_blocked_ignores_dangling_blocker_reference():
    gh = MockGitHubClient(issues=[Issue(number=1, body="Blocked by #999")])
    issue = gh.get_issue(1)

    assert is_declared_blocked(issue, gh) is False


def test_epic_is_always_declared_blocked_pre_decomposition():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=["epic"], body="Big project.")])
    issue = gh.get_issue(1)

    assert is_declared_blocked(issue, gh) is True


def test_select_dispatch_default_concurrency_is_five():
    assert DEFAULT_CONCURRENCY == 5


def test_select_dispatch_skips_declared_blocked_issues():
    gh = MockGitHubClient(
        issues=[
            Issue(number=1, body="Blocked by #2"),
            Issue(number=2),
            Issue(number=3),
        ]
    )
    candidates = [gh.get_issue(1), gh.get_issue(2), gh.get_issue(3)]

    decision = select_dispatch(candidates, gh)

    assert decision.blocked == [1]
    assert decision.to_dispatch == [2, 3]
    assert decision.queued == []


def test_select_dispatch_never_dispatches_an_epic():
    gh = MockGitHubClient(issues=[Issue(number=1, labels=["epic"])])
    candidates = [gh.get_issue(1)]

    decision = select_dispatch(candidates, gh)

    assert decision.to_dispatch == []
    assert decision.blocked == [1]


def test_select_dispatch_enforces_concurrency_ceiling_and_queues_overflow():
    gh = MockGitHubClient(issues=[Issue(number=n) for n in range(1, 8)])
    candidates = [gh.get_issue(n) for n in range(1, 8)]

    decision = select_dispatch(candidates, gh, concurrency=5)

    assert decision.to_dispatch == [1, 2, 3, 4, 5]
    assert decision.queued == [6, 7]
    assert decision.blocked == []


def test_select_dispatch_accounts_for_already_in_flight_issues():
    gh = MockGitHubClient(issues=[Issue(number=n) for n in range(1, 5)])
    candidates = [gh.get_issue(n) for n in range(1, 5)]

    decision = select_dispatch(candidates, gh, in_flight=3, concurrency=5)

    assert decision.to_dispatch == [1, 2]
    assert decision.queued == [3, 4]


def test_select_dispatch_zero_capacity_queues_everything_unblocked():
    gh = MockGitHubClient(issues=[Issue(number=1), Issue(number=2)])
    candidates = [gh.get_issue(1), gh.get_issue(2)]

    decision = select_dispatch(candidates, gh, in_flight=5, concurrency=5)

    assert decision.to_dispatch == []
    assert decision.queued == [1, 2]


def test_select_dispatch_blocked_issues_never_occupy_a_queue_slot():
    gh = MockGitHubClient(
        issues=[Issue(number=1, body="Blocked by #99"), Issue(number=2), Issue(number=99)]
    )
    candidates = [gh.get_issue(1), gh.get_issue(2)]

    decision = select_dispatch(candidates, gh, in_flight=0, concurrency=1)

    assert decision.blocked == [1]
    assert decision.to_dispatch == [2]
    assert decision.queued == []
