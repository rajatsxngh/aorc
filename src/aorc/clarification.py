"""S11 -- Clarification (grill-me): the refinement path for vague issues.

Runs orchestrator-side, no build container. A vague issue (triage's
"not-ready"/"vague" verdict) gets interrogated one question at a time as
GitHub comments -- a conversation, not a form dump -- instead of being
handed straight to design.

Resume is gated on two independent checks, both required:
  - permission: `Comment.author_association`, never identity -- anyone with
    write/triage access on the repo can answer, not just the issue author.
  - content: every reply re-runs the LLM's clarity evaluation against the
    full conversation. A "thanks!" or an off-topic reply does not blindly
    resume; it gets the next question (or a restatement).

Questions and nudges are tagged with a hidden marker so the conversation can
be reconstructed and de-duplicated by *content*, not by which GitHub account
posted it -- the real bot account name is an installation detail this module
has no business assuming.
"""

from __future__ import annotations

from dataclasses import dataclass

from .interfaces import Comment, GitHubClient, Issue, LLMClient, Message
from .pipeline import LABEL_COLUMN

LABEL = "needs-clarification"
BLOCKED_LABEL = "agent-blocked"

# GitHub's real `author_association` values that imply write/triage access.
# CONTRIBUTOR only means "has a merged PR before" -- not write access -- so
# it's deliberately excluded; resume is gated on permission, never identity.
WRITE_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

QUESTION_MARKER = "<!-- aorc:clarification-question -->"
NUDGE_MARKER = "<!-- aorc:clarification-nudge -->"
RESOLVED_TOKEN = "RESOLVED"

DEFAULT_NUDGE_DAYS = 7.0
DEFAULT_BLOCK_DAYS = 7.0

_SYSTEM_PROMPT = (
    "You are conducting a clarification interview about a vague software "
    "issue, asking exactly one question at a time. Review the conversation "
    "so far (issue body, then Question/Reply turns). If the issue is now "
    "clear enough to produce a bounded, testable design, reply with exactly: "
    f"{RESOLVED_TOKEN}. Otherwise reply with only the single next "
    "clarifying question to ask -- restate or narrow the last question if "
    "the latest reply didn't actually address it."
)


@dataclass
class ClarificationResult:
    status: str  # "asked" | "resume" | "ignored" | "nudged" | "blocked" | "ok"
    question: str | None = None


class ClarificationStage:
    """Runs the grill-me interrogation: post/resume questions, evaluate
    replies, and enforce the nudge/block timeout windows."""

    def __init__(self, llm: LLMClient, github: GitHubClient) -> None:
        self._llm = llm
        self._github = github

    def start(self, issue: Issue) -> ClarificationResult:
        """First question for a newly-flagged vague issue."""
        completion = self._llm.complete(
            [
                Message("system", _SYSTEM_PROMPT),
                Message("user", f"Issue #{issue.number} -- {issue.title}\n\n{issue.body}"),
            ]
        )
        question = completion.text.strip()
        self._post_question(issue.number, question)
        self._github.add_label(issue.number, LABEL)
        self._github.set_board_column(issue.number, LABEL_COLUMN[LABEL])
        return ClarificationResult(status="asked", question=question)

    def handle_comment(self, issue: Issue, comment: Comment) -> ClarificationResult:
        """Evaluate a new comment on a `needs-clarification` issue."""
        if comment.author_association not in WRITE_ASSOCIATIONS:
            return ClarificationResult(status="ignored")

        messages = self._history_messages(issue, extra_reply=comment)
        completion = self._llm.complete(messages)
        text = completion.text.strip()

        if text == RESOLVED_TOKEN:
            self._github.remove_label(issue.number, LABEL)
            return ClarificationResult(status="resume")

        self._post_question(issue.number, text)
        return ClarificationResult(status="asked", question=text)

    def check_timeout(
        self,
        issue_number: int,
        elapsed_days: float,
        *,
        nudge_days: float = DEFAULT_NUDGE_DAYS,
        block_days: float = DEFAULT_BLOCK_DAYS,
    ) -> ClarificationResult:
        """`elapsed_days` (days since the issue entered `needs-clarification`)
        is supplied by the caller -- the `GitHubClient` seam has no notion of
        "when was this label applied", so sourcing a real elapsed time from
        GitHub state is the orchestrator wake-loop's job, not this stage's.
        Windows of `float('inf')` never trip, restoring wait-forever."""
        comments = self._github.list_comments(issue_number)
        already_nudged = any(c.body.startswith(NUDGE_MARKER) for c in comments)

        if elapsed_days >= nudge_days + block_days:
            self._github.remove_label(issue_number, LABEL)
            self._github.add_label(issue_number, BLOCKED_LABEL)
            self._github.set_board_column(issue_number, LABEL_COLUMN[BLOCKED_LABEL])
            self._github.post_comment(
                issue_number,
                "Clarification timed out waiting for a response; labeling "
                f"`{BLOCKED_LABEL}` (reason: clarification timed out).",
            )
            return ClarificationResult(status="blocked")

        if elapsed_days >= nudge_days and not already_nudged:
            self._github.post_comment(
                issue_number,
                f"{NUDGE_MARKER}\nStill waiting on an answer to the open "
                "clarifying question above.",
            )
            return ClarificationResult(status="nudged")

        return ClarificationResult(status="ok")

    def _post_question(self, issue_number: int, question: str) -> None:
        self._github.post_comment(issue_number, f"{QUESTION_MARKER}\n{question}")

    def _history_messages(self, issue: Issue, *, extra_reply: Comment) -> list[Message]:
        parts = [f"Issue #{issue.number} -- {issue.title}\n\n{issue.body}"]
        seen_ids = set()
        for c in self._github.list_comments(issue.number):
            seen_ids.add(c.id)
            if c.body.startswith(NUDGE_MARKER):
                continue
            if c.body.startswith(QUESTION_MARKER):
                parts.append(f"Question: {c.body[len(QUESTION_MARKER):].strip()}")
            else:
                parts.append(f"Reply: {c.body}")
        if extra_reply.id not in seen_ids:
            parts.append(f"Reply: {extra_reply.body}")
        return [Message("system", _SYSTEM_PROMPT), Message("user", "\n\n".join(parts))]
