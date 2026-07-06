"""S14 -- Failure escalation + backoff.

Three pieces, all built on the strict separation between a provider being
down and the model producing bad output (separate counters -- a flaky
network must never burn `primary_attempts` and prematurely escalate a
solvable issue):

- `BackoffLLMClient`: decorator over any `LLMClient`. A transient
  `ProviderError` retries the *same* model on the fixed exponential
  schedule (2s / 8s / 30s); only when backoff exhausts does the error
  propagate, at which point the ladder counts it as one real failure.
  `FailFastProviderError` (the Local-LLM constraint: a local `base_url` on
  a cloud runner -- S18 raises it) is never retried.
- `EscalationLadder`: primary model x `failure.primary_attempts` ->
  escalation model x `failure.escalation_attempts` -> surface to human
  (`agent-blocked` + a failure comment carrying the full error, the last
  test output, and what was attempted). Model slots come from `.aorc.yml`
  (`config.AorcConfig.primary/escalation`); this module only ever sees
  `LLMClient` instances and reads `llm.model` back off them -- no model
  name is hardcoded or even inspected.
- `RateLimitedGitHubClient`: decorator over any `GitHubClient`. GitHub
  403/429 secondary rate limits are backed off (honoring `Retry-After`
  when present) and retried; they are never counted as a failure and never
  label `agent-blocked`. A plain 403 with no `Retry-After` header is a
  permissions failure, not a rate limit -- the header is the mechanical
  discriminator -- and propagates untouched.

`sleep` is injectable everywhere (default `time.sleep`), same
seam-boundary style as S11's caller-supplied `elapsed_days`: tests assert
the exact delay sequence without waiting.

Same live-wiring caveat as S10--S13: no stage runner routes its work
through `EscalationLadder.run` yet, because there is no orchestrator wake
loop to own that composition (S16/S18). The ladder, the backoff decorator,
and the rate-limit decorator are real, tested library pieces the loop will
compose: `attempt` is any callable taking the rung's `LLMClient` and
returning an `AttemptOutcome` (e.g. one CoderStage/DesignStage pass).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .interfaces import (
    Comment,
    Completion,
    FailFastProviderError,
    GitHubClient,
    GitHubRateLimitError,
    Issue,
    LLMClient,
    Message,
    ProviderError,
    PullRequest,
)
from .pipeline import LABEL_COLUMN

BLOCKED_LABEL = "agent-blocked"
FAILURE_MARKER = "<!-- aorc:escalation-failure -->"

# Provider-error backoff: retry the same model after each delay, in order.
BACKOFF_SCHEDULE = (2.0, 8.0, 30.0)


# --------------------------------------------------------------------------- #
# Provider-error backoff (same model, separate counter)
# --------------------------------------------------------------------------- #


class BackoffLLMClient(LLMClient):
    """Decorator: absorbs transient `ProviderError`s with exponential
    backoff on the same underlying model. Exhaustion re-raises the last
    `ProviderError` -- the ladder counts that as exactly one real failure.
    `FailFastProviderError` propagates immediately, untouched."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        schedule: tuple[float, ...] = BACKOFF_SCHEDULE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._schedule = tuple(schedule)
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        retries = 0
        while True:
            try:
                return self._inner.complete(
                    messages, max_tokens=max_tokens, temperature=temperature
                )
            except FailFastProviderError:
                raise  # not transient by definition; never retried
            except ProviderError:
                if retries >= len(self._schedule):
                    raise
                self._sleep(self._schedule[retries])
                retries += 1


# --------------------------------------------------------------------------- #
# Escalation ladder
# --------------------------------------------------------------------------- #


@dataclass
class AttemptOutcome:
    """What one stage pass reported back to the ladder."""

    ok: bool
    error: str = ""  # full error text (bad output reason / test failure)
    test_output: str = ""  # last toolchain output, if a run was reached
    result: Any = None  # the successful stage artifact, when ok


@dataclass
class AttemptRecord:
    """One consumed ladder attempt, kept for the failure comment."""

    slot: str  # "primary" | "escalation"
    model: str
    error: str
    test_output: str = ""


