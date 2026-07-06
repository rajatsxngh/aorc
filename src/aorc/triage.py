"""S3 -- Triage (orchestrator-side).

A cheap, first-pass classifier: `actionable` or `not-ready`. Deliberately not
the authoritative gate -- Design (S5) decides for real once it tries to
produce a bounded interface. Runs with no build container.

Actionability is the LLM's one job (is the definition of done bounded and
testable?). "epic" vs "vague" is a mechanical hint on top of a not-ready
verdict, never an LLM opinion of size, so it stays deterministic and cheap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .interfaces import Issue, LLMClient, Message

_TASK_LIST_RE = re.compile(r"^\s*-\s*\[[ xX]\]", re.MULTILINE)

_SYSTEM_PROMPT = (
    "You are a triage classifier for a software issue tracker. Decide whether "
    "the issue below has a bounded, testable definition of done: could a "
    "designer produce a finite interface and could a tester write a finite "
    "pass/fail test suite for it? Reply with exactly one word: 'actionable' "
    "if yes, 'not-ready' if the scope or intent is still unclear or unbounded."
)


@dataclass
class TriageResult:
    status: str  # "actionable" | "not-ready"
    reason: str | None = None  # "vague" | "epic", set only when not-ready


def looks_like_epic(issue: Issue) -> bool:
    """Mechanical epic hint: an `epic` label or a `- [ ]` task list body.

    Public (not triage-private) because S10's dispatch selector reuses it
    to keep epics out of the build pipeline before decomposition (S12).
    """
    return "epic" in issue.labels or bool(_TASK_LIST_RE.search(issue.body))


def triage(issue: Issue, llm: LLMClient) -> TriageResult:
    """Classify one issue as actionable or not-ready(+reason).

    Pure function of (issue, llm response) -- calling it again on refined
    issue content (clarified body, decomposed sub-issue) is naturally
    idempotent, with no hidden state to reset between calls.
    """
    if not issue.body.strip():
        return TriageResult("not-ready", "vague")

    completion = llm.complete(
        [
            Message("system", _SYSTEM_PROMPT),
            Message("user", f"Title: {issue.title}\n\nBody:\n{issue.body}"),
        ]
    )
    if completion.text.strip().lower().startswith("actionable"):
        return TriageResult("actionable")

    reason = "epic" if looks_like_epic(issue) else "vague"
    return TriageResult("not-ready", reason)
