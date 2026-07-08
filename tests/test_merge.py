"""S17 -- Merge-time handling + auto-rollback."""

from __future__ import annotations

import json

import pytest

from aorc.coder import CoderStage
from aorc.credentials import CredentialBroker
from aorc.design import design_doc_path
from aorc.github.mock import MockGitHubClient
from aorc.graphify import MockGraphifyClient
from aorc.guards import BLOCKED_LABEL
from aorc.harness import MockContainerRuntime
from aorc.interfaces import Comment, Issue, PullRequest
from aorc.llm.mock import MockLLMClient
from aorc.merge import (
    AGENT_AUTHOR,
    MergeTimeHandler,
    MockGitOps,
    classify_feedback_intent,
    feedback_marker,
    files_overlap,
    is_agent_comment,
    rollback_verdict,
)
from aorc.pipeline import DONE_COLUMN, LABEL_COLUMN, branch_name
from aorc.reviewer import ReviewerStage
from aorc.tester import MockTestRunner, TestRunResult as RunResult
from aorc.wake import WakeLoop

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEfakefakefakefakefakefakefake\n"
    "-----END RSA PRIVATE KEY-----"
)

_APPROVE = json.dumps({"verdict": "approve", "reason": "matches design"})
_REJECT = json.dumps({"verdict": "reject", "reason": "missing edge case"})


def design_json(files):
    return json.dumps(
        {
            "interface": [{"name": "add", "inputs": ["a", "b"], "outputs": "int"}],
            "test_specs": ["add(1, 2) == 3"],
            "task_list": ["implement add()"],
            "files": files,
            "confidence": 0.9,
        }
    )


def coder_task(path):
    return json.dumps(
        {"tasks": [{"task": "implement add()", "path": path, "code": "def add(a, b):\n    return a + b\n"}]}
    )


class CountingMinter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, private_key: str, repo: str, permissions: dict) -> str:
        self.calls.append((repo, dict(permissions)))
        return "ghs_" + f"{len(self.calls):04d}" + "a" * 32


class FakeWorktrees:
    def ensure(self, issue_number: int) -> str:
        return f"/worktrees/issue-{issue_number}"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_handler(
    issues=None,
    pulls=None,
    *,
    conflicts=(),
    moved=(),
    reviewer_responses=(),
    coder_responses=(),
    test_results=None,
    feedback_responses=(),
    graphify=None,
    use_worktrees=False,
):
    gh = MockGitHubClient(issues=issues, pulls=pulls)
    runtime = MockContainerRuntime()
    clock = Clock()
    broker = CredentialBroker(
        PRIVATE_KEY, CountingMinter(), llm_api_key="sk-" + "p" * 20, clock=clock
    )
    loop = WakeLoop.compose(
        gh, runtime, FakeWorktrees(), broker, repo="acme/widget", clock=clock
    )
    runner = MockTestRunner(results=test_results)
    coder_llm = MockLLMClient(responses=list(coder_responses))
    coder = CoderStage(coder_llm, loop.github, runner)
    reviewer_llm = MockLLMClient(responses=list(reviewer_responses))
    reviewer = ReviewerStage(reviewer_llm, coder, loop.github, runner)
    gitops = MockGitOps(conflicts=conflicts, moved=moved)
    feedback_llm = MockLLMClient(responses=list(feedback_responses))
    handler = MergeTimeHandler(
        loop,
        gitops,
        coder=coder,
        reviewer=reviewer,
        test_runner=runner,
        test_command="pytest -q",
        feedback_llm=feedback_llm,
        graphify=graphify,
        worktrees=FakeWorktrees() if use_worktrees else None,
    )
    handler_deps = {
        "gh": gh,
        "loop": loop,
        "runtime": runtime,
        "gitops": gitops,
        "coder_llm": coder_llm,
        "reviewer_llm": reviewer_llm,
        "feedback_llm": feedback_llm,
        "runner": runner,
    }
    return handler, handler_deps


def add_human_comment(gh, pr_number, comment_id, body, author="alice"):
    comment = Comment(id=comment_id, body=body, author=author, author_association="OWNER")
    gh.comments.setdefault(pr_number, []).append(comment)
    return comment


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_files_overlap_normalizes_paths():
    assert files_overlap(["./src/a.py"], ["src//a.py"]) is True
    assert files_overlap(["src/a.py"], ["src/b.py"]) is False
    assert files_overlap([], ["src/a.py"]) is False


