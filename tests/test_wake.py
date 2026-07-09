"""S16 -- Liveness & idempotency: the stateless orchestrator wake loop."""

from __future__ import annotations

import pytest

from aorc.credentials import (
    GITHUB_TOKEN_ENV,
    LLM_API_KEY_ENV,
    CredentialBroker,
    CredentialLeakError,
    ScrubbingGitHubClient,
)
from aorc.github.mock import MockGitHubClient
from aorc.harness import ContainerHarness, MockContainerRuntime
from aorc.interfaces import Issue, PullRequest
from aorc.pipeline import HELD_LABEL, LABEL_COLUMN, branch_name
from aorc.llm.mock import MockLLMClient
from aorc.wake import (
    WakeLoop,
    claim_event,
    event_marker,
    issue_for_branch,
    rebuild_state,
    should_run_stage,
    stage_artifact_present,
)

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEfakefakefakefakefakefakefake\n"
    "-----END RSA PRIVATE KEY-----"
)


class CountingMinter:
    """Each mint yields a distinct token so re-mints are observable."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, private_key: str, repo: str, permissions: dict) -> str:
        self.calls.append((repo, dict(permissions)))
        return "ghs_" + f"{len(self.calls):04d}" + "a" * 32


class FakeWorktrees:
    """Duck-typed stand-in for `WorktreeManager` -- wake tests exercise the
    dispatch seam, not git plumbing (covered by test_harness.py)."""

    def ensure(self, issue_number: int) -> str:
        return f"/worktrees/issue-{issue_number}"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_loop(issues=None, pulls=None, *, llm=None, concurrency=5):
    gh = MockGitHubClient(issues=issues, pulls=pulls)
    runtime = MockContainerRuntime()
    minter = CountingMinter()
    clock = Clock()
    broker = CredentialBroker(
        PRIVATE_KEY, minter, llm_api_key="sk-" + "p" * 20, clock=clock
    )
    loop = WakeLoop.compose(
        gh,
        runtime,
        FakeWorktrees(),
        broker,
        repo="acme/widget",
        llm=llm,
        concurrency=concurrency,
        clock=clock,
    )
    return loop, gh, runtime, minter, clock


# --------------------------------------------------------------------------- #
# Webhook double-fire dedup: (issue_number, stage, head_sha) marker
# --------------------------------------------------------------------------- #


def test_claim_event_records_marker_and_returns_true_first_time():
    gh = MockGitHubClient(issues=[Issue(number=7)])

    assert claim_event(gh, 7, "in-design", "abc123") is True

    bodies = [c.body for c in gh.list_comments(7)]
    assert any(event_marker(7, "in-design", "abc123") in b for b in bodies)


def test_duplicate_event_tuple_is_a_noop():
    gh = MockGitHubClient(issues=[Issue(number=7)])

    assert claim_event(gh, 7, "in-design", "abc123") is True
    assert claim_event(gh, 7, "in-design", "abc123") is False
    # the duplicate recorded nothing new
    assert len(gh.list_comments(7)) == 1


def test_same_stage_new_head_sha_is_a_new_event():
    gh = MockGitHubClient(issues=[Issue(number=7)])

    assert claim_event(gh, 7, "in-design", "abc123") is True
    assert claim_event(gh, 7, "in-design", "def456") is True


# --------------------------------------------------------------------------- #
# Artifact-presence check: catches the race the dedup key can't
# --------------------------------------------------------------------------- #


def test_stage_artifact_present_when_design_doc_committed():
    gh = MockGitHubClient(issues=[Issue(number=7)])
    gh.add_file(branch_name(7), "aorc/issue-7/design.md", "# design")

    assert stage_artifact_present(gh, 7, "in-design") is True
    assert stage_artifact_present(gh, 7, "in-test") is False


def test_should_run_stage_noops_when_artifact_already_on_branch():
    # The pre-commit race: a second container spun up, but the first already
    # committed the stage artifact -- wasteful, never wrong.
    gh = MockGitHubClient(issues=[Issue(number=7)])
    gh.add_file(branch_name(7), "aorc/issue-7/design.md", "# design")

    assert should_run_stage(gh, 7, "in-design", "abc123") is False


def test_should_run_stage_true_for_fresh_event_and_absent_artifact():
    gh = MockGitHubClient(issues=[Issue(number=7)])

    assert should_run_stage(gh, 7, "in-design", "abc123") is True
    # the same webhook delivered twice -> second is a no-op
    assert should_run_stage(gh, 7, "in-design", "abc123") is False


# --------------------------------------------------------------------------- #
# Stateless rebuild: GitHub is the single source of truth, every wake
# --------------------------------------------------------------------------- #


def test_rebuild_state_partitions_open_issues():
    gh = MockGitHubClient(
        issues=[
            Issue(number=1, labels=[HELD_LABEL]),
            Issue(number=2, labels=["in-design"]),
            Issue(number=3),
            Issue(number=4, state="closed", labels=[HELD_LABEL]),
        ]
    )

    state = rebuild_state(gh)

    assert [i.number for i in state.held] == [1]
    assert [i.number for i in state.in_pipeline] == [2]
    assert [i.number for i in state.backlog] == [3]


def test_state_is_reread_from_github_on_every_wake():
    # Nothing is cached between wakes: a held label added externally after the
    # first tick is acted on by the second.
    loop, gh, runtime, minter, clock = make_loop(
        issues=[Issue(number=5, body="Do the thing.")]
    )

    loop.cron_tick()
    assert runtime.calls == []

    gh.issues[5].labels.append(HELD_LABEL)  # external change between wakes
    loop.cron_tick()

    assert ("start", 5, branch_name(5)) in runtime.calls


def test_cron_tick_noops_on_empty_queue():
    loop, gh, runtime, minter, clock = make_loop(
        issues=[Issue(number=3, labels=["in-design"], body="busy")]
    )

    report = loop.cron_tick()

    assert report.released == [] and report.requeued == []
    assert runtime.calls == [] and minter.calls == [] and gh.calls == []


# --------------------------------------------------------------------------- #
# Held-issue wake: PR-merged webhook (primary) + cron backstop
# --------------------------------------------------------------------------- #


def test_pr_merged_sweeps_held_queue_and_redispatches():
    merged = PullRequest(number=100, head=branch_name(9), state="closed", merged=True)
    loop, gh, runtime, minter, clock = make_loop(
        issues=[
            Issue(number=9, body="Merged."),
            Issue(number=5, labels=[HELD_LABEL], body="Waiting on the merge."),
        ],
        pulls=[merged],
    )

    report = loop.on_pr_merged(100, head_sha="abc123")

    assert report.released == [5]
    assert HELD_LABEL not in gh.issues[5].labels
    assert ("start", 5, branch_name(5)) in runtime.calls


def test_duplicate_pr_merged_webhook_is_a_noop():
    merged = PullRequest(number=100, head=branch_name(9), state="closed", merged=True)
    loop, gh, runtime, minter, clock = make_loop(
        issues=[
            Issue(number=9, body="Merged."),
            Issue(number=5, labels=[HELD_LABEL], body="Waiting."),
        ],
        pulls=[merged],
    )

    loop.on_pr_merged(100, head_sha="abc123")
    starts_after_first = list(runtime.calls)

    report = loop.on_pr_merged(100, head_sha="abc123")

    assert report.duplicate is True
    assert runtime.calls == starts_after_first  # second delivery did nothing


def test_held_issue_with_open_declared_blocker_stays_held():
    loop, gh, runtime, minter, clock = make_loop(
        issues=[
            Issue(number=2, body="The blocker.", state="open"),
            Issue(number=5, labels=[HELD_LABEL], body="blocked by #2"),
        ]
    )

    report = loop.cron_tick()

    assert report.released == []
    assert HELD_LABEL in gh.issues[5].labels
    assert runtime.calls == []


def test_cron_backstop_releases_issue_after_blocker_closes():
    # The dropped-webhook scenario: no merge event ever arrives, but the cron
    # re-evaluates and un-starves the issue.
    loop, gh, runtime, minter, clock = make_loop(
        issues=[
            Issue(number=2, body="The blocker.", state="open"),
            Issue(number=5, labels=[HELD_LABEL], body="blocked by #2"),
        ]
    )
    loop.cron_tick()
    assert runtime.calls == []

    gh.issues[2].state = "closed"
    report = loop.cron_tick()

    assert report.released == [5]
    assert ("start", 5, branch_name(5)) in runtime.calls


def test_hold_labels_and_moves_issue_to_blocked_column():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])

    loop.hold(5)

    assert HELD_LABEL in gh.issues[5].labels
    assert gh.board[5] == LABEL_COLUMN[HELD_LABEL]


# --------------------------------------------------------------------------- #
# S15 wiring: expiry checked for every in-flight container on every wake
# --------------------------------------------------------------------------- #


def test_wake_requeues_expired_container_with_fresh_token():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])
    loop.dispatch_issue(5)
    first_token = loop.in_flight[5][1]

    clock.now += 3600.0  # past the ~1h TTL
    report = loop.cron_tick()

    assert report.requeued == [5]
    # torn down through the branch-preserving path, then re-dispatched
    assert ("teardown", 5) in runtime.calls
    assert ("delete_branch", branch_name(5)) not in gh.calls
    assert runtime.calls.count(("start", 5, branch_name(5))) == 2
    # freshly minted, not refreshed: two mints, distinct tokens, and the
    # expired token object itself was never touched
    assert len(minter.calls) == 2
    new_token = loop.in_flight[5][1]
    assert new_token is not first_token
    assert new_token.token != first_token.token
    assert first_token.expired(clock.now)
    # the fresh token is what the new container received
    assert runtime.envs[5][GITHUB_TOKEN_ENV] == new_token.token


def test_wake_leaves_unexpired_containers_alone():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])
    loop.dispatch_issue(5)
    handle, token = loop.in_flight[5]

    clock.now += 60.0
    report = loop.cron_tick()

    assert report.requeued == []
    assert ("teardown", 5) not in runtime.calls
    assert loop.in_flight[5] == (handle, token)
    assert len(minter.calls) == 1


# --------------------------------------------------------------------------- #
# S15 wiring: the broker is the only source of container env
# --------------------------------------------------------------------------- #


def test_harness_dispatch_rejects_key_shaped_env(tmp_path):
    gh = MockGitHubClient()
    runtime = MockContainerRuntime()
    harness = ContainerHarness(runtime, FakeWorktrees(), gh)

    with pytest.raises(CredentialLeakError):
        harness.dispatch(5, {"GITHUB_TOKEN": PRIVATE_KEY})

    assert runtime.calls == []  # rejected before the runtime ever saw it


def test_wake_loop_dispatch_env_is_broker_built():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])

    loop.dispatch_issue(5)

    token = loop.in_flight[5][1]
    assert runtime.envs[5] == {
        GITHUB_TOKEN_ENV: token.token,
        LLM_API_KEY_ENV: "sk-" + "p" * 20,
    }


# --------------------------------------------------------------------------- #
# S22: dispatch_issue runs the configured build-pipeline driver
# --------------------------------------------------------------------------- #


class RecordingDriver:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run(self, issue_number: int, **kwargs) -> None:
        self.calls.append(issue_number)


def test_dispatch_issue_has_no_driver_by_default():
    """Every hand-assembled/test loop reproduces the exact pre-S22
    behavior: mint + start a container, nothing else."""
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])

    loop.dispatch_issue(5)

    assert loop.driver is None
    assert ("start", 5, branch_name(5)) in runtime.calls


def test_dispatch_issue_runs_a_configured_driver():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])
    driver = RecordingDriver()
    loop.driver = driver

    loop.dispatch_issue(5)

    assert driver.calls == [5]


# --------------------------------------------------------------------------- #
# S15 wiring: composition root wraps the client in ScrubbingGitHubClient once
# --------------------------------------------------------------------------- #


def test_compose_wraps_client_once_and_harness_shares_it():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])

    assert isinstance(loop.github, ScrubbingGitHubClient)
    assert loop.harness._github is loop.github
    # writes through any wake-loop path arrive scrubbed
    loop.github.post_comment(5, "leaked ghp_" + "b" * 36)
    assert "ghp_" not in gh.comments[5][-1].body


def test_init_does_not_double_wrap_an_already_scrubbing_client():
    gh = MockGitHubClient()
    wrapped = ScrubbingGitHubClient(gh)
    runtime = MockContainerRuntime()
    broker = CredentialBroker(PRIVATE_KEY, CountingMinter(), clock=Clock())
    harness = ContainerHarness(runtime, FakeWorktrees(), wrapped)

    loop = WakeLoop(wrapped, harness, broker, repo="acme/widget")

    assert loop.github is wrapped


# --------------------------------------------------------------------------- #
# First-run backfill = re-sync
# --------------------------------------------------------------------------- #


def test_backfill_triages_all_open_issues_and_dispatches_five_at_a_time():
    issues = [Issue(number=n, body=f"Task {n}.") for n in range(1, 8)]  # 7 actionable
    issues.append(Issue(number=8, body=""))  # empty body -> not-ready, never LLM'd
    llm = MockLLMClient(default="actionable")
    loop, gh, runtime, minter, clock = make_loop(issues=issues, llm=llm)

    report = loop.backfill()

    assert report.dispatched == [1, 2, 3, 4, 5]
    assert report.queued == [6, 7]
    assert sorted(report.triaged) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert len([c for c in runtime.calls if c[0] == "start"]) == 5


def test_backfill_skips_issues_already_in_the_pipeline():
    issues = [
        Issue(number=1, labels=["in-code"], body="Mid-pipeline."),
        Issue(number=2, labels=[HELD_LABEL], body="Already held."),
        Issue(number=3, body="Fresh."),
    ]
    llm = MockLLMClient(default="actionable")
    loop, gh, runtime, minter, clock = make_loop(issues=issues, llm=llm)

    report = loop.backfill()

    assert report.triaged == [3]
    assert report.dispatched == [3]
    assert len(llm.calls) == 1  # in-flow issues never re-enter triage


def test_backfill_holds_declared_blocked_issues():
    issues = [
        Issue(number=2, body="The blocker.", state="open"),
        Issue(number=5, body="blocked by #2"),
    ]
    llm = MockLLMClient(default="actionable")
    loop, gh, runtime, minter, clock = make_loop(issues=issues, llm=llm)

    report = loop.backfill()

    assert 5 in report.held
    assert HELD_LABEL in gh.issues[5].labels
    assert gh.board[5] == LABEL_COLUMN[HELD_LABEL]


def test_backfill_without_an_llm_fails_loudly():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=1, body="x")])

    with pytest.raises(ValueError):
        loop.backfill()


def test_backfill_respects_existing_in_flight_count():
    issues = [Issue(number=n, body=f"Task {n}.") for n in range(1, 8)]
    llm = MockLLMClient(default="actionable")
    loop, gh, runtime, minter, clock = make_loop(issues=issues, llm=llm)
    loop.dispatch_issue(1)

    report = loop.backfill()

    # one slot already occupied by the in-flight container
    assert report.dispatched == [2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# Branch <-> issue mapping (deterministic, greppable -- PRD B24)
# --------------------------------------------------------------------------- #


def test_issue_for_branch_roundtrip():
    assert issue_for_branch(branch_name(42)) == 42
    assert issue_for_branch("feature/human-branch") is None


# --------------------------------------------------------------------------- #
# S31: dispatch_issue surfaces the driver's result instead of discarding it
# --------------------------------------------------------------------------- #


class ResultDriver:
    """Driver stub whose `run` returns a real DriverResult, so the loop's
    outcome plumbing (not the driver) is what's under test."""

    def run(self, issue_number: int, **kwargs):
        from aorc.driver import DriverResult

        return DriverResult(
            status="agent-blocked", stage="in-test", reason="attempt 1: boom"
        )