@dataclass
class LadderResult:
    status: str  # "success" | "agent-blocked"
    attempts: list[AttemptRecord] = field(default_factory=list)
    result: Any = None


class EscalationLadder:
    """primary xN -> escalation xM -> `agent-blocked` + failure comment.

    `attempt(llm)` performs one full stage pass with the given model and
    returns an `AttemptOutcome`. A `ProviderError` escaping the attempt
    means backoff already exhausted inside it (see `BackoffLLMClient`) and
    counts as one real failure. A `FailFastProviderError` is a
    configuration error retrying cannot heal -- the ladder blocks
    immediately instead of grinding through the remaining rungs.
    """

    def __init__(
        self,
        github: GitHubClient,
        primary: LLMClient,
        escalation: LLMClient | None = None,
        *,
        primary_attempts: int = 3,
        escalation_attempts: int = 1,
    ) -> None:
        self._github = github
        self._rungs = [("primary", primary, primary_attempts)]
        if escalation is not None:
            self._rungs.append(("escalation", escalation, escalation_attempts))

    def run(
        self, issue_number: int, attempt: Callable[[LLMClient], AttemptOutcome]
    ) -> LadderResult:
        records: list[AttemptRecord] = []
        for slot, llm, attempts in self._rungs:
            for _ in range(attempts):
                try:
                    outcome = attempt(llm)
                except FailFastProviderError as exc:
                    records.append(AttemptRecord(slot, llm.model, f"fail-fast: {exc}"))
                    self._block(issue_number, records)
                    return LadderResult(status="agent-blocked", attempts=records)
                except ProviderError as exc:
                    outcome = AttemptOutcome(
                        ok=False, error=f"provider error (backoff exhausted): {exc}"
                    )
                if outcome.ok:
                    return LadderResult(
                        status="success", attempts=records, result=outcome.result
                    )
                records.append(
                    AttemptRecord(slot, llm.model, outcome.error, outcome.test_output)
                )
        self._block(issue_number, records)
        return LadderResult(status="agent-blocked", attempts=records)

    def _block(self, issue_number: int, records: list[AttemptRecord]) -> None:
        self._github.add_label(issue_number, BLOCKED_LABEL)
        self._github.set_board_column(issue_number, LABEL_COLUMN[BLOCKED_LABEL])
        self._github.post_comment(issue_number, _failure_comment(records))


