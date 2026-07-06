"""S12 -- Epic decomposition: the refinement path for oversized/epic issues.

Runs orchestrator-side, no build container (same seam-boundary style as S3
triage and S11 clarification). An epic issue (S3's `not-ready`/`epic`
verdict) is handed to a decomposition agent that produces a PRD and a
bounded list of scoped sub-issues in one JSON response, mirroring S5's
schema-gate style: attempt the structured task first, and a low-confidence
(or empty) result *is* the "this epic is too vague to decompose" signal --
that's what routes to grill-me (S11) before any sub-issue is created,
rather than a separate up-front vagueness pass.

Idempotent by construction: each sub-issue is tagged with a deterministic
hidden marker (`aorc:parent=N` / `aorc:sub-index=I`); before creating
sub-issue I of parent N, existing issues (open and closed) are searched for
that exact marker pair and creation is skipped if found. Re-running
decomposition (e.g. on re-sync) only ever creates what's missing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .clarification import ClarificationStage
from .interfaces import GitHubClient, Issue, LLMClient, Message

DEFAULT_CONFIDENCE_THRESHOLD = 0.5

_DEPTH_LABEL_RE = re.compile(r"^depth:(\d+)$")

_SYSTEM_PROMPT = (
    "You are the decomposition agent for an oversized software issue (an "
    "epic). Produce a single JSON object with exactly these fields:\n"
    '  "prd": "a short PRD describing the epic\'s intent and scope"\n'
    '  "sub_issues": [{"title": ..., "body": ...}, ...]\n'
    '  "confidence": 0.0-1.0\n'
    "Each sub-issue must be concrete, scoped, and independently actionable. "
    "If the epic's intent is too unclear to decompose into concrete "
    "sub-issues, still reply with the JSON schema but set confidence low and "
    "sub_issues empty. Reply with the JSON object only, no surrounding prose."
)


def _marker(parent_number: int, index: int) -> str:
    return f"<!-- aorc:parent={parent_number} -->\n<!-- aorc:sub-index={index} -->"


def epic_depth(epic: Issue) -> int:
    """The epic's own `depth:N` diagnostic label, or 0 if it has none (a
    top-level epic). Sub-issues get `depth:{epic_depth + 1}` so a runaway
    epics-into-epics loop is legible without any behavioral cap -- S13's
    cost caps are the real backstop."""
    for label in epic.labels:
        m = _DEPTH_LABEL_RE.match(label)
        if m:
            return int(m.group(1))
    return 0


@dataclass
class DecompositionPlan:
    prd: str
    sub_issues: list[dict]
    confidence: float
    raw: dict = field(default_factory=dict)


@dataclass
class DecompositionResult:
    status: str  # "decomposed" | "needs-clarification"
    created: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    plan: DecompositionPlan | None = None


def parse_decomposition_response(text: str) -> DecompositionPlan | None:
    """Pure schema check, no LLM judgment -- mirrors S5's
    `parse_design_response`: `None` on any format miss."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if any(f not in data for f in ("prd", "sub_issues", "confidence")):
        return None
    if not isinstance(data["sub_issues"], list):
        return None
    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError):
        return None
    return DecompositionPlan(
        prd=str(data["prd"]),
        sub_issues=data["sub_issues"],
        confidence=confidence,
        raw=data,
    )


def existing_sub_issue_number(parent_number: int, index: int, github: GitHubClient) -> int | None:
    """Search open+closed issues for the exact (parent, index) marker pair --
    the idempotency check run before creating sub-issue `index`."""
    marker = _marker(parent_number, index)
    for issue in github.list_issues(state="all"):
        if marker in issue.body:
            return issue.number
    return None


class DecompositionStage:
    """Runs the decomposition agent on an epic; either creates its
    sub-issues (idempotently) or, when the epic itself is too vague to
    decompose, hands off to grill-me (S11) instead."""

    def __init__(
        self,
        llm: LLMClient,
        github: GitHubClient,
        *,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._llm = llm
        self._github = github
        self._confidence_threshold = confidence_threshold
        self._clarification = ClarificationStage(llm, github)

    def decompose(self, epic: Issue) -> DecompositionResult:
        completion = self._llm.complete(
            [
                Message("system", _SYSTEM_PROMPT),
                Message("user", f"Epic #{epic.number} -- {epic.title}\n\n{epic.body}"),
            ]
        )
        plan = parse_decomposition_response(completion.text)
        if plan is None or plan.confidence < self._confidence_threshold or not plan.sub_issues:
            self._clarification.start(epic)
            return DecompositionResult(status="needs-clarification", plan=plan)

        depth = epic_depth(epic) + 1
        created: list[int] = []
        skipped: list[int] = []
        for index, sub in enumerate(plan.sub_issues, start=1):
            existing = existing_sub_issue_number(epic.number, index, self._github)
            if existing is not None:
                skipped.append(existing)
                continue
            body = f"{_marker(epic.number, index)}\n\n{sub.get('body', '')}"
            issue = self._github.create_issue(
                title=sub.get("title", f"Sub-issue {index} of #{epic.number}"),
                body=body,
                labels=[f"depth:{depth}"],
            )
            created.append(issue.number)

        return DecompositionResult(status="decomposed", created=created, skipped=skipped, plan=plan)


def check_parent_complete(parent_number: int, github: GitHubClient) -> bool:
    """True (and closes the parent) iff the epic has at least one linked
    sub-issue and every one of them is closed.

    Library function only -- the issue specifies this as "a GitHub Actions
    rule": firing it automatically whenever a sub-issue closes needs a live
    event loop this repo doesn't have yet (same seam-boundary caveat S10/S11
    documented for their own live-wiring gaps). The check itself is real and
    tested here against `MockGitHubClient`.
    """
    marker_re = re.compile(rf"<!-- aorc:parent={parent_number} -->")
    sub_issues = [i for i in github.list_issues(state="all") if marker_re.search(i.body)]
    if not sub_issues:
        return False
    if any(i.state != "closed" for i in sub_issues):
        return False
    github.close_issue(parent_number)
    return True