def test_is_agent_comment_by_author_and_marker():
    assert is_agent_comment(Comment(id=1, body="looks off", author=AGENT_AUTHOR)) is True
    assert is_agent_comment(Comment(id=2, body="<!-- aorc:event ... -->", author="alice")) is True
    assert is_agent_comment(Comment(id=3, body="please fix this", author="alice")) is False


def test_classify_feedback_intent_mechanical_parse():
    assert classify_feedback_intent("wrong logic", MockLLMClient(responses=["code"])) == "code"
    assert classify_feedback_intent("test is wrong", MockLLMClient(responses=["Spec"])) == "spec"
    assert classify_feedback_intent("hmm", MockLLMClient(responses=["maybe both?"])) is None


# --------------------------------------------------------------------------- #
# rollback_verdict: overlap rule (PRD B18)
# --------------------------------------------------------------------------- #


def test_rollback_verdict_direct_overlap_requeues():
    assert rollback_verdict(["src/a.py"], ["./src/a.py"]) == "re-queue"


def test_rollback_verdict_no_overlap_continues():
    assert rollback_verdict(["src/a.py"], ["src/b.py"]) == "continue"


def test_rollback_verdict_no_checkpoint_is_conservative_requeue():
    assert rollback_verdict(["src/a.py"], None) == "re-queue"


def test_rollback_verdict_blast_radius_overlap_requeues():
    forward = MockGraphifyClient(edges={"src/a.py": {"src/b.py"}})
    assert rollback_verdict(["src/a.py"], ["src/b.py"], forward) == "re-queue"
    backward = MockGraphifyClient(edges={"src/b.py": {"src/a.py"}})
    assert rollback_verdict(["src/a.py"], ["src/b.py"], backward) == "re-queue"


def test_rollback_verdict_graphify_failure_is_conservative_requeue():
    graphify = MockGraphifyClient()
    graphify.fail_next = True
    assert rollback_verdict(["src/a.py"], ["src/b.py"], graphify) == "re-queue"


def test_rollback_verdict_clean_blast_radius_continues():
    graphify = MockGraphifyClient(edges={"src/a.py": {"src/c.py"}})
    assert rollback_verdict(["src/a.py"], ["src/b.py"], graphify) == "continue"


# --------------------------------------------------------------------------- #
# Issue auto-close on merge (-> Done)
# --------------------------------------------------------------------------- #


def test_merged_pr_closes_issue_moves_done_and_deletes_branch():
    issue = Issue(number=7, labels=["in-review"], body="issue body")
    pr = PullRequest(
        number=1001, head=branch_name(7), merged=True, state="closed", files=["src/a.py"]
    )
    handler, deps = make_handler(issues=[issue], pulls=[pr])

    report = handler.on_pr_merged(1001, "sha1")

    gh = deps["gh"]
    assert report.closed_issue == 7
    assert gh.issues[7].state == "closed"
    assert gh.board[7] == DONE_COLUMN
    assert ("delete_branch", branch_name(7)) in gh.calls  # merged -> delete (S4 rule)


def test_duplicate_merge_delivery_is_a_noop():
    issue = Issue(number=7, labels=["in-review"], body="issue body")
    pr = PullRequest(
        number=1001, head=branch_name(7), merged=True, state="closed", files=["src/a.py"]
    )
    handler, deps = make_handler(issues=[issue], pulls=[pr])

    first = handler.on_pr_merged(1001, "sha1")
    second = handler.on_pr_merged(1001, "sha1")

    assert first.duplicate is False
    assert second.duplicate is True
    closes = [c for c in deps["gh"].calls if c[0] == "close_issue"]
    assert closes == [("close_issue", 7)]


def test_non_aorc_pr_merge_still_wakes_but_closes_nothing():
    issue = Issue(number=7, labels=["in-review"], body="issue body")
    pr = PullRequest(
        number=1001, head="feature/human-branch", merged=True, state="closed", files=["src/a.py"]
    )
    handler, deps = make_handler(issues=[issue], pulls=[pr])

    report = handler.on_pr_merged(1001, "sha1")

    assert report.closed_issue is None
    assert report.wake is not None  # held sweep still ran
    assert deps["gh"].issues[7].state == "open"


def test_merge_reindexes_graphify():
    graphify = MockGraphifyClient()
    pr = PullRequest(
        number=1001, head="feature/human-branch", merged=True, state="closed", files=["src/a.py"]
    )
    handler, deps = make_handler(pulls=[pr], graphify=graphify)

    handler.on_pr_merged(1001, "sha1")

    assert graphify.reindex_calls == 1