def _failure_comment(records: list[AttemptRecord]) -> str:
    """The surface-to-human report: what was attempted, the full error from
    the last attempt, and the last test output that was reached."""
    lines = [
        FAILURE_MARKER,
        "All configured model attempts exhausted; labeling "
        f"`{BLOCKED_LABEL}`. A human should clarify the spec, fix a "
        "dependency, or close won't-fix.",
        "",
        "**What was attempted:**",
    ]
    for i, r in enumerate(records, 1):
        first_line = r.error.splitlines()[0] if r.error else "(no error text)"
        lines.append(f"{i}. {r.slot} model `{r.model}`: {first_line}")
    last_error = records[-1].error if records else "(no attempts recorded)"
    lines += ["", "**Full error (last attempt):**", "```", last_error, "```"]
    last_test = next((r.test_output for r in reversed(records) if r.test_output), "")
    lines += [
        "",
        "**Last test output:**",
        "```",
        last_test or "(no test run reached)",
        "```",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# GitHub secondary-rate-limit backoff (never a failure)
# --------------------------------------------------------------------------- #


def _rate_limit_retry_after(exc: BaseException) -> tuple[bool, float | None]:
    """Mechanical rate-limit discriminator. `GitHubRateLimitError` is one by
    contract. Anything else is duck-typed against the SDK exception shape
    (`.status` / `.status_code` + `.headers`) so the wrapper works over the
    real adapter without importing its SDK: 429 always is; 403 only when a
    `Retry-After` header is present (the secondary-limit signature) --
    a plain 403 is a permissions failure and must propagate."""
    if isinstance(exc, GitHubRateLimitError):
        return True, exc.retry_after

    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    headers = getattr(exc, "headers", None)
    retry_after = None
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                try:
                    retry_after = float(value)
                except (TypeError, ValueError):
                    retry_after = None
    if status == 429:
        return True, retry_after
    if status == 403 and retry_after is not None:
        return True, retry_after
    return False, None


class RateLimitedGitHubClient(GitHubClient):
    """Decorator: every seam call retries GitHub 403/429 secondary rate
    limits with backoff, honoring `Retry-After` when the response carried
    one and falling back to the fixed schedule otherwise. Exhaustion
    re-raises the rate-limit error -- infrastructure trouble, surfaced to
    the caller, but never a pipeline failure: nothing here labels
    `agent-blocked` or consumes a ladder attempt."""

    def __init__(
        self,
        inner: GitHubClient,
        *,
        schedule: tuple[float, ...] = BACKOFF_SCHEDULE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._schedule = tuple(schedule)
        self._sleep = sleep

    def _call(self, fn: Callable[[], Any]) -> Any:
        for delay in self._schedule:
            try:
                return fn()
            except Exception as exc:
                is_rate_limit, retry_after = _rate_limit_retry_after(exc)
                if not is_rate_limit:
                    raise
                self._sleep(retry_after if retry_after is not None else delay)
        return fn()  # final try after the last backoff; errors propagate

    # ---- issues ---------------------------------------------------------- #
    def get_issue(self, number: int) -> Issue:
        return self._call(lambda: self._inner.get_issue(number))

    def list_issues(self, state: str = "open") -> list[Issue]:
        return self._call(lambda: self._inner.list_issues(state))

    def close_issue(self, number: int) -> None:
        return self._call(lambda: self._inner.close_issue(number))

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> Issue:
        return self._call(lambda: self._inner.create_issue(title, body, labels))

    # ---- comments -------------------------------------------------------- #
    def post_comment(self, issue_number: int, body: str) -> Comment:
        return self._call(lambda: self._inner.post_comment(issue_number, body))

    def list_comments(self, issue_number: int) -> list[Comment]:
        return self._call(lambda: self._inner.list_comments(issue_number))

    # ---- labels ---------------------------------------------------------- #
    def get_labels(self, issue_number: int) -> list[str]:
        return self._call(lambda: self._inner.get_labels(issue_number))

    def add_label(self, issue_number: int, label: str) -> None:
        return self._call(lambda: self._inner.add_label(issue_number, label))

    def remove_label(self, issue_number: int, label: str) -> None:
        return self._call(lambda: self._inner.remove_label(issue_number, label))

    def set_labels(self, issue_number: int, labels: list[str]) -> None:
        return self._call(lambda: self._inner.set_labels(issue_number, labels))

    def create_label(
        self, name: str, color: str = "ededed", description: str = ""
    ) -> None:
        return self._call(lambda: self._inner.create_label(name, color, description))

    # ---- pull requests --------------------------------------------------- #
    def open_pull_request(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PullRequest:
        return self._call(lambda: self._inner.open_pull_request(title, body, head, base))

    def get_pull_request(self, number: int) -> PullRequest:
        return self._call(lambda: self._inner.get_pull_request(number))

    def list_pull_requests(self, state: str = "open") -> list[PullRequest]:
        return self._call(lambda: self._inner.list_pull_requests(state))

    def merge_pull_request(self, number: int, method: str = "merge") -> None:
        return self._call(lambda: self._inner.merge_pull_request(number, method))

    def delete_branch(self, branch: str) -> None:
        return self._call(lambda: self._inner.delete_branch(branch))

    # ---- repo contents ---------------------------------------------------- #
    def get_file(self, path: str, ref: str) -> str | None:
        return self._call(lambda: self._inner.get_file(path, ref))

    def commit_file(self, branch: str, path: str, content: str, message: str) -> None:
        return self._call(lambda: self._inner.commit_file(branch, path, content, message))

    # ---- projects board -------------------------------------------------- #
    def set_board_column(self, issue_number: int, column: str) -> None:
        return self._call(lambda: self._inner.set_board_column(issue_number, column))

    def get_board_column(self, issue_number: int) -> str | None:
        return self._call(lambda: self._inner.get_board_column(issue_number))
