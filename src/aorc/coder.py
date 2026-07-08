"""S7 -- Coder bounded fix loop.

The implementation stage. A coder agent writes minimal code to satisfy the
design doc's `task_list`, driven by the spec rather than its own assumptions
or the tester's test internals.

- The coder never sees test source -- only whether the previous attempt's
  toolchain run passed or failed, and its error output (`failing_test_summary`).
  It also never sees repo files outside the design's `files` list, which is
  the same bounded set the tester's generated test path is deliberately
  excluded from -- so "tests are locked" is a schema constraint
  (`parse_coder_response` rejects any path outside `files`), not a runtime
  convention to remember.
- One coder call per attempt produces one task per `task_list` entry, in
  order (mirroring S6's tester-doc shape) -- a pure, mechanical schema check,
  no LLM judgment.
- Each attempt runs `.aorc.yml`'s setup/test/lint commands via the same
  `TestRunner` seam S6 introduced; only a clean pass (returncode 0) on the
  full toolchain proceeds.
- Provider errors (`ProviderError`) are a distinct failure mode from bad
  code: they retry the same attempt without consuming the fix-loop counter,
  capped separately so a persistently broken provider still terminates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .design import DesignDoc
from .harness import write_worktree_file
from .interfaces import GitHubClient, LLMClient, Message, ProviderError
from .pipeline import branch_name
from .tester import TestRunner, TestRunResult, generated_test_path

DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_PROVIDER_RETRIES = 3
DEFAULT_TEST_COMMAND = "pytest -q"

# `ProviderError` moved to interfaces.py in S14 (it is the seam's error
# contract, shared with escalation.py and the adapters); re-exported here so
# existing imports keep working.
__all__ = ["CoderDoc", "CoderResult", "CoderStage", "ProviderError"]


_CODER_SYSTEM_PROMPT = (
    "You are the coder agent for a software issue. You implement the design "
    "doc's `task_list`, in order, against the given repo files. You do not "
    "see test source -- only whether the project's toolchain currently "
    'passes, and its error output if not. Produce a single JSON object: '
    '{"tasks": [{"task": "<task from task_list>", "path": "<exact path from '
    'the allowed files>", "code": "<full new file content>"}, ...]}. Write '
    "exactly one entry per task, in task_list order, touching only paths "
    "from the design's `files` list. Reply with the JSON object only, no "
    "surrounding prose."
)


@dataclass
class CoderDoc:
    tasks: list
    raw: dict = field(default_factory=dict)


@dataclass
class CoderResult:
    status: str  # "proceed" | "agent-blocked"
    attempts: int = 0
    # S31: why the stage blocked (provider exhaustion, or the last attempt's
    # failure summary) -- empty on "proceed". The only record of the failure.
    reason: str = ""


def _tail(text: str, limit: int = 1000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def parse_coder_response(text: str, task_list: list, allowed_files: list) -> CoderDoc | None:
    """Pure schema check, no LLM judgment: `None` on invalid JSON, a missing
    `tasks` list, a count that doesn't match `task_list` one-for-one, any
    entry missing non-empty `path`/`code`, or a `path` outside the design's
    `files` -- the mechanical enforcement that keeps the coder from ever
    writing to the locked test file."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "tasks" not in data:
        return None
    tasks = data["tasks"]
    if not isinstance(tasks, list) or len(tasks) != len(task_list):
        return None
    allowed = set(allowed_files)
    for entry in tasks:
        if not isinstance(entry, dict):
            return None
        path, code = entry.get("path"), entry.get("code")
        if not isinstance(path, str) or path not in allowed:
            return None
        if not isinstance(code, str) or not code.strip():
            return None
    return CoderDoc(tasks=tasks, raw=data)


def failing_test_summary(result: TestRunResult) -> str:
    """Pass/fail + error messages only -- never test source. This is the
    entirety of what the coder learns about a failed attempt."""
    outcome = "pass" if result.returncode == 0 else "fail"
    return f"result: {outcome}\n{result.stdout}\n{result.stderr}".strip()


