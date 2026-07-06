"""S12 -- Epic decomposition: epic -> PRD + sub-issues, run orchestrator-side.

Mirrors S5's schema-gate style (attempt the structured task; low confidence
*is* the vagueness signal) and S11's idempotency-marker style (hidden HTML
comment, searched before creating, never duplicated).
"""

from __future__ import annotations

import json

from aorc.decomposition import (
    DecompositionStage,
    check_parent_complete,
    epic_depth,
    existing_sub_issue_number,
    parse_decomposition_response,
)
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient

_GOOD_PLAN = json.dumps(
    {
        "prd": "Overhaul the whole auth subsystem.",
        "sub_issues": [
            {"title": "Add password reset flow", "body": "Implement /reset-password."},
            {"title": "Add 2FA", "body": "Implement TOTP-based 2FA."},
        ],
        "confidence": 0.9,
    }
)

_VAGUE_PLAN = json.dumps({"prd": "", "sub_issues": [], "confidence": 0.1})


def _epic(**kwargs) -> Issue:
    defaults = dict(
        number=42,
        title="Rewrite auth",
        body="Overhaul the whole auth subsystem.",
        labels=["epic"],
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def test_parse_rejects_invalid_json():
    assert parse_decomposition_response("not json") is None


def test_parse_rejects_missing_field():
    assert parse_decomposition_response(json.dumps({"prd": "x", "confidence": 0.9})) is None


def test_parse_accepts_well_formed_plan():
    plan = parse_decomposition_response(_GOOD_PLAN)
    assert plan is not None
    assert plan.confidence == 0.9
    assert len(plan.sub_issues) == 2


def test_decompose_creates_scoped_sub_issues_linked_to_parent():
    llm = MockLLMClient(responses=[_GOOD_PLAN])
    gh = MockGitHubClient(issues=[_epic()])

    result = DecompositionStage(llm, gh).decompose(gh.get_issue(42))

    assert result.status == "decomposed"
    assert len(result.created) == 2
    first = gh.get_issue(result.created[0])
    assert first.title == "Add password reset flow"
    assert "<!-- aorc:parent=42 -->" in first.body
    assert "<!-- aorc:sub-index=1 -->" in first.body
    assert "Implement /reset-password." in first.body
    second = gh.get_issue(result.created[1])
    assert "<!-- aorc:sub-index=2 -->" in second.body


def test_vague_epic_runs_grill_me_instead_of_decomposing():
    llm = MockLLMClient(responses=[_VAGUE_PLAN, "what's the actual goal here?"])
    gh = MockGitHubClient(issues=[_epic()])

    result = DecompositionStage(llm, gh).decompose(gh.get_issue(42))

    assert result.status == "needs-clarification"
    assert result.created == []
    assert "needs-clarification" in gh.issues[42].labels
    posted = gh.list_comments(42)
    assert len(posted) == 1
    assert "what's the actual goal here?" in posted[0].body


def test_unparseable_response_also_routes_to_clarification():
    llm = MockLLMClient(responses=["not json at all", "please clarify scope"])
    gh = MockGitHubClient(issues=[_epic()])

    result = DecompositionStage(llm, gh).decompose(gh.get_issue(42))

    assert result.status == "needs-clarification"
    assert result.plan is None


def test_rerun_is_idempotent_creates_only_missing_sub_issues():
    llm = MockLLMClient(responses=[_GOOD_PLAN])
    gh = MockGitHubClient(issues=[_epic()])
    stage = DecompositionStage(llm, gh)

    first_result = stage.decompose(gh.get_issue(42))
    assert len(first_result.created) == 2
    assert len(gh.issues) == 3  # epic + 2 subs

    llm2 = MockLLMClient(responses=[_GOOD_PLAN])
    second_result = DecompositionStage(llm2, gh).decompose(gh.get_issue(42))

    assert second_result.created == []
    assert sorted(second_result.skipped) == sorted(first_result.created)
    assert len(gh.issues) == 3  # no duplicates


def test_existing_sub_issue_number_finds_by_marker_not_index_order():
    gh = MockGitHubClient(
        issues=[
            _epic(),
            Issue(number=99, title="stray", body="<!-- aorc:parent=42 -->\n<!-- aorc:sub-index=1 -->\n\nhi"),
        ]
    )
    assert existing_sub_issue_number(42, 1, gh) == 99
    assert existing_sub_issue_number(42, 2, gh) is None


def test_sub_issues_tagged_with_depth_one_level_below_parent():
    llm = MockLLMClient(responses=[_GOOD_PLAN])
    gh = MockGitHubClient(issues=[_epic(labels=["epic"])])  # depth 0 (no depth label)

    result = DecompositionStage(llm, gh).decompose(gh.get_issue(42))

    assert gh.get_issue(result.created[0]).labels == ["depth:1"]


def test_depth_increments_from_parents_own_depth_label():
    llm = MockLLMClient(responses=[_GOOD_PLAN])
    gh = MockGitHubClient(issues=[_epic(labels=["epic", "depth:2"])])

    result = DecompositionStage(llm, gh).decompose(gh.get_issue(42))

    assert gh.get_issue(result.created[0]).labels == ["depth:3"]


def test_epic_depth_defaults_to_zero_with_no_depth_label():
    assert epic_depth(_epic(labels=["epic"])) == 0


def test_epic_depth_reads_existing_label():
    assert epic_depth(_epic(labels=["epic", "depth:4"])) == 4


def test_check_parent_complete_closes_when_all_subs_closed():
    gh = MockGitHubClient(
        issues=[
            _epic(state="open"),
            Issue(number=1, body="<!-- aorc:parent=42 -->\n<!-- aorc:sub-index=1 -->", state="closed"),
            Issue(number=2, body="<!-- aorc:parent=42 -->\n<!-- aorc:sub-index=2 -->", state="closed"),
        ]
    )
    assert check_parent_complete(42, gh) is True
    assert gh.issues[42].state == "closed"


def test_check_parent_complete_false_when_a_sub_is_still_open():
    gh = MockGitHubClient(
        issues=[
            _epic(state="open"),
            Issue(number=1, body="<!-- aorc:parent=42 -->\n<!-- aorc:sub-index=1 -->", state="closed"),
            Issue(number=2, body="<!-- aorc:parent=42 -->\n<!-- aorc:sub-index=2 -->", state="open"),
        ]
    )
    assert check_parent_complete(42, gh) is False
    assert gh.issues[42].state == "open"


def test_check_parent_complete_false_when_no_sub_issues_found():
    gh = MockGitHubClient(issues=[_epic(state="open")])
    assert check_parent_complete(42, gh) is False
    assert gh.issues[42].state == "open"
