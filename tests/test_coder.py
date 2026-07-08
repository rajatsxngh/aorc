"""S7 -- Coder bounded fix loop."""

from __future__ import annotations

import json
import os

from aorc.coder import (
    CoderStage as Stage,
    ProviderError,
    failing_test_summary,
    missing_preserved_names,
    parse_coder_response,
)
from aorc.design import DesignDoc
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.pipeline import branch_name
from aorc.tester import MockTestRunner, TestRunResult as RunResult, generated_test_path

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
_OUT_OF_SCOPE = json.dumps(
    {"tasks": [{"task": "implement add()", "path": "src/aorc/other.py", "code": "x = 1\n"}]}
)


def _stage(responses, test_results=None, **kwargs):
    llm = MockLLMClient(responses=responses)
    gh = MockGitHubClient(issues=[Issue(number=1)])
    # S29: branch creation is the driver's job, before any stage runs.
    gh.create_branch(branch_name(1))
    runner = MockTestRunner(results=test_results)
    stage = Stage(llm, gh, runner, **kwargs)
    return stage, llm, gh, runner


# ---- pure parsing/formatting functions ----------------------------------- #


def test_parse_coder_response_valid():
    doc = parse_coder_response(_ONE_TASK, _DESIGN.task_list, _DESIGN.files)
    assert doc is not None
    assert doc.tasks[0]["path"] == "src/aorc/add.py"


def test_parse_coder_response_path_outside_files_is_format_miss():
    assert parse_coder_response(_OUT_OF_SCOPE, _DESIGN.task_list, _DESIGN.files) is None


def test_parse_coder_response_count_mismatch_is_format_miss():
    two = json.dumps(
        {
            "tasks": [
                {"task": "a", "path": "src/aorc/add.py", "code": "x"},
                {"task": "b", "path": "src/aorc/add.py", "code": "y"},
            ]
        }
    )
    assert parse_coder_response(two, _DESIGN.task_list, _DESIGN.files) is None


def test_parse_coder_response_invalid_json_is_format_miss():
    assert parse_coder_response("not json", _DESIGN.task_list, _DESIGN.files) is None


def test_parse_coder_response_empty_code_is_format_miss():
    bad = json.dumps({"tasks": [{"task": "implement add()", "path": "src/aorc/add.py", "code": ""}]})
    assert parse_coder_response(bad, _DESIGN.task_list, _DESIGN.files) is None


def test_failing_test_summary_has_no_test_source_markers():
    summary = failing_test_summary(RunResult(returncode=1, stdout="AssertionError: assert 4 == 3"))
    assert "result: fail" in summary
    assert "AssertionError" in summary


# ---- CoderStage end-to-end ------------------------------------------------ #


def test_happy_path_proceeds_and_commits_code():
    stage, llm, gh, runner = _stage([_ONE_TASK], test_results=[RunResult(returncode=0)])

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.attempts == 1
    committed = gh.get_file("src/aorc/add.py", branch_name(1))
    assert committed is not None and "return a + b" in committed


def test_executes_task_list_in_order():
    two_task_design = DesignDoc(
        interface=_DESIGN.interface,
        test_specs=_DESIGN.test_specs,
        task_list=["first task", "second task"],
        files=["a.py", "b.py"],
        confidence=0.9,
    )
    response = json.dumps(
        {
            "tasks": [
                {"task": "first task", "path": "a.py", "code": "a = 1\n"},
                {"task": "second task", "path": "b.py", "code": "b = 2\n"},
            ]
        }
    )
    stage, llm, gh, runner = _stage([response], test_results=[RunResult(returncode=0)])

    result = stage.run(1, two_task_design)

    assert result.status == "proceed"
    commit_paths = [call[2] for call in gh.calls if call[0] == "commit_file"]
    assert commit_paths == ["a.py", "b.py"]


def test_coder_never_receives_test_source():
    stage, llm, gh, runner = _stage([_ONE_TASK], test_results=[RunResult(returncode=0)])

    stage.run(
        1,
        _DESIGN,
        repo_files={generated_test_path(1): "def test_add():\n    assert add(1, 2) == 3\n"},
    )

    sent = "\n".join(m.content for m in llm.calls[0][0])
    assert "def test_add" not in sent


def test_coder_receives_only_pass_fail_and_errors_on_retry():
    stage, llm, gh, runner = _stage(
        [_OUT_OF_SCOPE, _ONE_TASK],
        test_results=[RunResult(returncode=0)],
    )

    stage.run(1, _DESIGN)

    second_call_text = "\n".join(m.content for m in llm.calls[1][0])
    assert "format miss" in second_call_text