# --------------------------------------------------------------------------- #
# Merge conflict at PR-open (ReviewerStage + GitOps, PRD B17)
# --------------------------------------------------------------------------- #


def _reviewer(reviewer_responses, gitops, gh=None):
    gh = gh or MockGitHubClient(issues=[Issue(number=1, body="issue body")])
    runner = MockTestRunner()
    coder = CoderStage(MockLLMClient(), gh, runner)
    reviewer_llm = MockLLMClient(responses=list(reviewer_responses))
    stage = ReviewerStage(reviewer_llm, coder, gh, runner, gitops=gitops)
    return stage, gh, reviewer_llm


def _design_doc():
    from aorc.design import parse_design_response

    return parse_design_response(design_json(["src/aorc/add.py"]))


def test_pr_open_with_unmoved_main_opens_directly():
    gitops = MockGitOps()
    stage, gh, reviewer_llm = _reviewer([_APPROVE], gitops)

    result = stage.run(1, _design_doc(), "issue body")

    assert result.status == "proceed"
    assert result.pr is not None
    assert gitops.calls == [("rebase", branch_name(1), "main")]


def test_clean_rebase_reruns_reviewer_then_opens_pr():
    gitops = MockGitOps(moved={branch_name(1)})
    stage, gh, reviewer_llm = _reviewer([_APPROVE, _APPROVE], gitops)

    result = stage.run(1, _design_doc(), "issue body")

    assert result.status == "proceed"
    assert result.attempts == 2  # approve -> clean rebase -> re-review -> open
    assert len(reviewer_llm.calls) == 2
    assert len(gh.list_pull_requests(state="open")) == 1
    rebases = [c for c in gitops.calls if c[0] == "rebase"]
    assert len(rebases) == 2  # "clean" the first time, "up-to-date" the second


def test_true_conflict_blocks_agent_and_opens_no_pr():
    gitops = MockGitOps(conflicts={branch_name(1)})
    stage, gh, reviewer_llm = _reviewer([_APPROVE], gitops)

    result = stage.run(1, _design_doc(), "issue body")

    assert result.status == "agent-blocked"
    assert result.pr is None
    assert gh.list_pull_requests(state="open") == []
    assert BLOCKED_LABEL in gh.issues[1].labels
    assert gh.board[1] == LABEL_COLUMN[BLOCKED_LABEL]
    bodies = [c.body for c in gh.list_comments(1)]
    assert any("merge conflict with main" in b for b in bodies)


def test_run_against_existing_pr_opens_no_new_pr():
    gh = MockGitHubClient(
        issues=[Issue(number=1, body="issue body")],
        pulls=[PullRequest(number=1001, head=branch_name(1), files=["src/aorc/add.py"])],
    )
    gitops = MockGitOps(conflicts={branch_name(1)})  # would block if consulted
    stage, gh, reviewer_llm = _reviewer([_APPROVE], gitops, gh=gh)
    existing = gh.get_pull_request(1001)

    result = stage.run(1, _design_doc(), "issue body", pr=existing)

    assert result.status == "proceed"
    assert result.pr is existing
    assert gitops.calls == []  # caller already rebased; no second rebase, no conflict
    assert len(gh.list_pull_requests(state="open")) == 1
    assert any("approve" in c.body for c in gh.list_comments(1001))


# --------------------------------------------------------------------------- #
# Stale approved PR (PRD B16)
# --------------------------------------------------------------------------- #


def _stale_setup(**kwargs):
    issues = [
        Issue(number=7, labels=["in-review"], body="merged issue"),
        Issue(number=8, labels=["in-review"], body="stale issue"),
        Issue(number=9, labels=["in-review"], body="untouched issue"),
    ]
    pulls = [
        PullRequest(
            number=1001, head=branch_name(7), merged=True, state="closed", files=["src/shared.py"]
        ),
        PullRequest(number=1002, head=branch_name(8), files=["src/shared.py", "src/b.py"]),
        PullRequest(number=1003, head=branch_name(9), files=["docs/readme.md"]),
    ]
    handler, deps = make_handler(issues=issues, pulls=pulls, **kwargs)
    deps["gh"].add_file(branch_name(8), design_doc_path(8), design_json(["src/b.py"]))
    return handler, deps


