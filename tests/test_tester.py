"""S6 -- Tester + test-critic + red/error gate + interface coverage."""

from __future__ import annotations

import json
import os

from aorc.design import DesignDoc
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.pipeline import branch_name
from aorc.tester import (
    MockTestRunner,
    TesterStage as Stage,
    TestRunResult as RunResult,
    classify_test_run,
    interface_coverage_gate,
    parse_critic_response,
    parse_tester_response,
    generated_test_path,
    marker_path,
)

_DESIGN = DesignDoc(
    interface=[{"name": "add", "inputs": ["a", "b"], "outputs": "int"}],
    test_specs=["add(1, 2) == 3"],
    task_list=["implement add()"],
    files=["src/aorc/add.py"],
    confidence=0.9,
)

_ONE_TEST = json.dumps(
    {"tests": [{"task": "implement add()", "code": "def test_add():\n    assert add(1, 2) == 3\n"}]}
)
_MISSING_REFERENCE = json.dumps(
    {"tests": [{"task": "implement add()", "code": "def test_add():\n    assert True\n"}]}
)
_APPROVE = json.dumps({"verdict": "approve", "reason": "matches spec"})
_REJECT = json.dumps({"verdict": "reject", "reason": "off-spec"})


def _stage(tester_responses, critic_responses, test_results=None, **kwargs):
    tester_llm = MockLLMClient(responses=tester_responses)
    critic_llm = MockLLMClient(responses=critic_responses)
    gh = MockGitHubClient(issues=[Issue(number=1)])
    runner = MockTestRunner(results=test_results)
    stage = Stage(tester_llm, critic_llm, gh, runner, **kwargs)
    return stage, tester_llm, critic_llm, gh, runner


# ---- pure parsing/gating functions --------------------------------------- #


def test_parse_tester_response_valid():
    doc = parse_tester_response(_ONE_TEST, _DESIGN.task_list)
    assert doc is not None
    assert "assert add(1, 2) == 3" in doc.code


def test_parse_tester_response_count_mismatch_is_format_miss():
    two_tests = json.dumps({"tests": [{"task": "a", "code": "x"}, {"task": "b", "code": "y"}]})
    assert parse_tester_response(two_tests, _DESIGN.task_list) is None


def test_parse_tester_response_invalid_json_is_format_miss():
    assert parse_tester_response("not json", _DESIGN.task_list) is None


def test_parse_tester_response_empty_code_is_format_miss():
    bad = json.dumps({"tests": [{"task": "implement add()", "code": ""}]})
    assert parse_tester_response(bad, _DESIGN.task_list) is None


def test_parse_critic_response_valid():
    assert parse_critic_response(_APPROVE).verdict == "approve"
    assert parse_critic_response(_REJECT).verdict == "reject"


def test_parse_critic_response_invalid_verdict_is_format_miss():
    bad = json.dumps({"verdict": "maybe"})
    assert parse_critic_response(bad) is None


def test_interface_coverage_gate_true_when_all_referenced():
    doc = parse_tester_response(_ONE_TEST, _DESIGN.task_list)
    assert interface_coverage_gate(_DESIGN.interface, doc.code) is True


def test_interface_coverage_gate_false_when_design_fn_not_referenced():
    doc = parse_tester_response(_MISSING_REFERENCE, _DESIGN.task_list)
    assert interface_coverage_gate(_DESIGN.interface, doc.code) is False


def test_classify_test_run_green_red_error():
    assert classify_test_run(RunResult(returncode=0)) == "green"
    assert classify_test_run(RunResult(returncode=1, stdout="AssertionError: assert 4 == 3")) == "red"
    assert classify_test_run(RunResult(returncode=2, stdout="ImportError: no module named add")) == "error"


# ---- TesterStage end-to-end ----------------------------------------------- #


