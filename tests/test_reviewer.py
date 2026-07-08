"""S8 -- Reviewer + coverage + smoke + PR open."""

from __future__ import annotations

import json

from aorc.coder import CoderStage
from aorc.design import DesignDoc
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.pipeline import branch_name
from aorc.reviewer import (
    ReviewerStage as Stage,
    coverage_gate,
    parse_coverage_percent,
    parse_reviewer_response,
)
from aorc.tester import MockTestRunner, TestRunResult as RunResult

_DESIGN = DesignDoc(
    interface=[{"name": "add", "inputs": ["a", "b"], "outputs": "int"}],
    test_specs=["add(1, 2) == 3"],
    task_list=["implement add()"],
    files=["src/aorc/add.py"],
    confidence=0.9,
)

_ONE_TASK = json.dumps(
    {"tasks": [{"task": "implement add()", "path": "src/aorc/add.py", "code": "def add(a, b):\n    return a + b\n"}]}
)
_APPROVE = json.dumps({"verdict": "approve", "reason": "matches design"})
_REJECT = json.dumps({"verdict": "reject", "reason": "missing edge case"})


def _stage(reviewer_responses, coder_responses=None, test_results=None, gh=None, **kwargs):
    reviewer_llm = MockLLMClient(responses=reviewer_responses)
    coder_llm = MockLLMClient(responses=coder_responses or [])
    gh = gh or MockGitHubClient(issues=[Issue(number=1)])
    # S29: branch creation is the driver's job, before any stage runs.
    gh.create_branch(branch_name(1))
    runner = MockTestRunner(results=test_results)
    coder = CoderStage(coder_llm, gh, runner)
    stage = Stage(reviewer_llm, coder, gh, runner, **kwargs)
    return stage, reviewer_llm, coder_llm, gh, runner


# ---- pure parsing/gating functions --------------------------------------- #


def test_parse_reviewer_response_valid():
    assert parse_reviewer_response(_APPROVE).verdict == "approve"
    assert parse_reviewer_response(_REJECT).verdict == "reject"


def test_parse_reviewer_response_invalid_verdict_is_format_miss():
    assert parse_reviewer_response(json.dumps({"verdict": "maybe"})) is None


def test_parse_reviewer_response_invalid_json_is_format_miss():
    assert parse_reviewer_response("not json") is None


def test_parse_coverage_percent_takes_last_match():
    assert parse_coverage_percent("TOTAL     120     20    83%") == 83.0
    assert parse_coverage_percent("Diff coverage: 91.5%") == 91.5
    assert parse_coverage_percent("no numbers here") is None


def test_coverage_gate():
    assert coverage_gate(83.0, 80.0) is True
    assert coverage_gate(70.0, 80.0) is False
    assert coverage_gate(None, 80.0) is False


# ---- ReviewerStage end-to-end -------------------------------------------- #


def test_happy_path_approves_and_opens_pr():
    stage, reviewer_llm, coder_llm, gh, runner = _stage([_APPROVE])
    gh.add_file(branch_name(1), "src/aorc/add.py", "def add(a, b):\n    return a + b\n")

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "proceed"
    assert result.attempts == 1
    assert result.pr is not None
    assert result.pr.head == branch_name(1)
    prs = gh.list_pull_requests(state="open")
    assert len(prs) == 1 and prs[0].number == result.pr.number
    posted = gh.list_comments(result.pr.number)
    assert any("approve" in c.body for c in posted)


def test_reviewer_and_coder_are_distinct_llm_instances():
    stage, reviewer_llm, coder_llm, gh, runner = _stage([_APPROVE])
    assert reviewer_llm is not coder_llm


def test_reviewer_rejects_then_coder_fixes_then_approves():
    stage, reviewer_llm, coder_llm, gh, runner = _stage(
        [_REJECT, _APPROVE],
        coder_responses=[_ONE_TASK],
        test_results=[RunResult(returncode=0)],
    )

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "proceed"
    assert result.attempts == 2
    assert len(reviewer_llm.calls) == 2
    assert len(coder_llm.calls) == 1
    committed = gh.get_file("src/aorc/add.py", branch_name(1))
    assert committed is not None and "return a + b" in committed
    posted = [c.body for c in gh.list_comments(result.pr.number)]
    assert any("reject" in b for b in posted)
    assert any("approve" in b for b in posted)


def test_reviewer_format_miss_retries_without_touching_coder():
    stage, reviewer_llm, coder_llm, gh, runner = _stage(["garbage", _APPROVE])

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "proceed"
    assert result.attempts == 2
    assert len(coder_llm.calls) == 0


def test_persistent_rejection_exhausts_to_agent_blocked():
    stage, reviewer_llm, coder_llm, gh, runner = _stage(
        [_REJECT, _REJECT],
        coder_responses=[_ONE_TASK, _ONE_TASK],
        test_results=[RunResult(returncode=0), RunResult(returncode=0)],
        max_retries=2,
    )

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "agent-blocked"
    assert result.attempts == 2
    assert result.pr is None


def test_coder_fix_loop_exhaustion_blocks_reviewer_immediately():
    stage, reviewer_llm, coder_llm, gh, runner = _stage(
        [_REJECT],
        coder_responses=["garbage", "garbage", "garbage"],
        max_retries=3,
    )

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "agent-blocked"
    assert result.attempts == 1
    assert result.pr is None


def test_smoke_gate_failure_triggers_fix_then_reapproves():
    stage, reviewer_llm, coder_llm, gh, runner = _stage(
        [_APPROVE],
        coder_responses=[_ONE_TASK],
        test_results=[
            RunResult(returncode=1, stdout="smoke mismatch"),  # smoke fails
            RunResult(returncode=0),  # coder fix toolchain passes
            RunResult(returncode=0),  # smoke passes on retry
        ],
        smoke_command="run_app {input} {expect}",
        smoke_examples=[{"input": "in.yml", "expect": "out.sql"}],
    )

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "proceed"
    assert result.attempts == 2
    assert len(reviewer_llm.calls) == 1  # reviewer only consulted once gates pass
    assert runner.calls[0] == (".", "run_app in.yml out.sql")


def test_missing_smoke_command_skips_smoke_gate():
    stage, reviewer_llm, coder_llm, gh, runner = _stage(
        [_APPROVE], smoke_examples=[{"input": "in.yml", "expect": "out.sql"}]
    )

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "proceed"
    assert runner.calls == []  # nothing to run without a smoke_command template


def test_coverage_gate_failure_triggers_fix_then_reapproves():
    stage, reviewer_llm, coder_llm, gh, runner = _stage(
        [_APPROVE],
        coder_responses=[_ONE_TASK],
        test_results=[
            RunResult(returncode=0, stdout="TOTAL 100 40 60%"),  # below 80% floor
            RunResult(returncode=0),  # coder fix toolchain passes
            RunResult(returncode=0, stdout="TOTAL 100 5 90%"),  # meets floor
        ],
        coverage_command="coverage run -m pytest && coverage report",
    )

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "proceed"
    assert result.attempts == 2


def test_no_coverage_command_skips_coverage_gate():
    stage, reviewer_llm, coder_llm, gh, runner = _stage([_APPROVE])

    result = stage.run(1, _DESIGN, "issue body")

    assert result.status == "proceed"
    assert runner.calls == []
