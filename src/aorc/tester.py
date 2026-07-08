"""S6 -- Tester + test-critic + red/error gate + interface coverage.

The spec-encoding stages, kept strictly separate from implementation.

- The tester agent writes one failing test per design `test_specs` entry
  (S36 -- previously keyed to `task_list`, whose implementation steps like
  "open or create <file>" produced file-plumbing tests the critic then
  correctly rejected, forever), built only from the design doc's
  `interface`/`test_specs` -- never `task_list` (the coder's contract),
  repo files, or implementation code, so it tests behavior, not internals.
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
from .harness import container_name_for, issue_number_from_worktree_path, write_worktree_file
from .interfaces import GitHubClient, LLMClient, Message, strip_code_fences
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

# S33: markers of a `docker exec` that never reached the toolchain at all --
# the daemon's dead/removed-target errors. Not a test outcome: before this,
# "container ... is not running" carried none of the error markers, classified
# "red", and made the tester falsely proceed on a dead container.
_INFRA_MARKERS = (
    "is not running",
    "No such container",
)

_TESTER_SYSTEM_PROMPT = (
    "You are the tester agent for a software issue. You write failing "
    "tests -- you never see or write implementation code. Given the "
    "design doc's `interface` and `test_specs`, produce a single JSON "
    'object: {"tests": [{"spec": "<entry from test_specs>", '
    '"code": "<python test function source, calling only functions listed '
    'in `interface`>"}, ...]}. Write exactly one test per test_specs '
    "entry, in test_specs order -- behavioral tests only, never tests of "
    "file existence or implementation steps. Reply with the JSON object "
    "only, no surrounding prose."
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
    # S39: must sit inside the target project's conventional test root --
    # pytest configs routinely restrict collection (testpaths=["tests"]),
    # and a generated test that never gets collected silently turns the
    # tester's red gate green and lets the coder's green gate pass without
    # the generated tests running at all. Unique basename per issue so
    # concurrent issues never collide on module names.
    return f"tests/test_aorc_issue_{issue_number}.py"


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
    # S31: which gate failed and why, one line per attempt -- empty on
    # "proceed". This is the only record of the failure; nothing else in the
    # loop logs or raises.
    reason: str = ""


def _snippet(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _output_tail(result: TestRunResult, limit: int = 400) -> str:
    combined = (result.stdout + result.stderr).strip()
    return combined if len(combined) <= limit else "..." + combined[-limit:]


def parse_tester_response(text: str) -> TesterDoc | None:
    """Pure schema check, no LLM judgment: `None` on invalid JSON, a missing
    or empty `tests` list, or any entry missing non-empty `code`. No count
    check against the design (S32/S36): the parser enforces shape only;
    coverage is `interface_coverage_gate`'s job."""
    try:
        data = json.loads(strip_code_fences(text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "tests" not in data:
        return None
    tests = data["tests"]
    if not isinstance(tests, list) or not tests:
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
        data = json.loads(strip_code_fences(text))
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


def uncovered_interface_names(interface: list, code: str) -> set[str]:
    """The design-interface functions the test code never references --
    empty means the coverage gate passes."""
    names = {item["name"] for item in interface if isinstance(item, dict) and "name" in item}
    return names - _referenced_names(code)


def interface_coverage_gate(interface: list, code: str) -> bool:
    """Pure static set-comparison, no execution: design interface functions
    must be a subset of the functions the test code references."""
    return not uncovered_interface_names(interface, code)


def classify_test_run(result: TestRunResult) -> str:
    """Mechanical red/error/green classification, no LLM judgment:
    - returncode 0 -> "green" (unexpectedly already passing)
    - a docker-exec dead-target marker present -> "infra-fail" (S33: the
      toolchain never ran; stages hard-fail on it instead of retrying)
    - a collection/import/syntax crash marker present -> "error"
    - any other non-zero return (a clean assertion failure) -> "red"
    """
    if result.returncode == 0:
        return "green"
    text = result.stdout + result.stderr
    if any(marker in text for marker in _INFRA_MARKERS):
        return "infra-fail"
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
    `WorktreeManager`'s git shell-out, S4). Runs on the host; superseded in
    the live `docker` composition path by `ContainerTestRunner` (S27), but
    still used for the unit suite and the `--no-container` dev escape
    hatch."""

    def run(self, cwd: str, command: str) -> TestRunResult:
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
        return TestRunResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class ContainerTestRunner(TestRunner):
    """S27 -- real test execution via `docker exec` into the issue's own
    running container, against its mounted `/workspace`, instead of
    `SubprocessTestRunner`'s host subprocess. This is what makes the
    container the harness already starts (`DockerContainerRuntime.start`)
    the actual isolation boundary for untrusted, LLM-generated setup/test/
    lint commands, rather than a cosmetic one.

    The container to exec into is resolved from `cwd` alone -- the per-issue
    worktree path every stage already passes (`WorktreeManager.ensure` /
    `.path_for`) -- via `issue_number_from_worktree_path` and
    `container_name_for`, the same naming convention
    `DockerContainerRuntime.start` used to name the container in the first
    place. No separate issue -> container-id registry has to be threaded
    through the driver or stages to keep this correct: both names are pure
    functions of the same issue number.
    """

    def __init__(self, workdir: str = "/workspace") -> None:
        self._workdir = workdir

    def run(self, cwd: str, command: str) -> TestRunResult:
        issue_number = issue_number_from_worktree_path(cwd)
        if issue_number is None:
            raise ValueError(
                f"ContainerTestRunner cannot resolve an issue container from cwd {cwd!r} "
                "-- expected a per-issue worktree path (WorktreeManager.path_for)"
            )
        name = container_name_for(issue_number)
        proc = subprocess.run(
            ["docker", "exec", "-w", self._workdir, name, "sh", "-c", command],
            capture_output=True,
            text=True,
        )
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
        setup_command: str | None = None,
    ) -> None:
        self._tester_llm = tester_llm
        self._critic_llm = critic_llm
        self._github = github
        self._test_runner = test_runner
        self._max_retries = max_retries
        self._test_command = test_command
        self._setup_command = setup_command

    def _tester_messages(self, design: DesignDoc, feedback: str | None = None) -> list[Message]:
        # Scoped to the design's interface/test_specs only -- never
        # task_list (implementation steps: the coder's contract, S36),
        # repo files, or implementation code. `feedback` is the critic's
        # rejection reason from the previous attempt -- same retry-prompt
        # slot pattern as the coder's failure summary.
        parts = [
            f"interface: {json.dumps(design.interface)}",
            f"test_specs: {json.dumps(design.test_specs)}",
        ]
        if feedback is not None:
            parts.append(f"Previous attempt was rejected by the test-critic: {feedback}")
        return [Message("system", _TESTER_SYSTEM_PROMPT), Message("user", "\n".join(parts))]

    def _critic_messages(self, design: DesignDoc, code: str) -> list[Message]:
        parts = [
            f"interface: {json.dumps(design.interface)}",
            f"test_specs: {json.dumps(design.test_specs)}",
            f"tests:\n{code}",
        ]
        return [Message("system", _CRITIC_SYSTEM_PROMPT), Message("user", "\n".join(parts))]

    def run(self, issue_number: int, design: DesignDoc, *, cwd: str = ".") -> TesterResult:
        # S37: `.aorc.yml`'s setup runs once, before the attempt loop -- the
        # tester's pytest is the first toolchain command a fresh per-issue
        # container ever sees, so without this the target package was never
        # installed at in-test. Fail-fast on a broken environment: no test
        # regeneration can fix it, so no LLM attempt is ever spent on it.
        if self._setup_command:
            setup_result = self._test_runner.run(cwd, self._setup_command)
            if setup_result.returncode != 0:
                if classify_test_run(setup_result) == "infra-fail":
                    reason = (
                        "toolchain infrastructure failure during setup (docker exec "
                        f"target unavailable); returncode={setup_result.returncode}; "
                        f"output tail: {_output_tail(setup_result)}"
                    )
                else:
                    reason = (
                        f"setup command failed; returncode={setup_result.returncode}; "
                        f"output tail: {_output_tail(setup_result)}"
                    )
                return TesterResult(status="agent-blocked", code=None, attempts=0, reason=reason)

        attempts = 0
        reasons: list[str] = []  # S31: one line per failed attempt
        if self._setup_command:
            # S38: a later block was misread live as "setup never ran" --
            # answer "did setup run?" directly in the block reason.
            reasons.append("setup: ok (returncode=0)")
        feedback: str | None = None  # S36: critic's rejection reason, fed to the retry
        for attempts in range(1, self._max_retries + 1):
            completion = self._tester_llm.complete(self._tester_messages(design, feedback))
            doc = parse_tester_response(completion.text)
            if doc is None:
                reasons.append(
                    f"attempt {attempts}: tester response failed schema check "
                    "(expected JSON with a non-empty `tests` list); "
                    f"response head: {_snippet(completion.text)}"
                )
                continue  # format miss -- retry the tester

            uncovered = uncovered_interface_names(design.interface, doc.code)
            if uncovered:
                reasons.append(
                    f"attempt {attempts}: interface coverage gate failed; "
                    f"uncovered interface names: {', '.join(sorted(uncovered))}"
                )
                continue  # shallow suite -- retry the tester

            critic_completion = self._critic_llm.complete(self._critic_messages(design, doc.code))
            verdict = parse_critic_response(critic_completion.text)
            if verdict is None:
                reasons.append(
                    f"attempt {attempts}: critic response failed schema check; "
                    f"response head: {_snippet(critic_completion.text)}"
                )
                continue
            if verdict.verdict == "reject":
                reasons.append(f"attempt {attempts}: critic rejected tests: {verdict.reason}")
                feedback = verdict.reason
                continue  # off-spec -- retry the tester, now with the reason

            self._commit(issue_number, doc.code)
            # S22 split-brain fix: same ordering pin as `CoderStage` -- mirror
            # the committed test source into `cwd` before the toolchain runs.
            write_worktree_file(cwd, generated_test_path(issue_number), doc.code)
            run_result = self._test_runner.run(cwd, self._test_command)
            classification = classify_test_run(run_result)
            if classification == "red":
                return TesterResult(status="proceed", code=doc.code, attempts=attempts)
            if classification == "infra-fail":
                # S33: the exec target is gone -- regenerating tests cannot
                # fix that, so hard-fail now instead of burning retries.
                reasons.append(
                    f"attempt {attempts}: toolchain infrastructure failure "
                    f"(docker exec target unavailable); returncode="
                    f"{run_result.returncode}; output tail: {_output_tail(run_result)}"
                )
                return TesterResult(
                    status="agent-blocked", code=None, attempts=attempts, reason="\n".join(reasons)
                )
            # error/crash (or an unexpected green) -- back to tester/design
            reasons.append(
                f"attempt {attempts}: test run classified {classification!r} (need 'red'); "
                f"returncode={run_result.returncode}; output tail: {_output_tail(run_result)}"
            )

        return TesterResult(status="agent-blocked", code=None, attempts=attempts, reason="\n".join(reasons))

    def _commit(self, issue_number: int, code: str) -> None:
        branch = branch_name(issue_number)
        self._github.commit_file(
            branch, generated_test_path(issue_number), code, message=f"test: issue #{issue_number}"
        )
        self._github.commit_file(
            branch, marker_path(issue_number), "committed", message=f"test: issue #{issue_number} marker"
        )
