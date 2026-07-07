"""S6 -- Tester + test-critic + red/error gate + interface coverage.

The spec-encoding stages, kept strictly separate from implementation.

- The tester agent writes one failing test per design `task_list` entry,
  built only from the design doc's `interface`/`task_list`/`test_specs` --
  never repo files or implementation code, so it tests behavior, not
  internals.
- The test-critic agent is a *distinct* `LLMClient` (no incentive to pass
  its own work) that reviews those tests against the design doc before the
  coder ever runs.
- Interface coverage is a separate, pure static set-comparison (no
  execution, no LLM): every function in the design's `interface` must be
  referenced by at least one test.
- Only after both gates pass are tests committed and actually run; the
  red/error classifier is mechanical too -- a clean assertion failure
  ("red") proceeds to the coder, a crash/error sends the loop back to
  retry the tester.
"""

from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .design import DesignDoc
from .harness import write_worktree_file
from .interfaces import GitHubClient, LLMClient, Message
from .pipeline import branch_name

DEFAULT_MAX_RETRIES = 3
DEFAULT_TEST_COMMAND = "pytest -q"

# Markers that distinguish a collection/import crash from a clean assertion
# failure -- present in pytest's own output for those failure modes.
_ERROR_MARKERS = (
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "NameError",
    "AttributeError",
    "collected 0 items",
    "INTERNALERROR",
    "ERRORS",
)

_TESTER_SYSTEM_PROMPT = (
    "You are the tester agent for a software issue. You write failing "
    "tests -- you never see or write implementation code. Given the "
    "design doc's `interface`, `task_list`, and `test_specs`, produce a "
    'single JSON object: {"tests": [{"task": "<task from task_list>", '
    '"code": "<python test function source, calling only functions listed '
    'in `interface`>"}, ...]}. Write exactly one test per task, in '
    "task_list order. Reply with the JSON object only, no surrounding prose."
)

_CRITIC_SYSTEM_PROMPT = (
    "You are the test-critic agent. You did not write these tests -- you "
    "review them against the design doc. Reject any test that calls a "
    "function not listed in `interface`, or that does not correspond to "
    "one of the design's `test_specs` behaviors. Reply with a single JSON "
    'object: {"verdict": "approve" | "reject", "reason": "..."}. Reply '
    "with the JSON object only, no surrounding prose."
)


def generated_test_path(issue_number: int) -> str:
    return f"aorc/issue-{issue_number}/test_generated.py"


def marker_path(issue_number: int) -> str:
    # Same path `ArtifactChecker.tests_committed` (S2) already checks.
    return f"aorc/issue-{issue_number}/tests.marker"


@dataclass
class TesterDoc:
    tests: list
    code: str
    raw: dict = field(default_factory=dict)


@dataclass
class CriticVerdict:
    verdict: str  # "approve" | "reject"
    reason: str = ""


@dataclass
class TestRunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class TesterResult:
    status: str  # "proceed" | "agent-blocked"
    code: str | None = None
    attempts: int = 0


def parse_tester_response(text: str, task_list: list) -> TesterDoc | None:
    """Pure schema check, no LLM judgment: `None` on invalid JSON, a missing
    `tests` list, a count that doesn't match `task_list` one-for-one, or any
    entry missing non-empty `code`."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "tests" not in data:
        return None
    tests = data["tests"]
    if not isinstance(tests, list) or len(tests) != len(task_list):
        return None
    for entry in tests:
        if not isinstance(entry, dict) or not isinstance(entry.get("code"), str) or not entry["code"].strip():
            return None
    code = "\n\n".join(entry["code"] for entry in tests)
    return TesterDoc(tests=tests, code=code, raw=data)


def parse_critic_response(text: str) -> CriticVerdict | None:
    """Pure schema check: `None` unless `verdict` is exactly "approve" or
    "reject"."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in ("approve", "reject"):
        return None
    return CriticVerdict(verdict=verdict, reason=str(data.get("reason", "")))


def _referenced_names(code: str) -> set[str]:
    return set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", code))


def interface_coverage_gate(interface: list, code: str) -> bool:
    """Pure static set-comparison, no execution: design interface functions
    must be a subset of the functions the test code references."""
    names = {item["name"] for item in interface if isinstance(item, dict) and "name" in item}
    if not names:
        return True
    return names <= _referenced_names(code)


