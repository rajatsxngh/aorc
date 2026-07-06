"""S3 -- Triage (orchestrator-side).

Cheap first-pass actionable/not-ready classifier. Not the authoritative gate
(Design decides for real in S5) -- just a fast guess, mocked LLM throughout.
"""

from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.triage import TriageResult, triage


def test_actionable_issue_from_llm():
    llm = MockLLMClient(responses=["actionable"])
    issue = Issue(number=1, title="Add retry", body="Add exponential backoff retry to the HTTP client.")
    assert triage(issue, llm) == TriageResult("actionable")


def test_not_ready_vague_when_llm_says_not_ready_and_no_epic_hints():
    llm = MockLLMClient(responses=["not-ready"])
    issue = Issue(number=1, title="Make it better", body="Improve things somehow.")
    assert triage(issue, llm) == TriageResult("not-ready", "vague")


def test_not_ready_epic_reason_from_task_list_no_epic_label():
    llm = MockLLMClient(responses=["not-ready"])
    body = "We need to do a bunch of things:\n- [ ] one\n- [ ] two\n- [ ] three"
    issue = Issue(number=1, title="Big project", body=body)
    assert triage(issue, llm) == TriageResult("not-ready", "epic")


def test_not_ready_epic_reason_from_epic_label_no_task_list():
    llm = MockLLMClient(responses=["not-ready"])
    issue = Issue(
        number=1, title="Rewrite auth", body="Overhaul the whole auth subsystem.", labels=["epic"]
    )
    assert triage(issue, llm) == TriageResult("not-ready", "epic")


def test_empty_body_is_vague_without_calling_llm():
    llm = MockLLMClient(responses=["actionable"])  # would say actionable if asked -- must not be asked
    issue = Issue(number=1, title="???", body="")
    assert triage(issue, llm) == TriageResult("not-ready", "vague")
    assert llm.calls == []


def test_whitespace_only_body_is_vague():
    llm = MockLLMClient(responses=["actionable"])
    issue = Issue(number=1, title="???", body="   \n\t  ")
    assert triage(issue, llm) == TriageResult("not-ready", "vague")
    assert llm.calls == []


def test_retriage_is_idempotent_actionable_stays_actionable():
    llm = MockLLMClient(default="actionable")
    issue = Issue(number=1, title="Add retry", body="Add exponential backoff retry to the HTTP client.")
    first = triage(issue, llm)
    second = triage(issue, llm)
    assert first == second == TriageResult("actionable")


def test_retriage_after_clarification_reenters_as_actionable():
    llm = MockLLMClient(responses=["not-ready", "actionable"])
    vague_issue = Issue(number=1, title="Fix stuff", body="Something is broken.")
    assert triage(vague_issue, llm) == TriageResult("not-ready", "vague")

    clarified_issue = Issue(
        number=1,
        title="Fix stuff",
        body="The /login endpoint returns 500 when the password field is empty; it should return 400.",
    )
    assert triage(clarified_issue, llm) == TriageResult("actionable")


def test_retriage_of_decomposed_sub_issue_is_actionable_in_one_step():
    llm = MockLLMClient(responses=["actionable"])
    sub_issue = Issue(
        number=2,
        title="Sub-issue of #1: validate empty password",
        body="Return 400 (not 500) from POST /login when password is empty.",
    )
    assert triage(sub_issue, llm) == TriageResult("actionable")
