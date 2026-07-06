"""S5 -- Design stage + strict schema + actionability gate."""

from __future__ import annotations

import json

from aorc.design import (
    DesignStage,
    checkpoint_report,
    design_doc_path,
    parse_design_response,
    rebuild_in_flight_registry,
)
from aorc.github.mock import MockGitHubClient
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.pipeline import branch_name

_VALID = json.dumps(
    {
        "interface": [{"name": "add", "inputs": ["a", "b"], "outputs": "int"}],
        "test_specs": ["add(1, 2) == 3"],
        "task_list": ["implement add()"],
        "files": ["src/aorc/add.py"],
        "confidence": 0.9,
    }
)

_LOW_CONFIDENCE = json.dumps(
    {
        "interface": [],
        "test_specs": [],
        "task_list": [],
        "files": [],
        "confidence": 0.1,
    }
)


def _stage(responses, **kwargs):
    llm = MockLLMClient(responses=responses)
    gh = MockGitHubClient(issues=[Issue(number=1)])
    return DesignStage(llm, gh, **kwargs), llm, gh


def test_parse_design_response_valid():
    doc = parse_design_response(_VALID)
    assert doc is not None
    assert doc.files == ["src/aorc/add.py"]
    assert doc.confidence == 0.9


def test_parse_design_response_missing_field_is_format_miss():
    bad = json.dumps({"interface": [], "test_specs": [], "task_list": [], "files": []})
    assert parse_design_response(bad) is None


def test_parse_design_response_invalid_json_is_format_miss():
    assert parse_design_response("not json") is None


def test_parse_design_response_is_pure_no_llm_involved():
    # the discriminator is a plain function call -- no LLMClient anywhere
    assert parse_design_response(_VALID) is not None
    assert parse_design_response("{}") is None


def test_valid_schema_proceeds_and_commits_design_doc():
    stage, llm, gh = _stage([_VALID])

    result = stage.run(1, "build an adder")

    assert result.status == "proceed"
    assert result.doc.files == ["src/aorc/add.py"]
    assert result.attempts == 1
    committed = gh.get_file(design_doc_path(1), branch_name(1))
    assert committed is not None
    assert json.loads(committed)["confidence"] == 0.9


def test_low_confidence_routes_to_needs_clarification_without_commit():
    stage, llm, gh = _stage([_LOW_CONFIDENCE])

    result = stage.run(1, "do something vague")

    assert result.status == "needs-clarification"
    assert gh.get_file(design_doc_path(1), branch_name(1)) is None


def test_format_miss_retries_then_agent_blocked():
    stage, llm, gh = _stage(["garbage", "still garbage", "nope"], max_retries=3)

    result = stage.run(1, "issue body")

    assert result.status == "agent-blocked"
    assert result.attempts == 3
    assert len(llm.calls) == 3
    assert gh.get_file(design_doc_path(1), branch_name(1)) is None


def test_format_miss_then_recovers_on_retry():
    stage, llm, gh = _stage(["garbage", _VALID], max_retries=3)

    result = stage.run(1, "issue body")

    assert result.status == "proceed"
    assert result.attempts == 2
    assert len(llm.calls) == 2


def test_scoped_context_only_no_cross_stage_leakage():
    stage, llm, gh = _stage([_VALID])

    stage.run(
        1,
        "the issue body",
        qa=["Q: what? A: this"],
        repo_files={"src/aorc/foo.py": "def foo(): pass"},
    )

    sent_messages, _kwargs = llm.calls[0]
    sent_text = "\n".join(m.content for m in sent_messages)
    assert "the issue body" in sent_text
    assert "what? A: this" in sent_text
    assert "def foo(): pass" in sent_text


def test_no_graphify_client_leaves_prompt_unchanged():
    stage, llm, gh = _stage([_VALID])

    stage.run(1, "the issue body", repo_files={"src/aorc/foo.py": "def foo(): pass"})

    sent_messages, _kwargs = llm.calls[0]
    sent_text = "\n".join(m.content for m in sent_messages)
    assert "Blast radius" not in sent_text


def test_graphify_blast_radius_included_in_design_prompt():
    from aorc.graphify import MockGraphifyClient

    graphify = MockGraphifyClient(edges={"src/aorc/foo.py": {"src/aorc/bar.py"}})
    stage, llm, gh = _stage([_VALID], graphify=graphify)

    stage.run(1, "the issue body", repo_files={"src/aorc/foo.py": "def foo(): pass"})

    sent_messages, _kwargs = llm.calls[0]
    sent_text = "\n".join(m.content for m in sent_messages)
    assert "src/aorc/bar.py" in sent_text


def test_graphify_query_failure_noted_but_does_not_block_design():
    from aorc.graphify import MockGraphifyClient

    graphify = MockGraphifyClient()
    graphify.fail_next = True
    stage, llm, gh = _stage([_VALID], graphify=graphify)

    result = stage.run(1, "the issue body", repo_files={"src/aorc/foo.py": "def foo(): pass"})

    assert result.status == "proceed"
    sent_messages, _kwargs = llm.calls[0]
    sent_text = "\n".join(m.content for m in sent_messages)
    assert "Blast radius query failed" in sent_text


def test_checkpoint_report_carries_files_from_design_doc():
    doc = parse_design_response(_VALID)

    report = checkpoint_report(7, doc)

    assert report.issue_number == 7
    assert report.files == ["src/aorc/add.py"]


def test_rebuild_in_flight_registry_reads_committed_design_docs():
    gh = MockGitHubClient(issues=[Issue(number=1), Issue(number=2)])
    gh.commit_file(branch_name(1), design_doc_path(1), _VALID, message="design: #1")

    registry = rebuild_in_flight_registry([1, 2], gh)

    assert registry.claimed_by_others(999) == {1: ["src/aorc/add.py"]}


def test_rebuild_in_flight_registry_skips_issues_with_no_design_doc_yet():
    gh = MockGitHubClient(issues=[Issue(number=1)])

    registry = rebuild_in_flight_registry([1], gh)

    assert registry.claimed_by_others(999) == {}


def test_rebuild_in_flight_registry_skips_unparseable_design_doc():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.commit_file(branch_name(1), design_doc_path(1), "not json", message="design: #1")

    registry = rebuild_in_flight_registry([1], gh)

    assert registry.claimed_by_others(999) == {}


def test_rebuild_in_flight_registry_is_a_fresh_registry_each_call():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.commit_file(branch_name(1), design_doc_path(1), _VALID, message="design: #1")

    first = rebuild_in_flight_registry([1], gh)
    second = rebuild_in_flight_registry([], gh)

    assert first.claimed_by_others(999) == {1: ["src/aorc/add.py"]}
    assert second.claimed_by_others(999) == {}