def test_overlapping_pr_rebased_retested_rereviewed_stays_approved():
    handler, deps = _stale_setup(reviewer_responses=[_APPROVE])

    report = handler.on_pr_merged(1001, "sha1")

    assert report.stale == {1002: "approved"}
    gh, gitops = deps["gh"], deps["gitops"]
    assert ("rebase", branch_name(8), "main") in gitops.calls
    # the non-overlapping PR was never touched
    assert all(branch_name(9) not in c for c in gitops.calls)
    assert deps["runner"].calls == [(".", "pytest -q")]  # re-test against new HEAD
    assert len(deps["reviewer_llm"].calls) == 1
    assert gh.pulls[1002].state == "open"  # stays approved, waiting for the human
    assert gh.pulls[1003].state == "open"


def test_stale_pr_recheck_uses_the_per_issue_worktree_when_wired(monkeypatch):
    """S27: with a real `WorktreeManager` given, the stale-PR recheck must
    run against issue 8's own worktree, not the single fixed `cwd="."`
    every issue used to share -- that fixed path is what would make
    `ContainerTestRunner` (docker exec into the wrong/no container, or
    none at all) impossible to wire in correctly here."""
    handler, deps = _stale_setup(reviewer_responses=[_APPROVE], use_worktrees=True)

    handler.on_pr_merged(1001, "sha1")

    assert deps["runner"].calls == [(f"/worktrees/issue-8", "pytest -q")]


def test_stale_pr_rebase_conflict_blocks_agent_but_never_closes_pr():
    handler, deps = _stale_setup(conflicts={branch_name(8)})

    report = handler.on_pr_merged(1001, "sha1")

    gh = deps["gh"]
    assert report.stale == {1002: "agent-blocked"}
    assert BLOCKED_LABEL in gh.issues[8].labels
    assert gh.pulls[1002].state == "open"  # never auto-closed as superseded
    assert len(deps["reviewer_llm"].calls) == 0


def test_stale_pr_broken_by_merge_reenters_fix_loop_then_reapproves():
    handler, deps = _stale_setup(
        reviewer_responses=[_APPROVE],
        coder_responses=[coder_task("src/b.py")],
        test_results=[
            RunResult(returncode=1, stdout="1 failed"),  # re-test against new HEAD: red
            RunResult(returncode=0),  # coder fix-loop toolchain: green
        ],
    )

    report = handler.on_pr_merged(1001, "sha1")

    assert report.stale == {1002: "approved"}
    coder_llm = deps["coder_llm"]
    assert len(coder_llm.calls) == 1
    sent = "\n".join(m.content for m in coder_llm.calls[0][0])
    assert "1 failed" in sent  # the breakage fed the fix loop as feedback
    assert deps["gh"].get_file("src/b.py", branch_name(8)) is not None


def test_stale_pr_fix_loop_exhaustion_blocks_agent():
    handler, deps = _stale_setup(
        coder_responses=["garbage", "garbage", "garbage"],
        test_results=[RunResult(returncode=1, stdout="1 failed")],
    )

    report = handler.on_pr_merged(1001, "sha1")

    assert report.stale == {1002: "agent-blocked"}
    assert BLOCKED_LABEL in deps["gh"].issues[8].labels


# --------------------------------------------------------------------------- #
# Human PR feedback routing (PRD B22)
# --------------------------------------------------------------------------- #


def _feedback_setup(**kwargs):
    issues = [Issue(number=7, labels=["in-review"], body="issue body")]
    pulls = [PullRequest(number=1001, head=branch_name(7), files=["src/a.py"])]
    handler, deps = make_handler(issues=issues, pulls=pulls, **kwargs)
    deps["gh"].add_file(branch_name(7), design_doc_path(7), design_json(["src/a.py"]))
    return handler, deps


def test_code_intent_routes_to_coder_fix_loop_with_tests_locked():
    handler, deps = _feedback_setup(
        feedback_responses=["code"], coder_responses=[coder_task("src/a.py")]
    )
    add_human_comment(deps["gh"], 1001, 42, "wrong logic in add(): handle negatives")

    report = handler.on_pr_comment(1001, 42)

    assert report.action == "code"
    assert report.issue == 7
    assert report.fixed is True
    coder_llm = deps["coder_llm"]
    assert len(coder_llm.calls) == 1
    sent = "\n".join(m.content for m in coder_llm.calls[0][0])
    assert "handle negatives" in sent
    # tests stay locked: the fix loop only ever committed the design's files
    committed = [c[2] for c in deps["gh"].calls if c[0] == "commit_file"]
    assert committed == ["src/a.py"]