def test_dispatch_issue_returns_the_driver_result_in_its_outcome():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])
    loop.driver = ResultDriver()

    outcome = loop.dispatch_issue(5)

    assert outcome.handle.issue_number == 5
    assert outcome.result is not None
    assert outcome.result.status == "agent-blocked"
    assert outcome.result.stage == "in-test"
    assert outcome.result.reason == "attempt 1: boom"


def test_dispatch_issue_without_driver_returns_an_outcome_with_no_result():
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="x")])

    outcome = loop.dispatch_issue(5)

    assert outcome.handle is not None
    assert outcome.result is None


# --------------------------------------------------------------------------- #
# S43: post-design collision checkpoint wired into the live dispatch path
# --------------------------------------------------------------------------- #


class HeldDriver:
    """Driver stub returning a checkpoint hold, so the loop's held-teardown
    plumbing (not the driver) is what's under test."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def run(self, issue_number: int, **kwargs):
        from aorc.driver import DriverResult

        self.calls.append(issue_number)
        return DriverResult(status="held", stage="checkpoint")


def test_dispatch_issue_tears_down_container_on_held_result():
    """A checkpoint hold must not leave the container in `in_flight` --
    otherwise `_sweep_held` skips the issue forever -- and must go through
    the branch-preserving teardown (never delete_branch)."""
    loop, gh, runtime, minter, clock = make_loop(issues=[Issue(number=5, body="Task.")])
    loop.driver = HeldDriver()

    outcome = loop.dispatch_issue(5)

    assert outcome.result.status == "held"
    assert 5 not in loop.in_flight
    assert ("teardown", 5) in runtime.calls
    assert not any(call[0] == "delete_branch" for call in gh.calls)


import json as _json

from aorc.coder import CoderStage
from aorc.design import DesignStage, design_doc_path
from aorc.driver import PipelineDriver
from aorc.reviewer import ReviewerStage
from aorc.tester import MockTestRunner, TesterStage as TestStage

_DESIGN_SUBTRACT = _json.dumps(
    {
        "interface": [{"name": "subtract", "inputs": ["a", "b"], "outputs": "int"}],
        "test_specs": ["subtract(3, 1) == 2"],
        "task_list": ["implement subtract()"],
        "files": ["math_utils.py"],
        "confidence": 0.9,
    }
)
_DESIGN_POWER = _json.dumps(
    {
        "interface": [{"name": "power", "inputs": ["a", "b"], "outputs": "int"}],
        "test_specs": ["power(2, 3) == 8"],
        "task_list": ["implement power()"],
        "files": ["math_utils.py"],
        "confidence": 0.9,
    }
)


def _checkpointed_driver(loop, design_responses):
    """A real PipelineDriver wired to the loop's shared harness checkpoint --
    the exact live composition. Tester/coder/reviewer LLMs get no responses:
    a held issue must stop before ever reaching them."""
    gh = loop.github
    runner = MockTestRunner(results=[])
    coder = CoderStage(MockLLMClient(responses=[]), gh, runner)
    return PipelineDriver(
        gh,
        FakeWorktrees(),
        DesignStage(MockLLMClient(responses=design_responses), gh),
        TestStage(MockLLMClient(responses=[]), MockLLMClient(responses=[]), gh, runner),
        coder,
        ReviewerStage(MockLLMClient(responses=[]), coder, gh, runner),
        checkpoint=loop.harness.checkpoint,
    )


def test_backfill_rebuilds_registry_and_holds_colliding_new_issue():
    """The live issues-24/25 bug across an orchestrator restart: issue 4 is
    mid-pipeline (committed design doc claims math_utils.py) but this fresh
    process has no in-memory record of it. Backfill must rebuild the
    registry from GitHub before dispatching, so issue 6 -- whose design
    claims the same file -- is held at the checkpoint, not run to review."""
    issues = [
        Issue(number=4, body="add subtract to math_utils", labels=["in-test"]),
        Issue(number=6, body="add power to math_utils"),
    ]
    llm = MockLLMClient(default="actionable")
    loop, gh, runtime, minter, clock = make_loop(issues=issues, llm=llm)
    gh.add_file(branch_name(4), design_doc_path(4), _DESIGN_SUBTRACT)
    loop.driver = _checkpointed_driver(loop, design_responses=[_DESIGN_POWER])

    report = loop.backfill()

    assert report.dispatched == [6]  # the selector still dispatches it...
    assert HELD_LABEL in gh.get_labels(6)  # ...but the checkpoint holds it
    assert 6 not in loop.in_flight
    assert ("teardown", 6) in runtime.calls


def test_wake_sweep_rebuilds_registry_and_reholds_colliding_held_issue():
    """A held issue released by the sweep must re-clear the checkpoint: with
    issue 4 still mid-pipeline claiming math_utils.py, releasing issue 6
    (same file) re-holds it instead of running it to review."""
    issues = [
        Issue(number=4, body="add subtract to math_utils", labels=["in-test"]),
        Issue(number=6, body="add power to math_utils", labels=[HELD_LABEL]),
    ]
    loop, gh, runtime, minter, clock = make_loop(issues=issues)
    gh.add_file(branch_name(4), design_doc_path(4), _DESIGN_SUBTRACT)
    loop.driver = _checkpointed_driver(loop, design_responses=[_DESIGN_POWER])

    report = loop.wake()

    assert report.released == [6]
    assert HELD_LABEL in gh.get_labels(6)
    assert 6 not in loop.in_flight
    assert ("teardown", 6) in runtime.calls


# --------------------------------------------------------------------------- #
# S45: a worktree/main rebase conflict at dispatch maps to agent-blocked
# --------------------------------------------------------------------------- #


def test_dispatch_issue_maps_worktree_sync_conflict_to_agent_blocked():
    """`WorktreeManager.ensure` raises `WorktreeSyncConflict` when the issue
    branch truly conflicts with the freshly-merged main (S45). Dispatch must
    route that like every other hard stop -- label agent-blocked, explain on
    the issue, claim no in-flight slot -- never crash the wake loop and never
    silently run on the stale tree."""
    from aorc.guards import BLOCKED_LABEL
    from aorc.harness import WorktreeSyncConflict

    class ConflictingWorktrees:
        def ensure(self, issue_number: int) -> str:
            raise WorktreeSyncConflict("rebasing aorc/issue-5 onto origin/main conflicts")

    gh = MockGitHubClient(issues=[Issue(number=5)])
    minter = CountingMinter()
    clock = Clock()
    broker = CredentialBroker(PRIVATE_KEY, minter, llm_api_key="sk-" + "p" * 20, clock=clock)
    loop = WakeLoop.compose(
        gh, MockContainerRuntime(), ConflictingWorktrees(), broker, repo="acme/widget", clock=clock
    )

    outcome = loop.dispatch_issue(5)

    assert outcome.handle is None
    assert 5 not in loop.in_flight
    assert BLOCKED_LABEL in gh.issues[5].labels
    comments = [c.body for c in gh.list_comments(5)]
    assert any("conflict" in body for body in comments)