class CoderStage:
    """Bounded fix loop: implement `task_list` -> run setup/test/lint from
    `.aorc.yml` -> green proceeds, anything else retries with the new
    failure summary, up to `max_retries`. Provider errors retry without
    consuming that cap."""

    def __init__(
        self,
        llm: LLMClient,
        github: GitHubClient,
        test_runner: TestRunner,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_provider_retries: int = DEFAULT_MAX_PROVIDER_RETRIES,
        setup_command: str | None = None,
        test_command: str = DEFAULT_TEST_COMMAND,
        lint_command: str | None = None,
    ) -> None:
        self._llm = llm
        self._github = github
        self._test_runner = test_runner
        self._max_retries = max_retries
        self._max_provider_retries = max_provider_retries
        self._setup_command = setup_command
        self._test_command = test_command
        self._lint_command = lint_command

    def _messages(
        self, issue_number: int, design: DesignDoc, repo_files: dict, failure: str | None
    ) -> list[Message]:
        # Scoped to the design's interface/task_list/files plus the repo
        # files it must modify -- test source never enters this prompt, and
        # the generated test file is dropped defensively even if a caller
        # passed it in by mistake.
        excluded = generated_test_path(issue_number)
        parts = [
            f"interface: {json.dumps(design.interface)}",
            f"task_list: {json.dumps(design.task_list)}",
            f"files: {json.dumps(design.files)}",
        ]
        for path, content in repo_files.items():
            if path == excluded:
                continue
            parts.append(f"Repo file {path}:\n{content}")
        parts.append(
            f"Previous attempt result:\n{failure}" if failure is not None else "No previous attempt yet."
        )
        return [Message("system", _CODER_SYSTEM_PROMPT), Message("user", "\n\n".join(parts))]

    def run(
        self,
        issue_number: int,
        design: DesignDoc,
        *,
        repo_files: dict[str, str] | None = None,
        cwd: str = ".",
        review_feedback: str | None = None,
    ) -> CoderResult:
        repo_files = dict(repo_files or {})
        # Reuses the same "what to fix" prompt slot the test-failure loop
        # uses -- the reviewer's rejection reason is just another attempt's
        # feedback, not a new context channel (S8: reviewer re-enters this
        # same bounded fix loop instead of its own retry mechanism).
        failure: str | None = f"Reviewer feedback: {review_feedback}" if review_feedback else None
        provider_retries = 0
        attempts = 0
        while attempts < self._max_retries:
            attempts += 1
            try:
                completion = self._llm.complete(self._messages(issue_number, design, repo_files, failure))
            except ProviderError:
                attempts -= 1  # provider errors never consume the fix-loop counter
                provider_retries += 1
                if provider_retries > self._max_provider_retries:
                    return CoderResult(
                        status="agent-blocked",
                        attempts=attempts,
                        reason=(
                            f"provider error: {self._max_provider_retries} retries "
                            "exhausted without a completion"
                        ),
                    )
                continue

            doc = parse_coder_response(completion.text, design.task_list, design.files)
            if doc is None:
                failure = "format miss: coder response did not match the required schema"
                continue

            for entry in doc.tasks:
                self._commit(issue_number, entry["path"], entry["code"])
                # S22 split-brain fix: mirror the just-committed content into
                # the worktree `cwd` the toolchain is about to run in -- see
                # `harness.write_worktree_file`. Must happen before
                # `_run_toolchain`, not after: that's the whole bug.
                write_worktree_file(cwd, entry["path"], entry["code"])
                repo_files[entry["path"]] = entry["code"]

            run_result = self._run_toolchain(cwd)
            if run_result.returncode == 0:
                return CoderResult(status="proceed", attempts=attempts)
            failure = failing_test_summary(run_result)

        reason = f"fix loop exhausted after {attempts} attempts"
        if failure:
            reason += f"; last failure:\n{_tail(failure)}"
        return CoderResult(status="agent-blocked", attempts=attempts, reason=reason)

    def _run_toolchain(self, cwd: str) -> TestRunResult:
        """setup -> test -> lint, from `.aorc.yml` -- never guessed. Stops
        (and reports) at the first non-zero step."""
        if self._setup_command:
            result = self._test_runner.run(cwd, self._setup_command)
            if result.returncode != 0:
                return result
        result = self._test_runner.run(cwd, self._test_command)
        if result.returncode != 0 or not self._lint_command:
            return result
        return self._test_runner.run(cwd, self._lint_command)

    def _commit(self, issue_number: int, path: str, content: str) -> None:
        self._github.commit_file(
            branch_name(issue_number), path, content, message=f"code: issue #{issue_number}"
        )
