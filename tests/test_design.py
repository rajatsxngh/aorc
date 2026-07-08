"""S5 -- Design stage + strict schema + actionability gate."""

from __future__ import annotations

import json

from aorc.design import (
    DesignStage,
    checkpoint_report,
    design_doc_path,
    mentioned_files,
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
    # S29: branch creation is the driver's job, before any stage runs.
    gh.create_branch(branch_name(1))
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
    gh.add_file(branch_name(1), design_doc_path(1), _VALID)

    registry = rebuild_in_flight_registry([1, 2], gh)

    assert registry.claimed_by_others(999) == {1: ["src/aorc/add.py"]}


def test_rebuild_in_flight_registry_skips_issues_with_no_design_doc_yet():
    gh = MockGitHubClient(issues=[Issue(number=1)])

    registry = rebuild_in_flight_registry([1], gh)

    assert registry.claimed_by_others(999) == {}


def test_rebuild_in_flight_registry_skips_unparseable_design_doc():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.add_file(branch_name(1), design_doc_path(1), "not json")

    registry = rebuild_in_flight_registry([1], gh)

    assert registry.claimed_by_others(999) == {}


def test_rebuild_in_flight_registry_is_a_fresh_registry_each_call():
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.add_file(branch_name(1), design_doc_path(1), _VALID)

    first = rebuild_in_flight_registry([1], gh)
    second = rebuild_in_flight_registry([], gh)

    assert first.claimed_by_others(999) == {1: ["src/aorc/add.py"]}
    assert second.claimed_by_others(999) == {}


# ---- S32: real Claude wraps JSON in markdown code fences ------------------- #


def test_strip_code_fences_passes_raw_json_through_unchanged():
    from aorc.interfaces import strip_code_fences

    assert strip_code_fences(_VALID) == _VALID


def test_strip_code_fences_unwraps_a_json_tagged_fence():
    from aorc.interfaces import strip_code_fences

    assert strip_code_fences(f"```json\n{_VALID}\n```") == _VALID


def test_strip_code_fences_unwraps_a_bare_fence_with_surrounding_prose():
    from aorc.interfaces import strip_code_fences

    text = f"Here is the design you asked for:\n```\n{_VALID}\n```\nHope this helps!"
    assert strip_code_fences(text) == _VALID


def test_parse_design_response_accepts_a_fenced_response():
    doc = parse_design_response(f"Sure!\n```json\n{_VALID}\n```")
    assert doc is not None
    assert doc.confidence == 0.9


# ---- S42: design file paths resolved against the real worktree tree -------- #


def test_resolve_design_files_snaps_a_bare_basename_to_its_real_path(tmp_path):
    from aorc.design import resolve_design_files

    (tmp_path / "src" / "sandbox").mkdir(parents=True)
    (tmp_path / "src" / "sandbox" / "math_utils.py").write_text("def multiply(a, b): ...\n")

    resolved = resolve_design_files(["math_utils.py"], str(tmp_path))

    assert resolved == ["src/sandbox/math_utils.py"]


def test_resolve_design_files_keeps_exact_existing_paths(tmp_path):
    from aorc.design import resolve_design_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("")

    assert resolve_design_files(["src/x.py"], str(tmp_path)) == ["src/x.py"]


def test_resolve_design_files_keeps_new_and_ambiguous_entries(tmp_path):
    from aorc.design import resolve_design_files

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "dup.py").write_text("")
    (tmp_path / "b" / "dup.py").write_text("")

    # brand-new file: nothing to snap to -> kept verbatim
    assert resolve_design_files(["src/new_module.py"], str(tmp_path)) == ["src/new_module.py"]
    # two candidates: ambiguous -> kept verbatim rather than guessed
    assert resolve_design_files(["dup.py"], str(tmp_path)) == ["dup.py"]


def test_resolve_design_files_ignores_vcs_and_cache_dirs(tmp_path):
    from aorc.design import resolve_design_files

    (tmp_path / ".git" / "sub").mkdir(parents=True)
    (tmp_path / ".git" / "sub" / "math_utils.py").write_text("")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "math_utils.py").write_text("")

    assert resolve_design_files(["math_utils.py"], str(tmp_path)) == ["src/math_utils.py"]


# ---- S44: files the issue text mentions ------------------------------------ #


def test_mentioned_files_resolves_paths_named_in_the_issue_body(tmp_path):
    (tmp_path / "src" / "sandbox").mkdir(parents=True)
    (tmp_path / "src" / "sandbox" / "math_utils.py").write_text(
        "def multiply(a, b):\n    return a * b\n"
    )

    body = "add power(a, b) to math_utils.py -- target python 3.12"
    assert mentioned_files(body, str(tmp_path)) == ["src/sandbox/math_utils.py"]


def test_mentioned_files_empty_when_issue_names_nothing_real(tmp_path):
    assert mentioned_files("make everything faster", str(tmp_path)) == []
    assert mentioned_files("touch ghost_module.py please", str(tmp_path)) == []