def test_happy_path_proceeds_and_commits_tests():
    stage, tester_llm, critic_llm, gh, runner = _stage(
        [_ONE_TEST], [_APPROVE], test_results=[RunResult(returncode=1, stdout="AssertionError")]
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.attempts == 1
    committed = gh.get_file(generated_test_path(1), branch_name(1))
    assert committed is not None and "assert add(1, 2) == 3" in committed
    assert gh.get_file(marker_path(1), branch_name(1)) is not None


def test_tester_never_sees_repo_files_or_implementation():
    stage, tester_llm, critic_llm, gh, runner = _stage(
        [_ONE_TEST], [_APPROVE], test_results=[RunResult(returncode=1, stdout="AssertionError")]
    )

    stage.run(1, _DESIGN)

    sent = "\n".join(m.content for m in tester_llm.calls[0][0])
    assert "src/aorc/add.py" not in sent


def test_tester_and_critic_are_distinct_llm_instances():
    stage, tester_llm, critic_llm, gh, runner = _stage(
        [_ONE_TEST], [_APPROVE], test_results=[RunResult(returncode=1, stdout="AssertionError")]
    )

    stage.run(1, _DESIGN)

    assert len(tester_llm.calls) == 1
    assert len(critic_llm.calls) == 1
    assert tester_llm is not critic_llm


def test_critic_rejects_off_spec_test_then_tester_retries_and_succeeds():
    stage, tester_llm, critic_llm, gh, runner = _stage(
        [_ONE_TEST, _ONE_TEST],
        [_REJECT, _APPROVE],
        test_results=[RunResult(returncode=1, stdout="AssertionError")],
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.attempts == 2
    assert len(tester_llm.calls) == 2
    assert len(critic_llm.calls) == 2


def test_interface_coverage_failure_retries_tester_without_committing():
    stage, tester_llm, critic_llm, gh, runner = _stage(
        [_MISSING_REFERENCE, _MISSING_REFERENCE, _MISSING_REFERENCE], [], max_retries=3
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert len(critic_llm.calls) == 0  # never got past the static gate
    assert gh.get_file(generated_test_path(1), branch_name(1)) is None


def test_format_miss_retries_then_agent_blocked():
    stage, tester_llm, critic_llm, gh, runner = _stage(["garbage", "garbage", "garbage"], [], max_retries=3)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert result.attempts == 3
    assert gh.get_file(generated_test_path(1), branch_name(1)) is None


def test_red_result_proceeds():
    stage, tester_llm, critic_llm, gh, runner = _stage(
        [_ONE_TEST], [_APPROVE], test_results=[RunResult(returncode=1, stdout="AssertionError: boom")]
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"


def test_error_result_retries_then_agent_blocked():
    stage, tester_llm, critic_llm, gh, runner = _stage(
        [_ONE_TEST, _ONE_TEST],
        [_APPROVE, _APPROVE],
        test_results=[
            RunResult(returncode=2, stdout="ImportError: cannot import name 'add'"),
            RunResult(returncode=2, stdout="ImportError: cannot import name 'add'"),
        ],
        max_retries=2,
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert result.attempts == 2
    assert len(runner.calls) == 2


# ---- S22 split-brain fix: commit -> sync -> run ---------------------------- #


class _ReadsFileTestRunner:
    """Same pin as `test_coder.py`'s -- records the on-disk content of the
    just-committed generated test file at the moment the toolchain runs."""

    def __init__(self, path: str, results=None) -> None:
        self._path = path
        self._results = list(results or [])
        self.seen_content: list[str | None] = []

    def run(self, cwd: str, command: str) -> RunResult:
        full_path = os.path.join(cwd, self._path)
        self.seen_content.append(
            open(full_path).read() if os.path.exists(full_path) else None
        )
        return self._results.pop(0) if self._results else RunResult(returncode=0)


def test_committed_test_file_is_visible_to_the_toolchain_before_it_runs(tmp_path):
    runner = _ReadsFileTestRunner(
        generated_test_path(1), results=[RunResult(returncode=1, stdout="AssertionError")]
    )
    stage = Stage(MockLLMClient(responses=[_ONE_TEST]), MockLLMClient(responses=[_APPROVE]), MockGitHubClient(issues=[Issue(number=1)]), runner)

    result = stage.run(1, _DESIGN, cwd=str(tmp_path))

    assert result.status == "proceed"
    assert runner.seen_content == ["def test_add():\n    assert add(1, 2) == 3\n"]
