"""S22 -- Build-pipeline driver + worktree/API split-brain fix."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aorc.coder import CoderStage
from aorc.design import DesignDoc, DesignStage, design_doc_path
from aorc.driver import PipelineDriver
from aorc.github.mock import MockGitHubClient
from aorc.guards import BLOCKED_LABEL
from aorc.harness import Checkpoint, WorktreeManager
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.pipeline import HELD_LABEL, branch_name
from aorc.reviewer import ReviewerStage
from aorc.tester import (
    MockTestRunner,
    SubprocessTestRunner,
    TesterStage as TestStage,
    TestRunResult as RunResult,
    generated_test_path,
    marker_path,
)

_DESIGN = {
    "interface": [{"name": "add", "inputs": ["a", "b"], "outputs": "int"}],
    "test_specs": ["add(1, 2) == 3"],
    "task_list": ["implement add()"],
    "files": ["src/aorc/add.py"],
    "confidence": 0.9,
}
_DESIGN_JSON = json.dumps(_DESIGN)
_LOW_CONFIDENCE_DESIGN_JSON = json.dumps({**_DESIGN, "confidence": 0.1})
_ONE_TEST = json.dumps(
    {"tests": [{"task": "implement add()", "code": "def test_add():\n    assert add(1, 2) == 3\n"}]}
)
_CRITIC_APPROVE = json.dumps({"verdict": "approve", "reason": "matches spec"})
_ONE_TASK = json.dumps(
    {"tasks": [{"task": "implement add()", "path": "src/aorc/add.py", "code": "def add(a, b):\n    return a + b\n"}]}
)
_REVIEW_APPROVE = json.dumps({"verdict": "approve", "reason": "looks good"})

# tester's toolchain run (must be a clean "red"), then coder's (green).
_DEFAULT_TEST_RESULTS = [
    RunResult(returncode=1, stdout="AssertionError: assert 4 == 3"),
    RunResult(returncode=0),
]


class FakeWorktrees:
    """A single fixed path -- real on-disk writes still happen (S22's
    `write_worktree_file` only skips the stages' own `cwd="."` default), so
    this proves the driver actually threads one shared `cwd` through every
    stage without needing a real git worktree for the sequencing tests
    below. The real-git split-brain proof is a separate test further down."""

    def __init__(self, path: str) -> None:
        self._path = path

    def ensure(self, issue_number: int) -> str:
        return self._path


def _build_driver(
    tmp_path,
    *,
    design_responses=None,
    tester_responses=None,
    critic_responses=None,
    coder_responses=None,
    reviewer_responses=None,
    test_results=None,
    issues=None,
    with_checkpoint=False,
):
    gh = MockGitHubClient(issues=issues or [Issue(number=1, body="add two numbers")])
    llms = {
        "design": MockLLMClient(responses=_or_default(design_responses, [_DESIGN_JSON])),
        "tester": MockLLMClient(responses=_or_default(tester_responses, [_ONE_TEST])),
        "critic": MockLLMClient(responses=_or_default(critic_responses, [_CRITIC_APPROVE])),
        "coder": MockLLMClient(responses=_or_default(coder_responses, [_ONE_TASK])),
        "reviewer": MockLLMClient(responses=_or_default(reviewer_responses, [_REVIEW_APPROVE])),
    }
    runner = MockTestRunner(results=_or_default(test_results, list(_DEFAULT_TEST_RESULTS)))
    design = DesignStage(llms["design"], gh)
    tester = TestStage(llms["tester"], llms["critic"], gh, runner)
    coder = CoderStage(llms["coder"], gh, runner)
    reviewer = ReviewerStage(llms["reviewer"], coder, gh, runner)
    checkpoint = Checkpoint(github=gh).verdict if with_checkpoint else None
    driver = PipelineDriver(
        gh, FakeWorktrees(str(tmp_path)), design, tester, coder, reviewer, checkpoint=checkpoint
    )
    return driver, gh, llms, runner


def _or_default(value, default):
    return default if value is None else value


# ---- full sequencing -------------------------------------------------------- #


def test_full_sequence_runs_all_four_stages_and_opens_pr(tmp_path):
    driver, gh, llms, runner = _build_driver(tmp_path)

    result = driver.run(1)

    assert result.status == "proceed"
    assert result.stage == "in-review"
    assert result.pr is not None
    assert gh.get_labels(1) == ["in-review"]
    assert len(llms["design"].calls) == 1
    assert len(llms["tester"].calls) == 1
    assert len(llms["coder"].calls) == 1
    assert len(llms["reviewer"].calls) == 1
    committed = gh.get_file("src/aorc/add.py", branch_name(1))
    assert committed is not None and "return a + b" in committed


def test_needs_clarification_labels_issue_and_stops_before_tester(tmp_path):
    driver, gh, llms, runner = _build_driver(tmp_path, design_responses=[_LOW_CONFIDENCE_DESIGN_JSON])

    result = driver.run(1)

    assert result.status == "needs-clarification"
    assert "needs-clarification" in gh.get_labels(1)
    assert len(llms["tester"].calls) == 0


def test_coder_failure_labels_agent_blocked_and_preserves_branch(tmp_path):
    driver, gh, llms, runner = _build_driver(
        tmp_path,
        coder_responses=["garbage", "garbage", "garbage"],
        test_results=[RunResult(returncode=1, stdout="AssertionError")],
    )

    result = driver.run(1)

    assert result.status == "agent-blocked"
    assert result.stage == "in-code"
    assert BLOCKED_LABEL in gh.get_labels(1)
    assert not any(call[0] == "delete_branch" for call in gh.calls)


# ---- idempotent resume (S16 discipline) ------------------------------------ #


def test_resume_skips_design_when_doc_already_committed(tmp_path):
    driver, gh, llms, runner = _build_driver(tmp_path, design_responses=[])
    gh.issues[1].labels.append("in-design")
    gh.add_file(branch_name(1), design_doc_path(1), _DESIGN_JSON)

    result = driver.run(1)

    assert result.status == "proceed"
    assert len(llms["design"].calls) == 0
    assert len(llms["tester"].calls) == 1


def test_resume_skips_tester_when_tests_already_committed(tmp_path):
    driver, gh, llms, runner = _build_driver(
        tmp_path, tester_responses=[], critic_responses=[], test_results=[RunResult(returncode=0)]
    )
    gh.issues[1].labels.append("in-test")
    gh.add_file(branch_name(1), design_doc_path(1), _DESIGN_JSON)
    gh.add_file(branch_name(1), marker_path(1), "committed")

    result = driver.run(1)

    assert result.status == "proceed"
    assert len(llms["tester"].calls) == 0
    assert len(llms["coder"].calls) == 1


def test_in_code_always_reruns_coder_even_when_resumed_there(tmp_path):
    """`ArtifactChecker.artifact_exists` always reports `True` for "in-code"
    (no static artifact for this stage, by original S2 design) -- the driver
    must not trust that as "already done" the way it does for the other
    three stages, or it would skip the coder entirely on any resume."""
    driver, gh, llms, runner = _build_driver(tmp_path, test_results=[RunResult(returncode=0)])
    gh.issues[1].labels.append("in-code")
    gh.add_file(branch_name(1), design_doc_path(1), _DESIGN_JSON)
    gh.add_file(branch_name(1), marker_path(1), "committed")

    result = driver.run(1)

    assert result.status == "proceed"
    assert len(llms["design"].calls) == 0
    assert len(llms["tester"].calls) == 0
    assert len(llms["coder"].calls) == 1


def test_resume_reviews_existing_pr_instead_of_opening_a_second_one(tmp_path):
    driver, gh, llms, runner = _build_driver(tmp_path, test_results=[])
    gh.issues[1].labels.append("in-review")
    gh.add_file(branch_name(1), design_doc_path(1), _DESIGN_JSON)
    gh.add_file(branch_name(1), marker_path(1), "committed")
    pr = gh.open_pull_request(title="AORC: issue #1", body="x", head=branch_name(1))

    result = driver.run(1)

    assert result.status == "proceed"
    assert result.pr.number == pr.number
    assert len(gh.list_pull_requests(state="open")) == 1
    assert len(llms["design"].calls) == 0
    assert len(llms["coder"].calls) == 0


def test_terminal_label_is_a_noop(tmp_path):
    driver, gh, llms, runner = _build_driver(
        tmp_path,
        design_responses=[],
        tester_responses=[],
        critic_responses=[],
        coder_responses=[],
        reviewer_responses=[],
        test_results=[],
    )
    gh.issues[1].labels.append(BLOCKED_LABEL)

    result = driver.run(1)

    assert result.status == BLOCKED_LABEL
    assert all(len(llm.calls) == 0 for llm in llms.values())
    assert len(runner.calls) == 0


# ---- S22 split-brain fix, real git + real subprocess toolchain ------------- #


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@a.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_real_worktree_and_subprocess_toolchain_see_coders_committed_code(tmp_path):
    """The literal S22 acceptance criterion, with no mock standing in for
    the piece that matters: a real git worktree (`WorktreeManager` against a
    real throwaway repo, `tests/test_gitops.py`'s style) and a real
    `SubprocessTestRunner` executing a real Python process. Before the S22
    fix, `add.py` was committed only to `MockGitHubClient`'s in-memory store
    and never reached this directory -- the `python3 -c "import add"`
    command below would fail with `ModuleNotFoundError`."""
    repo = _init_repo(tmp_path)
    worktrees = WorktreeManager(str(repo), str(tmp_path / "worktrees"))
    design = DesignDoc(
        interface=[{"name": "add", "inputs": ["a", "b"], "outputs": "int"}],
        test_specs=["add(1, 2) == 3"],
        task_list=["implement add()"],
        files=["add.py"],
        confidence=0.9,
    )
    gh = MockGitHubClient(issues=[Issue(number=1)])
    gh.create_branch(branch_name(1))  # S29: the driver's job, done in setup here
    coder_llm = MockLLMClient(
        responses=[
            json.dumps(
                {
                    "tasks": [
                        {
                            "task": "implement add()",
                            "path": "add.py",
                            "code": "def add(a, b):\n    return a + b\n",
                        }
                    ]
                }
            )
        ]
    )
    runner = SubprocessTestRunner()
    coder = CoderStage(
        coder_llm,
        gh,
        runner,
        test_command="python3 -c \"import add; assert add.add(1, 2) == 3\"",
    )

    cwd = worktrees.ensure(1)
    result = coder.run(1, design, cwd=cwd)

    assert result.status == "proceed"
    assert result.attempts == 1
    assert (Path(cwd) / "add.py").read_text() == "def add(a, b):\n    return a + b\n"


# ---- S26 -- fetch remote branch on resume from a fresh worktree ------------ #


def _real_git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_bare_remote(tmp_path):
    remote = tmp_path / "remote.git"
    _real_git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _real_git(["init", "-b", "main"], cwd=seed)
    _real_git(["config", "user.email", "a@a.com"], cwd=seed)
    _real_git(["config", "user.name", "a"], cwd=seed)
    (seed / "README.md").write_text("hi\n")
    _real_git(["add", "."], cwd=seed)
    _real_git(["commit", "-m", "init"], cwd=seed)
    _real_git(["remote", "add", "origin", str(remote)], cwd=seed)
    _real_git(["push", "origin", "main"], cwd=seed)
    return remote


def _real_clone(remote, dest):
    _real_git(["clone", str(remote), str(dest)], cwd=dest.parent)
    _real_git(["config", "user.email", "a@a.com"], cwd=dest)
    _real_git(["config", "user.name", "a"], cwd=dest)
    return dest


# ---- S29 -- create the issue branch before the first commit_file ----------- #


def test_fresh_dispatch_creates_issue_branch_before_first_commit(tmp_path):
    """Live run-issue finding: real GitHub 404s the design doc commit because
    nothing ever creates `aorc/issue-<n>` on the remote. The driver must
    create it (off the default branch) before any stage's `commit_file`."""
    driver, gh, llms, runner = _build_driver(tmp_path)

    result = driver.run(1)

    assert result.status == "proceed"
    assert ("create_branch", branch_name(1), "main") in gh.calls
    call_kinds = [c[0] for c in gh.calls]
    assert call_kinds.index("create_branch") < call_kinds.index("commit_file")
    assert gh.get_file(design_doc_path(1), branch_name(1)) is not None


def test_fresh_dispatch_with_real_remote_commits_design_doc_to_created_branch(tmp_path):
    """S29 acceptance, S26-style harness: a real throwaway bare remote backs
    the worktree, the hardened mock stands in for the contents API. A fresh
    issue's very first dispatch must succeed end-to-end -- before the fix,
    the design doc commit dies on the nonexistent branch exactly like the
    live 404."""
    remote = _init_bare_remote(tmp_path)
    local_clone = _real_clone(remote, tmp_path / "local")

    gh = MockGitHubClient(issues=[Issue(number=1, body="add two numbers")])
    runner = MockTestRunner(results=list(_DEFAULT_TEST_RESULTS))
    design = DesignStage(MockLLMClient(responses=[_DESIGN_JSON]), gh)
    tester = TestStage(
        MockLLMClient(responses=[_ONE_TEST]), MockLLMClient(responses=[_CRITIC_APPROVE]), gh, runner
    )
    coder = CoderStage(MockLLMClient(responses=[_ONE_TASK]), gh, runner)
    reviewer = ReviewerStage(MockLLMClient(responses=[_REVIEW_APPROVE]), coder, gh, runner)
    worktrees = WorktreeManager(str(local_clone), str(tmp_path / "worktrees"))
    driver = PipelineDriver(gh, worktrees, design, tester, coder, reviewer)

    result = driver.run(1)

    assert result.status == "proceed"
    assert gh.get_file(design_doc_path(1), branch_name(1)) is not None


def test_driver_resume_at_in_code_with_fresh_worktree_sees_earlier_committed_test(tmp_path):
    """The literal S26 acceptance criterion: driver resumes at "in-code" --
    `ArtifactChecker.tests_committed` reports True via `MockGitHubClient`'s
    own in-memory marker (the API-side record of the earlier `TesterStage`
    run) -- but the worktree for this issue has never been created on this
    machine. Before the S26 fix, `WorktreeManager.ensure` would build a
    worktree with no knowledge of the generated test file an earlier
    attempt actually pushed to the real branch, so the coder's toolchain
    would find no tests to run at all (and could go green vacuously)."""
    remote = _init_bare_remote(tmp_path)
    local_clone = _real_clone(remote, tmp_path / "local")

    branch = branch_name(1)
    machine_a = _real_clone(remote, tmp_path / "machine-a")
    _real_git(["checkout", "-b", branch], cwd=machine_a)
    test_dir = machine_a / "tests"
    test_dir.mkdir(parents=True)
    (test_dir / "test_aorc_issue_1.py").write_text("def test_from_earlier_attempt():\n    assert True\n")
    _real_git(["add", "."], cwd=machine_a)
    _real_git(["commit", "-m", "tests"], cwd=machine_a)
    _real_git(["push", "origin", branch], cwd=machine_a)

    gh = MockGitHubClient(issues=[Issue(number=1, body="add two numbers")])
    gh.issues[1].labels.append("in-code")
    gh.add_file(branch_name(1), design_doc_path(1), _DESIGN_JSON)
    gh.add_file(branch_name(1), marker_path(1), "committed")

    runner = SubprocessTestRunner()
    coder = CoderStage(
        MockLLMClient(responses=[_ONE_TASK]), gh, runner, test_command="pytest -q"
    )
    reviewer = ReviewerStage(MockLLMClient(responses=[_REVIEW_APPROVE]), coder, gh, runner)
    design = DesignStage(MockLLMClient(responses=[]), gh)
    tester = TestStage(MockLLMClient(responses=[]), MockLLMClient(responses=[]), gh, runner)
    worktrees = WorktreeManager(str(local_clone), str(tmp_path / "worktrees"))
    driver = PipelineDriver(gh, worktrees, design, tester, coder, reviewer)

    result = driver.run(1)

    assert result.status == "proceed"
    cwd = worktrees.ensure(1)
    assert (Path(cwd) / "tests" / "test_aorc_issue_1.py").exists()


# ---- S42: live repro -- unqualified design paths poison every stage -------- #


def test_unqualified_design_file_path_is_resolved_before_the_tester_runs(tmp_path):
    """Reproduces the live issue-21 failure end-to-end: the design LLM said
    files=["math_utils.py"] while the real module is src/sandbox/
    math_utils.py. Before S42 the whole chain was consistently wrong --
    module derived as "math_utils", prompt taught the model the wrong
    module, header imported it, the stripper kept it, the stub landed at
    the repo root -- and S41's unit tests (fed a well-formed design)
    couldn't see any of it."""
    (tmp_path / "src" / "sandbox").mkdir(parents=True)
    (tmp_path / "src" / "sandbox" / "math_utils.py").write_text(
        "def multiply(a, b):\n    return a * b\n"
    )
    bad_path_design = json.dumps(
        {
            "interface": [{"name": "divide", "inputs": ["a", "b"], "outputs": "float"}],
            "test_specs": ["divide(10, 2) == 5.0"],
            "task_list": ["implement divide()"],
            "files": ["math_utils.py"],  # the live LLM's unqualified guess
            "confidence": 0.9,
        }
    )
    wrong_import_tests = json.dumps(
        {"tests": [{"spec": "divide(10, 2) == 5.0",
                    "code": "from math_utils import divide\n\ndef test_divide():\n    assert divide(10, 2) == 5.0\n"}]}
    )
    one_task = json.dumps(
        {"tasks": [{"task": "implement divide()", "path": "src/sandbox/math_utils.py",
                    "code": "def multiply(a, b):\n    return a * b\n\ndef divide(a, b):\n    return a / b\n"}]}
    )
    driver, gh, llms, runner = _build_driver(
        tmp_path,
        design_responses=[bad_path_design],
        tester_responses=[wrong_import_tests],
        coder_responses=[one_task],
        test_results=[
            RunResult(returncode=1, stdout="FAILED - NotImplementedError"),  # tester: red
            RunResult(returncode=0),  # coder: green
        ],
    )

    result = driver.run(1)

    assert result.status == "proceed"
    committed_test = gh.get_file("tests/test_aorc_issue_1.py", branch_name(1))
    assert committed_test.startswith("from sandbox.math_utils import divide")
    assert "from math_utils import" not in committed_test
    # the stub was appended to the REAL module, and no stray root file exists
    stub_target = gh.get_file("src/sandbox/math_utils.py", branch_name(1))
    assert stub_target is not None and "divide" in stub_target
    assert gh.get_file("math_utils.py", branch_name(1)) is None


# ---- S43: post-design checkpoint -- live multi-issue collision repro ------- #


def test_second_issue_touching_same_file_is_held_at_checkpoint(tmp_path):
    """The live issues-24/25 repro at the driver level: two issues whose
    design docs claim the same file, run through the SAME checkpoint (as the
    live loop's shared harness provides). The first proceeds and records its
    claim; the second must hold at the post-design checkpoint -- never reach
    the tester."""
    driver, gh, llms, runner = _build_driver(
        tmp_path,
        issues=[
            Issue(number=1, body="add two numbers"),
            Issue(number=2, body="add two numbers, again"),
        ],
        design_responses=[_DESIGN_JSON, _DESIGN_JSON],  # both claim src/aorc/add.py
        with_checkpoint=True,
    )

    first = driver.run(1)
    assert first.status == "proceed"

    second = driver.run(2)

    assert second.status == "held"
    assert HELD_LABEL in gh.get_labels(2)
    assert len(llms["tester"].calls) == 1  # only issue 1's tester ever ran
    bodies = [c.body for c in gh.list_comments(2)]
    assert any("checkpoint" in b.lower() for b in bodies), "hold must leave a breadcrumb"


def test_checkpoint_holds_on_open_aorc_pr_files_even_with_empty_registry(tmp_path):
    """Restart analog: the in-process registry is empty (fresh orchestrator),
    but another issue's unmerged AORC PR already occupies the file. The
    checkpoint's open-PR scan must hold the newcomer."""
    driver, gh, llms, runner = _build_driver(
        tmp_path,
        issues=[Issue(number=2, body="add two numbers, again")],
        design_responses=[_DESIGN_JSON],
        with_checkpoint=True,
    )
    pr = gh.open_pull_request(title="AORC: issue #1", body="x", head=branch_name(1))
    pr.files = ["src/aorc/add.py"]

    result = driver.run(2)

    assert result.status == "held"
    assert HELD_LABEL in gh.get_labels(2)
    assert len(llms["tester"].calls) == 0


# ---- S31: blocking posts the stage + reason to the issue ------------------- #


def test_blocked_tester_posts_stage_and_reason_comment(tmp_path):
    driver, gh, llms, runner = _build_driver(
        tmp_path, tester_responses=["not json", "not json", "not json"]
    )

    result = driver.run(1)

    assert result.status == "agent-blocked"
    assert result.stage == "in-test"
    assert "schema" in result.reason
    bodies = [c.body for c in gh.list_comments(1)]
    block_notes = [b for b in bodies if BLOCKED_LABEL in b]
    assert block_notes, "blocking must post an explanatory comment"
    assert any("in-test" in b and "schema" in b for b in block_notes)


def test_blocked_coder_posts_stage_and_reason_comment(tmp_path):
    driver, gh, llms, runner = _build_driver(
        tmp_path,
        coder_responses=["garbage", "garbage", "garbage"],
        test_results=[RunResult(returncode=1, stdout="AssertionError")],
    )

    result = driver.run(1)

    assert result.status == "agent-blocked"
    assert result.stage == "in-code"
    assert result.reason
    bodies = [c.body for c in gh.list_comments(1)]
    assert any("in-code" in b and BLOCKED_LABEL in b for b in bodies)


# ---- S44: live issue-29 repro -- coder rewrites shared files from a blank --- #
# ---- slate and deletes pre-existing code ------------------------------------ #


class _PreservingCoderLLM:
    """Stands in for a competent coder model: it can only preserve code it can
    SEE. Shown math_utils.py's current contents it returns a full file keeping
    every existing def; blind (the pre-S44 driver passed no repo_files), it
    emits just its own function -- exactly what the live model did."""

    def __init__(self) -> None:
        self.calls: list = []

    @property
    def model(self) -> str:
        return "mock-model"

    def complete(self, messages, *, max_tokens=1024, temperature=0.0):
        from aorc.interfaces import Completion

        self.calls.append((messages, {}))
        sent = "\n".join(m.content for m in messages)
        if "def multiply" in sent:
            code = (
                "def multiply(a, b):\n    return a * b\n\n\n"
                "def power(a, b):\n    return a ** b\n"
            )
        else:
            code = "def power(a, b):\n    return a ** b\n"
        response = json.dumps(
            {"tasks": [{"task": "implement power()", "path": "math_utils.py", "code": code}]}
        )
        return Completion(text=response, model="mock-model", finish_reason="stop")


def test_coder_preserves_preexisting_functions_in_shared_file(tmp_path):
    """Live issue-29 repro at the driver level: math_utils.py already defines
    multiply (an earlier issue's merged work); this issue's design covers only
    power. The coder writes FULL file contents, so unless the driver feeds it
    the file's current contents, it rewrites the module from a blank slate and
    multiply is deleted from both the worktree and the PR."""
    existing = "def multiply(a, b):\n    return a * b\n"
    (tmp_path / "math_utils.py").write_text(existing)

    gh = MockGitHubClient(issues=[Issue(number=1, body="add power(a, b) to math_utils.py")])
    # A real branch created off main carries the file; the mock's
    # create_branch copies nothing, so seed the branch-side copy directly.
    gh.add_file(branch_name(1), "math_utils.py", existing)

    power_design = json.dumps(
        {
            "interface": [{"name": "power", "inputs": ["a", "b"], "outputs": "int"}],
            "test_specs": ["power(2, 3) == 8"],
            "task_list": ["implement power()"],
            "files": ["math_utils.py"],
            "confidence": 0.9,
        }
    )
    power_test = json.dumps(
        {"tests": [{"task": "implement power()", "code": "def test_power():\n    assert power(2, 3) == 8\n"}]}
    )
    coder_llm = _PreservingCoderLLM()
    runner = MockTestRunner(results=list(_DEFAULT_TEST_RESULTS))
    design = DesignStage(MockLLMClient(responses=[power_design]), gh)
    tester = TestStage(
        MockLLMClient(responses=[power_test]), MockLLMClient(responses=[_CRITIC_APPROVE]), gh, runner
    )
    coder = CoderStage(coder_llm, gh, runner)
    reviewer = ReviewerStage(MockLLMClient(responses=[_REVIEW_APPROVE]), coder, gh, runner)
    driver = PipelineDriver(gh, FakeWorktrees(str(tmp_path)), design, tester, coder, reviewer)

    result = driver.run(1)

    assert result.status == "proceed"
    # The coder's prompt carried the file's current contents...
    coder_prompt = "\n".join(m.content for m in coder_llm.calls[0][0])
    assert "def multiply" in coder_prompt
    assert "preserve" in coder_prompt.lower()
    # ...and multiply survived in the worktree and the committed content.
    worktree_now = (tmp_path / "math_utils.py").read_text()
    assert "def multiply" in worktree_now and "def power" in worktree_now
    committed = gh.get_file("math_utils.py", branch_name(1))
    assert committed is not None
    assert "def multiply" in committed and "def power" in committed


def test_design_stage_sees_contents_of_files_the_issue_mentions(tmp_path):
    """S44, design half: an issue that names an existing file ("add power to
    math_utils.py") must put that file's current contents in front of the
    design agent, so the design accounts for the interfaces already there
    instead of describing the file as if it were new."""
    (tmp_path / "math_utils.py").write_text("def multiply(a, b):\n    return a * b\n")
    driver, gh, llms, runner = _build_driver(
        tmp_path,
        issues=[Issue(number=1, body="add power(a, b) to math_utils.py")],
        design_responses=[_LOW_CONFIDENCE_DESIGN_JSON],  # stop after design
    )

    driver.run(1)

    design_prompt = "\n".join(m.content for m in llms["design"].calls[0][0])
    assert "def multiply" in design_prompt
