"""S11 -- Clarification (grill-me): interrogate vague issues one question at
a time; resume gated on permission (author_association) AND content."""

from __future__ import annotations

from aorc.clarification import (
    BLOCKED_LABEL,
    LABEL,
    NUDGE_MARKER,
    QUESTION_MARKER,
    RESOLVED_TOKEN,
    ClarificationStage,
)
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Comment, Issue
from aorc.llm.mock import MockLLMClient


def _stage(responses):
    llm = MockLLMClient(responses=responses)
    gh = MockGitHubClient(issues=[Issue(number=1, title="vague thing", body="do the thing")])
    return ClarificationStage(llm, gh), llm, gh


def test_start_posts_first_question_and_labels_issue():
    stage, llm, gh = _stage(["what does 'the thing' mean?"])
    result = stage.start(gh.get_issue(1))

    assert result.status == "asked"
    assert result.question == "what does 'the thing' mean?"
    assert gh.issues[1].labels == [LABEL]
    assert gh.board[1] == "Needs Clarification"
    posted = gh.list_comments(1)
    assert len(posted) == 1
    assert posted[0].body == f"{QUESTION_MARKER}\nwhat does 'the thing' mean?"


def test_non_write_reply_is_ignored():
    stage, llm, gh = _stage(["first question"])
    stage.start(gh.get_issue(1))

    reply = Comment(id=999, body="thanks!", author="rando", author_association="NONE")
    result = stage.handle_comment(gh.get_issue(1), reply)

    assert result.status == "ignored"
    assert len(llm.calls) == 1  # only the initial question call was made
    assert gh.issues[1].labels == [LABEL]  # untouched


def test_write_reply_that_answers_resumes_and_clears_label():
    stage, llm, gh = _stage(["first question", RESOLVED_TOKEN])
    stage.start(gh.get_issue(1))

    reply = Comment(id=501, body="it means X", author="alice", author_association="COLLABORATOR")
    result = stage.handle_comment(gh.get_issue(1), reply)

    assert result.status == "resume"
    assert gh.issues[1].labels == []


def test_write_reply_that_is_partial_asks_next_question():
    stage, llm, gh = _stage(["first question", "ok but which environment?"])
    stage.start(gh.get_issue(1))

    reply = Comment(id=501, body="thanks!", author="alice", author_association="MEMBER")
    result = stage.handle_comment(gh.get_issue(1), reply)

    assert result.status == "asked"
    assert result.question == "ok but which environment?"
    assert gh.issues[1].labels == [LABEL]  # still waiting
    # the LLM saw the full conversation: issue body, first question, the reply
    conversation = llm.calls[-1][0][-1].content
    assert "first question" in conversation
    assert "thanks!" in conversation


def test_history_reconstructed_from_markers_not_author_name():
    stage, llm, gh = _stage(["first question", "next question"])
    stage.start(gh.get_issue(1))
    # simulate the real adapter: the bot's actual GitHub login, not "aorc[bot]"
    gh.comments[1][0].author = "some-app-installation-login"

    reply = Comment(id=501, body="partial answer", author="alice", author_association="OWNER")
    stage.handle_comment(gh.get_issue(1), reply)

    conversation = llm.calls[-1][0][-1].content
    assert "Question: first question" in conversation
    assert "Reply: partial answer" in conversation


def test_check_timeout_ok_before_nudge_window():
    stage, llm, gh = _stage([])
    result = stage.check_timeout(1, elapsed_days=3, nudge_days=7, block_days=7)
    assert result.status == "ok"
    assert gh.list_comments(1) == []


def test_check_timeout_nudges_once_at_window_one():
    stage, llm, gh = _stage([])
    first = stage.check_timeout(1, elapsed_days=7, nudge_days=7, block_days=7)
    assert first.status == "nudged"
    assert any(c.body.startswith(NUDGE_MARKER) for c in gh.list_comments(1))

    # re-checking at the same/later elapsed time before window 2 is a no-op,
    # not a second nudge comment
    second = stage.check_timeout(1, elapsed_days=8, nudge_days=7, block_days=7)
    assert second.status == "ok"
    assert len([c for c in gh.list_comments(1) if c.body.startswith(NUDGE_MARKER)]) == 1


def test_check_timeout_blocks_at_window_two():
    stage, llm, gh = _stage([])
    gh.issues[1].labels = [LABEL]
    result = stage.check_timeout(1, elapsed_days=14, nudge_days=7, block_days=7)

    assert result.status == "blocked"
    assert gh.issues[1].labels == [BLOCKED_LABEL]
    assert gh.board[1] == "Blocked"


def test_check_timeout_infinite_windows_never_trip():
    stage, llm, gh = _stage([])
    result = stage.check_timeout(
        1, elapsed_days=10_000, nudge_days=float("inf"), block_days=float("inf")
    )
    assert result.status == "ok"
    assert gh.list_comments(1) == []
