"""S6 -- Tester + test-critic + red/error gate + interface coverage."""

from __future__ import annotations

import json
import os

from aorc.design import DesignDoc
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.pipeline import branch_name
from aorc.harness import WorktreeManager
from aorc.tester import (
    ContainerTestRunner,
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
    # S29: branch creation is the driver's job, before any stage runs.
    gh.create_branch(branch_name(1))
    runner = MockTestRunner(results=test_results)
    stage = Stage(tester_llm, critic_llm, gh, runner, **kwargs)
    return stage, tester_llm, critic_llm, gh, runner


# ---- pure parsing/gating functions --------------------------------------- #


def test_parse_tester_response_valid():
    doc = parse_tester_response(_ONE_TEST)
    assert doc is not None
    assert "assert add(1, 2) == 3" in doc.code


def test_parse_tester_response_accepts_any_number_of_valid_tests():
    """S32/S36: count checks against the design are gone entirely (the
    task_list parameter is dead) -- the parser enforces shape only;
    coverage is the interface gate's job."""
    two_tests = json.dumps(
        {"tests": [{"spec": "a", "code": "def test_a():\n    assert add(1, 2) == 3\n"},
                   {"spec": "b", "code": "def test_b():\n    assert add(0, 0) == 0\n"}]}
    )
    doc = parse_tester_response(two_tests)
    assert doc is not None
    assert "test_a" in doc.code and "test_b" in doc.code


def test_parse_tester_response_empty_tests_list_is_format_miss():
    assert parse_tester_response(json.dumps({"tests": []})) is None


def test_parse_tester_response_invalid_json_is_format_miss():
    assert parse_tester_response("not json") is None


def test_parse_tester_response_empty_code_is_format_miss():
    bad = json.dumps({"tests": [{"task": "implement add()", "code": ""}]})
    assert parse_tester_response(bad) is None


def test_parse_critic_response_valid():
    assert parse_critic_response(_APPROVE).verdict == "approve"
    assert parse_critic_response(_REJECT).verdict == "reject"


def test_parse_critic_response_invalid_verdict_is_format_miss():
    bad = json.dumps({"verdict": "maybe"})
    assert parse_critic_response(bad) is None


def test_interface_coverage_gate_true_when_all_referenced():
    doc = parse_tester_response(_ONE_TEST)
    assert interface_coverage_gate(_DESIGN.interface, doc.code) is True


def test_interface_coverage_gate_false_when_design_fn_not_referenced():
    doc = parse_tester_response(_MISSING_REFERENCE)
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
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.create_branch(branch_name(1))
    stage = Stage(MockLLMClient(responses=[_ONE_TEST]), MockLLMClient(responses=[_APPROVE]), gh, runner)

    result = stage.run(1, _DESIGN, cwd=str(tmp_path))

    assert result.status == "proceed"
    assert runner.seen_content == ["def test_add():\n    assert add(1, 2) == 3\n"]


# ---- S27: ContainerTestRunner -- docker exec into the issue's container --- #


class _FakeDockerExec:
    def __init__(self, returncode=0, stdout="", stderr="") -> None:
        self.argv: list[str] | None = None
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def __call__(self, argv, **kwargs):
        import subprocess as _subprocess

        self.argv = list(argv)
        return _subprocess.CompletedProcess(argv, self._returncode, self._stdout, self._stderr)


def test_container_test_runner_execs_into_the_issue_container(monkeypatch):
    fake = _FakeDockerExec(returncode=1, stdout="out", stderr="err")
    monkeypatch.setattr("aorc.tester.subprocess.run", fake)
    worktrees = WorktreeManager("/repo", "/worktrees")
    runner = ContainerTestRunner()

    result = runner.run(worktrees.path_for(42), "pytest -q")

    assert fake.argv == ["docker", "exec", "-w", "/workspace", "aorc-issue-42", "sh", "-c", "pytest -q"]
    assert result == RunResult(returncode=1, stdout="out", stderr="err")


def test_container_test_runner_uses_a_custom_workdir(monkeypatch):
    fake = _FakeDockerExec()
    monkeypatch.setattr("aorc.tester.subprocess.run", fake)
    worktrees = WorktreeManager("/repo", "/worktrees")
    runner = ContainerTestRunner(workdir="/other")

    runner.run(worktrees.path_for(9), "true")

    assert fake.argv[:4] == ["docker", "exec", "-w", "/other"]


def test_container_test_runner_rejects_a_cwd_it_cannot_resolve(monkeypatch):
    monkeypatch.setattr("aorc.tester.subprocess.run", _FakeDockerExec())
    runner = ContainerTestRunner()

    try:
        runner.run(".", "pytest -q")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "." in str(exc)


# ---- S31: blocked results carry the gate + detail that failed ------------- #


def test_blocked_by_parse_gate_records_reason_per_attempt():
    stage, *_ = _stage(["not json"] * 3, [])

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert "schema" in result.reason
    assert "attempt 1" in result.reason and "attempt 3" in result.reason
    assert "not json" in result.reason  # the offending response head


def test_blocked_by_interface_coverage_names_the_uncovered_functions():
    stage, *_ = _stage([_MISSING_REFERENCE] * 3, [])

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert "interface" in result.reason
    assert "add" in result.reason


def test_blocked_by_critic_records_the_critic_reason():
    stage, *_ = _stage([_ONE_TEST] * 3, [_REJECT] * 3)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert "critic" in result.reason
    assert "off-spec" in result.reason


def test_blocked_by_classifier_records_verdict_and_output_tail():
    error = RunResult(returncode=2, stdout="ImportError: No module named 'add'")
    stage, *_ = _stage([_ONE_TEST] * 3, [_APPROVE] * 3, test_results=[error] * 3)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert "'error'" in result.reason
    assert "ImportError: No module named 'add'" in result.reason


def test_proceed_leaves_reason_empty():
    red = RunResult(returncode=1, stdout="AssertionError: assert 4 == 3")
    stage, *_ = _stage([_ONE_TEST], [_APPROVE], test_results=[red])

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.reason == ""


# ---- S32: real Claude wraps JSON in markdown code fences ------------------- #


def test_parse_tester_response_accepts_a_fenced_response():
    fenced = f"Here are the tests:\n```json\n{_ONE_TEST}\n```"
    doc = parse_tester_response(fenced)
    assert doc is not None
    assert "assert add(1, 2) == 3" in doc.code


def test_parse_critic_response_accepts_a_fenced_response():
    verdict = parse_critic_response(f"```json\n{_APPROVE}\n```")
    assert verdict is not None
    assert verdict.verdict == "approve"


def test_stage_proceeds_end_to_end_with_fenced_llm_responses():
    fenced_test = f"Sure!\n```json\n{_ONE_TEST}\n```"
    fenced_approve = f"```json\n{_APPROVE}\n```"
    red = RunResult(returncode=1, stdout="AssertionError: assert 4 == 3")
    stage, *_ = _stage([fenced_test], [fenced_approve], test_results=[red])

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.attempts == 1


# ---- S36: tester keyed to test_specs; critic feedback drives the retry ----- #


def test_tester_prompt_carries_test_specs_and_interface_but_never_task_list():
    """The tester's contract is the design's observable behaviors
    (test_specs) + callable surface (interface). task_list is the coder's
    implementation plan -- sending it made the tester write file-plumbing
    tests the critic then (correctly) rejected, forever."""
    red = RunResult(returncode=1, stdout="AssertionError: assert 4 == 3")
    stage, tester_llm, *_ = _stage([_ONE_TEST], [_APPROVE], test_results=[red])

    stage.run(1, _DESIGN)

    messages, _ = tester_llm.calls[0]
    system, user = messages[0].content, messages[1].content
    assert "task_list" not in system
    assert "test_specs" in system
    assert "task_list" not in user
    assert "implement add()" not in user  # the task_list entry's text
    assert "add(1, 2) == 3" in user  # the test_specs entry's text


def test_critic_rejection_reason_feeds_the_next_tester_attempt():
    """S36: retries were blind -- identical prompt in, identical tests out,
    identical rejection, 3x. The critic's reason now rides the retry
    prompt, same pattern as the coder's failure slot."""
    red = RunResult(returncode=1, stdout="AssertionError: assert 4 == 3")
    stage, tester_llm, *_ = _stage(
        [_ONE_TEST, _ONE_TEST], [_REJECT, _APPROVE], test_results=[red]
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.attempts == 2
    first_user = tester_llm.calls[0][0][1].content
    second_user = tester_llm.calls[1][0][1].content
    assert "off-spec" not in first_user  # _REJECT's reason
    assert "off-spec" in second_user


# ---- S33: a docker exec infra failure is never a test outcome -------------- #

_NOT_RUNNING = RunResult(
    returncode=1,
    stderr="Error response from daemon: container aorc-issue-1 is not running",
)
_NO_SUCH = RunResult(
    returncode=1,
    stderr="Error response from daemon: No such container: aorc-issue-1",
)


def test_classify_container_not_running_is_infra_fail_not_red():
    assert classify_test_run(_NOT_RUNNING) == "infra-fail"


def test_classify_no_such_container_is_infra_fail_not_red():
    assert classify_test_run(_NO_SUCH) == "infra-fail"


def test_classify_plain_assertion_failure_is_still_red():
    assert classify_test_run(RunResult(returncode=1, stdout="AssertionError")) == "red"


def test_stage_hard_fails_immediately_on_infra_failure():
    """No LLM retries: a dead exec target cannot be fixed by regenerating
    tests, and before S33 this exact output classified as 'red' and made
    the tester falsely proceed."""
    stage, tester_llm, *_ = _stage(
        [_ONE_TEST] * 3, [_APPROVE] * 3, test_results=[_NOT_RUNNING] * 3
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert result.attempts == 1
    assert len(tester_llm.calls) == 1
    assert "is not running" in result.reason
    assert "infra" in result.reason
