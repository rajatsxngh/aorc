"""S13 -- Cost + compute guards: per-issue/per-run/daily cost circuit
breakers, the 1.5x single-stage overshoot hard-stop, and the container
wall-clock kill."""

from __future__ import annotations

from aorc.github.mock import MockGitHubClient
from aorc.guards import (
    BLOCKED_LABEL,
    ComputeGuard,
    CostGuard,
    CostTotals,
)
from aorc.interfaces import Issue


def _github(number=1):
    return MockGitHubClient(issues=[Issue(number=number, title="t", body="b")])


def test_continues_below_all_caps():
    github = _github()
    guard = CostGuard(github, per_issue_cap=5.0, per_run_cap=50.0, daily_cap=100.0)
    totals = CostTotals()

    verdict = guard.record(1, 1.0, totals)

    assert verdict.action == "continue"
    assert verdict.overshoot is False
    assert github.issues[1].labels == []


def test_per_issue_cap_trip_blocks_issue_with_cost_cap_reason():
    github = _github()
    guard = CostGuard(github, per_issue_cap=5.0, per_run_cap=50.0, daily_cap=100.0)
    totals = CostTotals()

    verdict = guard.record(1, 5.0, totals)

    assert verdict.action == "block-issue"
    assert verdict.overshoot is False
    assert BLOCKED_LABEL in github.issues[1].labels
    comments = github.list_comments(1)
    assert len(comments) == 1
    assert "cost cap" in comments[0].body.lower()


def test_single_stage_overshoot_hard_stops_at_1_5x_per_issue_cap():
    github = _github()
    guard = CostGuard(github, per_issue_cap=4.0, per_run_cap=50.0, daily_cap=100.0)
    totals = CostTotals()

    verdict = guard.record(1, 6.0, totals)  # 6.0 >= 4.0 * 1.5

    assert verdict.action == "block-issue"
    assert verdict.overshoot is True
    assert BLOCKED_LABEL in github.issues[1].labels


def test_per_issue_cap_hit_without_overshoot_is_not_flagged_as_overshoot():
    github = _github()
    guard = CostGuard(github, per_issue_cap=4.0, per_run_cap=50.0, daily_cap=100.0)
    totals = CostTotals()

    verdict = guard.record(1, 4.5, totals)  # over cap, under 1.5x (6.0)

    assert verdict.action == "block-issue"
    assert verdict.overshoot is False


def test_per_run_cap_trip_pauses_and_blocks_tripping_issue():
    github = _github()
    guard = CostGuard(github, per_issue_cap=5.0, per_run_cap=10.0, daily_cap=100.0)
    totals = CostTotals()

    verdict = guard.record(1, 10.0, totals)

    assert verdict.action == "pause-run"
    assert BLOCKED_LABEL in github.issues[1].labels


def test_per_run_pause_persists_across_issues_within_same_guard_instance():
    github = MockGitHubClient(issues=[Issue(number=1, title="a"), Issue(number=2, title="b")])
    guard = CostGuard(github, per_issue_cap=5.0, per_run_cap=10.0, daily_cap=100.0)

    first = guard.record(1, 10.0, CostTotals())
    assert first.action == "pause-run"

    # A different issue, tiny spend -- the run is already paused, so it
    # should still come back paused/blocked rather than "continue".
    second = guard.record(2, 0.10, CostTotals())
    assert second.action == "pause-run"
    assert BLOCKED_LABEL in github.issues[2].labels


def test_daily_cap_trip_halts_and_blocks_tripping_issue():
    github = _github()
    guard = CostGuard(github, per_issue_cap=5.0, per_run_cap=50.0, daily_cap=20.0)
    totals = CostTotals()

    verdict = guard.record(1, 20.0, totals)

    assert verdict.action == "halt-daily"
    assert BLOCKED_LABEL in github.issues[1].labels


def test_daily_cap_takes_priority_over_lower_thresholds():
    github = _github()
    guard = CostGuard(github, per_issue_cap=1.0, per_run_cap=1.0, daily_cap=5.0)
    totals = CostTotals()

    verdict = guard.record(1, 5.0, totals)

    assert verdict.action == "halt-daily"


def test_compute_guard_continues_under_wall_clock_limit():
    guard = ComputeGuard(wall_clock_minutes=30.0)
    assert guard.check(20.0).action == "continue"


def test_compute_guard_kills_at_wall_clock_limit():
    guard = ComputeGuard(wall_clock_minutes=30.0)
    verdict = guard.check(30.0)
    assert verdict.action == "kill"