def test_spec_intent_kicks_back_to_design_and_redispatches():
    handler, deps = _feedback_setup(feedback_responses=["spec"])
    add_human_comment(deps["gh"], 1001, 42, "this test asserts the wrong behavior")

    report = handler.on_pr_comment(1001, 42)

    gh = deps["gh"]
    assert report.action == "spec"
    assert "in-review" not in gh.issues[7].labels
    assert "in-design" in gh.issues[7].labels
    assert gh.board[7] == LABEL_COLUMN["in-design"]
    assert 7 in deps["loop"].in_flight  # re-runs forward from Design
    assert ("start", 7, branch_name(7)) in deps["runtime"].calls


def test_agent_own_comment_never_self_triggers():
    handler, deps = _feedback_setup(feedback_responses=["code"])
    comment = add_human_comment(
        deps["gh"], 1001, 42, "attempt 1: reject -- missing edge case", author=AGENT_AUTHOR
    )

    report = handler.on_pr_comment(1001, comment.id)

    assert report.action == "ignored"
    assert deps["feedback_llm"].calls == []  # not even classified
    assert deps["coder_llm"].calls == []


def test_duplicate_comment_delivery_is_handled_once():
    handler, deps = _feedback_setup(
        feedback_responses=["code", "code"], coder_responses=[coder_task("src/a.py")]
    )
    add_human_comment(deps["gh"], 1001, 42, "wrong logic")

    first = handler.on_pr_comment(1001, 42)
    second = handler.on_pr_comment(1001, 42)

    assert first.action == "code"
    assert second.action == "duplicate"
    assert len(deps["coder_llm"].calls) == 1


def test_unclassifiable_feedback_is_not_marked_handled():
    handler, deps = _feedback_setup(feedback_responses=["shrug"])
    add_human_comment(deps["gh"], 1001, 42, "interesting")

    report = handler.on_pr_comment(1001, 42)

    assert report.action == "unrouted"
    bodies = [c.body for c in deps["gh"].list_comments(1001)]
    assert not any(feedback_marker(42) in b for b in bodies)  # a later wake retries


# --------------------------------------------------------------------------- #
# Auto-rollback vs in-flight containers (PRD B18, safety 9)
# --------------------------------------------------------------------------- #


def _rollback_setup(graphify=None):
    issues = [
        Issue(number=7, labels=["in-review"], body="offending issue"),
        Issue(number=8, body="overlapping in-flight"),
        Issue(number=9, body="unrelated in-flight"),
        Issue(number=10, body="no checkpoint yet"),
    ]
    pulls = [
        PullRequest(
            number=1001, head=branch_name(7), merged=True, state="closed", files=["src/shared.py"]
        )
    ]
    handler, deps = make_handler(issues=issues, pulls=pulls, graphify=graphify)
    gh, loop = deps["gh"], deps["loop"]
    gh.add_file(branch_name(8), design_doc_path(8), design_json(["src/shared.py"]))
    gh.add_file(branch_name(9), design_doc_path(9), design_json(["src/other.py"]))
    # issue 10 has no design doc committed: no checkpoint reached yet
    for number in (8, 9, 10):
        loop.dispatch_issue(number)
    return handler, deps


def test_rollback_reverts_pr_and_requeues_by_overlap():
    handler, deps = _rollback_setup()

    report = handler.on_main_broken(1001)

    assert ("revert", 1001) in deps["gitops"].calls
    assert report.requeued == [8, 10]  # overlap + conservative no-checkpoint
    assert report.continued == [9]
    runtime = deps["runtime"]
    assert ("teardown", 8) in runtime.calls
    assert ("teardown", 10) in runtime.calls
    assert ("teardown", 9) not in runtime.calls
    # torn down through the branch-preserving path, then re-dispatched
    assert ("delete_branch", branch_name(8)) not in deps["gh"].calls
    assert runtime.calls.count(("start", 8, branch_name(8))) == 2
    assert 8 in deps["loop"].in_flight and 9 in deps["loop"].in_flight


def test_rollback_blast_radius_overlap_requeues_via_graphify():
    graphify = MockGraphifyClient(edges={"src/shared.py": {"src/other.py"}})
    handler, deps = _rollback_setup(graphify=graphify)

    report = handler.on_main_broken(1001)

    assert 9 in report.requeued  # indirect (blast-radius) overlap


def test_rollback_requeued_issue_gets_fresh_token():
    handler, deps = _rollback_setup()
    old_token = deps["loop"].in_flight[8][1]

    handler.on_main_broken(1001)

    new_token = deps["loop"].in_flight[8][1]
    assert new_token.token != old_token.token
