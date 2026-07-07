"""S18 -- `LocalGitOps`: the real `GitOps` adapter, exercised against real
throwaway git repositories (no mocks -- this is the S17 wiring obligation
"real rebase execution against actual branches" made true)."""

from __future__ import annotations

import subprocess

import pytest

from aorc.gitops import LocalGitOps


def _git(repo, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real repo: `main` with one file, identity configured for commits."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "app.py").write_text("VERSION = 1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


def _commit(repo, filename, content, message):
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)


def test_rebase_reports_up_to_date_when_branch_contains_main(repo):
    _git(repo, "checkout", "-b", "aorc/issue-1")
    _commit(repo, "feature.py", "x = 1\n", "feature work")
    assert LocalGitOps(repo).rebase("aorc/issue-1").status == "up-to-date"


def test_rebase_applies_cleanly_when_main_moved_on_disjoint_files(repo):
    _git(repo, "checkout", "-b", "aorc/issue-1")
    _commit(repo, "feature.py", "x = 1\n", "feature work")
    _git(repo, "checkout", "main")
    _commit(repo, "other.py", "y = 2\n", "unrelated main work")

    result = LocalGitOps(repo).rebase("aorc/issue-1")

    assert result.status == "clean"
    # The rebased branch now sits on top of the moved main: it contains both
    # main's new file and its own commit.
    _git(repo, "checkout", "aorc/issue-1")
    assert (repo / "other.py").exists()
    assert (repo / "feature.py").exists()


def test_rebase_conflict_is_reported_and_aborted(repo):
    _git(repo, "checkout", "-b", "aorc/issue-1")
    _commit(repo, "app.py", "VERSION = 100\n", "branch edit")
    branch_sha = _git(repo, "rev-parse", "aorc/issue-1")
    _git(repo, "checkout", "main")
    _commit(repo, "app.py", "VERSION = 200\n", "conflicting main edit")

    result = LocalGitOps(repo).rebase("aorc/issue-1")

    assert result.status == "conflict"
    # Aborted, never left mid-rebase: the branch is exactly where it was.
    assert _git(repo, "rev-parse", "aorc/issue-1") == branch_sha
    assert not (repo / ".git" / "rebase-merge").exists()
    assert not (repo / ".git" / "rebase-apply").exists()


def test_revert_pr_reverts_a_merge_commit(repo):
    _git(repo, "checkout", "-b", "aorc/issue-7")
    _commit(repo, "app.py", "VERSION = 999\n", "bad change")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "aorc/issue-7", "-m",
         "Merge pull request #7 from acme/aorc/issue-7")

    LocalGitOps(repo).revert_pr(7)

    _git(repo, "checkout", "main")
    assert (repo / "app.py").read_text() == "VERSION = 1\n"


def test_revert_pr_reverts_a_squash_merge_by_pr_suffix(repo):
    _commit(repo, "app.py", "VERSION = 999\n", "bad squashed change (#9)")

    LocalGitOps(repo).revert_pr(9)

    assert (repo / "app.py").read_text() == "VERSION = 1\n"


def test_revert_pr_does_not_match_a_different_pr_number_prefix(repo):
    # A commit for PR #91 must never be picked up when reverting PR #9.
    _commit(repo, "app.py", "VERSION = 91\n", "other change (#91)")
    with pytest.raises(ValueError):
        LocalGitOps(repo).revert_pr(9)


def test_revert_pr_raises_when_no_commit_matches(repo):
    with pytest.raises(ValueError):
        LocalGitOps(repo).revert_pr(42)