def classify_test_run(result: TestRunResult) -> str:
    """Mechanical red/error/green classification, no LLM judgment:
    - returncode 0 -> "green" (unexpectedly already passing)
    - a collection/import/syntax crash marker present -> "error"
    - any other non-zero return (a clean assertion failure) -> "red"
    """
    if result.returncode == 0:
        return "green"
    text = result.stdout + result.stderr
    if any(marker in text for marker in _ERROR_MARKERS):
        return "error"
    return "red"


class TestRunner(ABC):
    """Executes the project's test command against a worktree. The command
    comes from `.aorc.yml`'s `test:` entry -- never guessed."""

    @abstractmethod
    def run(self, cwd: str, command: str) -> TestRunResult: ...


class MockTestRunner(TestRunner):
    """Scripted `TestRunResult`s for the unit suite; records every call like
    `MockLLMClient`/`MockGitHubClient` do."""

    def __init__(self, results: list[TestRunResult] | None = None) -> None:
        self._results = list(results or [])
        self.calls: list[tuple[str, str]] = []

    def run(self, cwd: str, command: str) -> TestRunResult:
        self.calls.append((cwd, command))
        if self._results:
            return self._results.pop(0)
        return TestRunResult(returncode=0)


class SubprocessTestRunner(TestRunner):
    """Real test execution via the project's own toolchain -- not a provider
    SDK, so this doesn't cross architecture invariant #1 (same reasoning as
    `WorktreeManager`'s git shell-out, S4)."""

    def run(self, cwd: str, command: str) -> TestRunResult:
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
        return TestRunResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class TesterStage:
    """Tester writes failing tests from the design doc alone; a distinct
    critic LLM reviews them; a pure static gate asserts interface coverage;
    only then are tests committed and run, gated by the mechanical
    red/error classifier -- only a clean red proceeds to the coder."""

    def __init__(
        self,
        tester_llm: LLMClient,
        critic_llm: LLMClient,
        github: GitHubClient,
        test_runner: TestRunner,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        test_command: str = DEFAULT_TEST_COMMAND,
    ) -> None:
        self._tester_llm = tester_llm
        self._critic_llm = critic_llm
        self._github = github
        self._test_runner = test_runner
        self._max_retries = max_retries
        self._test_command = test_command

    def _tester_messages(self, design: DesignDoc) -> list[Message]:
        # Scoped to the design's interface/task_list/test_specs only --
        # never repo files or implementation code.
        parts = [
            f"interface: {json.dumps(design.interface)}",
            f"task_list: {json.dumps(design.task_list)}",
            f"test_specs: {json.dumps(design.test_specs)}",
        ]
        return [Message("system", _TESTER_SYSTEM_PROMPT), Message("user", "\n".join(parts))]

    def _critic_messages(self, design: DesignDoc, code: str) -> list[Message]:
        parts = [
            f"interface: {json.dumps(design.interface)}",
            f"test_specs: {json.dumps(design.test_specs)}",
            f"tests:\n{code}",
        ]
        return [Message("system", _CRITIC_SYSTEM_PROMPT), Message("user", "\n".join(parts))]

    def run(self, issue_number: int, design: DesignDoc, *, cwd: str = ".") -> TesterResult:
        attempts = 0
        for attempts in range(1, self._max_retries + 1):
            completion = self._tester_llm.complete(self._tester_messages(design))
            doc = parse_tester_response(completion.text, design.task_list)
            if doc is None:
                continue  # format miss -- retry the tester

            if not interface_coverage_gate(design.interface, doc.code):
                continue  # shallow suite -- retry the tester

            critic_completion = self._critic_llm.complete(self._critic_messages(design, doc.code))
            verdict = parse_critic_response(critic_completion.text)
            if verdict is None or verdict.verdict == "reject":
                continue  # off-spec (or critic format miss) -- retry the tester

            self._commit(issue_number, doc.code)
            # S22 split-brain fix: same ordering pin as `CoderStage` -- mirror
            # the committed test source into `cwd` before the toolchain runs.
            write_worktree_file(cwd, generated_test_path(issue_number), doc.code)
            run_result = self._test_runner.run(cwd, self._test_command)
            if classify_test_run(run_result) == "red":
                return TesterResult(status="proceed", code=doc.code, attempts=attempts)
            # error/crash (or an unexpected green) -- back to tester/design

        return TesterResult(status="agent-blocked", code=None, attempts=attempts)

    def _commit(self, issue_number: int, code: str) -> None:
        branch = branch_name(issue_number)
        self._github.commit_file(
            branch, generated_test_path(issue_number), code, message=f"test: issue #{issue_number}"
        )
        self._github.commit_file(
            branch, marker_path(issue_number), "committed", message=f"test: issue #{issue_number} marker"
        )