def test_runs_setup_test_and_lint_in_order():
    stage, llm, gh, runner = _stage(
        [_ONE_TASK],
        test_results=[RunResult(returncode=0), RunResult(returncode=0), RunResult(returncode=0)],
        setup_command="pip install -e .",
        test_command="pytest -q",
        lint_command="ruff check .",
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert runner.calls == [(".", "pip install -e ."), (".", "pytest -q"), (".", "ruff check .")]


def test_lint_failure_retries_the_loop():
    stage, llm, gh, runner = _stage(
        [_ONE_TASK, _ONE_TASK],
        test_results=[
            RunResult(returncode=0),  # test passes
            RunResult(returncode=1, stdout="lint error"),  # lint fails
            RunResult(returncode=0),  # retry: test passes
            RunResult(returncode=0),  # retry: lint passes
        ],
        lint_command="ruff check .",
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.attempts == 2


def test_red_result_retries_then_agent_blocked():
    stage, llm, gh, runner = _stage(
        [_ONE_TASK, _ONE_TASK],
        test_results=[
            RunResult(returncode=1, stdout="AssertionError: assert 4 == 3"),
            RunResult(returncode=1, stdout="AssertionError: assert 4 == 3"),
        ],
        max_retries=2,
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert result.attempts == 2


def test_format_miss_retries_then_agent_blocked():
    stage, llm, gh, runner = _stage(["garbage", "garbage", "garbage"], max_retries=3)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert result.attempts == 3
    assert len(runner.calls) == 0  # never got past the schema gate


def test_provider_error_does_not_consume_attempt_counter():
    stage, llm, gh, runner = _stage(
        [ProviderError("connection reset"), _ONE_TASK],
        test_results=[RunResult(returncode=0)],
        max_retries=1,
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.attempts == 1
    assert len(llm.calls) == 2


def test_provider_error_exhausts_to_agent_blocked_without_touching_real_ladder():
    stage, llm, gh, runner = _stage(
        [ProviderError("boom")] * 5,
        max_retries=3,
        max_provider_retries=2,
    )

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert result.attempts == 0  # no real (non-provider) attempt ever ran
    assert len(runner.calls) == 0


# ---- S22 split-brain fix: commit -> sync -> run, per attempt -------------- #


class _ReadsFileTestRunner:
    """Records the on-disk content of the just-committed file *at the moment
    the toolchain runs* -- pins that the write lands before the run, not
    just eventually. `MockTestRunner` can't prove this: it never touches the
    filesystem."""

    def __init__(self, path: str, results=None) -> None:
        self._path = path
        self._results = list(results or [])
        self.seen_content: list[str | None] = []
        self.calls: list[tuple[str, str]] = []

    def run(self, cwd: str, command: str) -> RunResult:
        self.calls.append((cwd, command))
        full_path = os.path.join(cwd, self._path)
        self.seen_content.append(
            open(full_path).read() if os.path.exists(full_path) else None
        )
        return self._results.pop(0) if self._results else RunResult(returncode=0)


def test_committed_code_is_visible_to_the_toolchain_before_it_runs(tmp_path):
    runner = _ReadsFileTestRunner("src/aorc/add.py")
    llm = MockLLMClient(responses=[_ONE_TASK])
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.create_branch(branch_name(1))
    stage = Stage(llm, gh, runner)

    result = stage.run(1, _DESIGN, cwd=str(tmp_path))

    assert result.status == "proceed"
    # The toolchain's very first (and only) run already saw the committed
    # content -- proves commit -> sync -> run ordering, not commit -> run.
    assert runner.seen_content == ["def add(a, b):\n    return a + b\n"]


def test_worktree_sync_happens_again_on_every_fix_loop_attempt(tmp_path):
    runner = _ReadsFileTestRunner(
        "src/aorc/add.py",
        results=[RunResult(returncode=1, stdout="AssertionError"), RunResult(returncode=0)],
    )
    second_task = json.dumps(
        {"tasks": [{"task": "implement add()", "path": "src/aorc/add.py", "code": "def add(a, b):\n    return a - b  # fixed\n"}]}
    )
    llm = MockLLMClient(responses=[_ONE_TASK, second_task])
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.create_branch(branch_name(1))
    stage = Stage(llm, gh, runner)

    result = stage.run(1, _DESIGN, cwd=str(tmp_path))

    assert result.status == "proceed"
    assert result.attempts == 2
    assert runner.seen_content == [
        "def add(a, b):\n    return a + b\n",
        "def add(a, b):\n    return a - b  # fixed\n",
    ]


# ---- S31: blocked results carry the gate + detail that failed ------------- #


def test_blocked_by_fix_loop_exhaustion_records_last_failure():
    failing = RunResult(returncode=1, stdout="AssertionError: assert 4 == 3")
    stage, llm, gh, runner = _stage([_ONE_TASK] * 3, test_results=[failing] * 3)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert "3 attempts" in result.reason
    assert "AssertionError: assert 4 == 3" in result.reason


def test_blocked_by_provider_exhaustion_records_provider_reason():
    errors = [ProviderError("boom")] * 4  # one past max_provider_retries=3
    stage, llm, gh, runner = _stage(errors)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert "provider" in result.reason


def test_proceed_leaves_reason_empty():
    stage, llm, gh, runner = _stage([_ONE_TASK], test_results=[RunResult(returncode=0)])

    result = stage.run(1, _DESIGN)

    assert result.status == "proceed"
    assert result.reason == ""


# ---- S32: real Claude wraps JSON in markdown code fences ------------------- #


def test_parse_coder_response_accepts_a_fenced_response():
    fenced = f"Here you go:\n```json\n{_ONE_TASK}\n```"
    doc = parse_coder_response(fenced, _DESIGN.task_list, _DESIGN.files)
    assert doc is not None
    assert doc.tasks[0]["path"] == "src/aorc/add.py"


# ---- S33: a docker exec infra failure is never a test outcome -------------- #


# ---- S34: a format miss must show what the model actually returned -------- #


def test_format_miss_reason_includes_response_head_and_finish_reason():
    """Live blocks showed only 'format miss: ... did not match the required
    schema' -- useless for diagnosing WHY (truncation? wrong shape? bad
    paths?). The reason must carry the response head and finish_reason."""
    prose = "Looking at the task list, I would implement add() as follows: def add(a, b)..."
    stage, llm, gh, runner = _stage([prose] * 3)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert "format miss" in result.reason
    assert "I would implement add()" in result.reason
    assert "finish_reason=stop" in result.reason


def test_coder_hard_fails_immediately_on_infra_failure():
    """A dead exec target must not burn fix-loop attempts feeding daemon
    errors to the coder as if they were failing tests."""
    infra = RunResult(
        returncode=1,
        stderr="Error response from daemon: container aorc-issue-1 is not running",
    )
    stage, llm, gh, runner = _stage([_ONE_TASK] * 3, test_results=[infra] * 3)

    result = stage.run(1, _DESIGN)

    assert result.status == "agent-blocked"
    assert result.attempts == 1
    assert len(llm.calls) == 1
    assert "is not running" in result.reason
    assert "infra" in result.reason


# ---- S44: mechanical preservation guard ----------------------------------- #
# The prompt asks the coder to preserve existing code; the guard enforces it.
# A response whose full-file content drops a top-level name that the file's
# current contents define is a failed attempt (fed back like a schema miss),
# never a commit.

_MATH_DESIGN = DesignDoc(
    interface=[{"name": "power", "inputs": ["a", "b"], "outputs": "int"}],
    test_specs=["power(2, 3) == 8"],
    task_list=["implement power()"],
    files=["math_utils.py"],
    confidence=0.9,
)
_EXISTING_MATH = "def multiply(a, b):\n    return a * b\n"
_DROPS_MULTIPLY = json.dumps(
    {"tasks": [{"task": "implement power()", "path": "math_utils.py",
                "code": "def power(a, b):\n    return a ** b\n"}]}
)
_KEEPS_MULTIPLY = json.dumps(
    {"tasks": [{"task": "implement power()", "path": "math_utils.py",
                "code": "def multiply(a, b):\n    return a * b\n\n\ndef power(a, b):\n    return a ** b\n"}]}
)


def test_missing_preserved_names_covers_def_class_assignment_and_async():
    old = (
        "X = 1\n\nclass Shape:\n    def draw(self):\n        pass\n\n"
        "def area(s):\n    return 0\n\nasync def fetch():\n    pass\n"
    )
    new = "def area(s):\n    return 1\n"

    assert missing_preserved_names(old, new) == ["Shape", "X", "fetch"]
    assert missing_preserved_names(old, old) == []
    # methods are not top-level: dropping draw() alone is not a violation
    assert "draw" not in missing_preserved_names(old, new)


def test_deleting_existing_top_level_name_fails_the_attempt_and_feeds_back():
    stage, llm, gh, runner = _stage(
        [_DROPS_MULTIPLY, _KEEPS_MULTIPLY], test_results=[RunResult(returncode=0)]
    )

    result = stage.run(1, _MATH_DESIGN, repo_files={"math_utils.py": _EXISTING_MATH})

    assert result.status == "proceed"
    assert result.attempts == 2
    second_prompt = "\n".join(m.content for m in llm.calls[1][0])
    assert "preservation miss" in second_prompt
    assert "multiply" in second_prompt
    committed = gh.get_file("math_utils.py", branch_name(1))
    assert "def multiply" in committed and "def power" in committed
    # the deleting attempt never reached the branch
    assert len([c for c in gh.calls if c[0] == "commit_file"]) == 1


def test_persistent_deletion_exhausts_to_agent_blocked_without_committing():
    stage, llm, gh, runner = _stage([_DROPS_MULTIPLY] * 3, test_results=[])

    result = stage.run(1, _MATH_DESIGN, repo_files={"math_utils.py": _EXISTING_MATH})

    assert result.status == "agent-blocked"
    assert "preservation miss" in result.reason
    assert gh.get_file("math_utils.py", branch_name(1)) is None
    assert len(runner.calls) == 0  # deleting code never reached the toolchain
