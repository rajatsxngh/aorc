"""S18 -- `LocalGitOps`: the real implementation behind S17's `GitOps` seam.

Runs plain `git` (subprocess) against a local checkout -- the clone the
push-to-main Actions workflow (or a self-hosted orchestrator) already has.
Same discriminator contract as `MockGitOps`: git mechanically reports a
conflict or it doesn't; no LLM judgment ever routes the result.

The PR -> commit mapping is deliberately DB-free, like everything else in the
orchestrator: GitHub's own merge machinery stamps the PR number into the
commit subject ("Merge pull request #N ..." for merge commits, a "(#N)"
suffix for squash merges), so the offending commit is recoverable from `git
log` alone.
"""

from __future__ import annotations

import re
import subprocess

from .merge import GitOps, RebaseResult


class LocalGitOps(GitOps):
    def __init__(self, repo_path: str) -> None:
        self._repo = str(repo_path)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", self._repo, *args],
            capture_output=True,
            text=True,
            check=check,
        )

    # ---- the seam ---------------------------------------------------------- #

    def rebase(self, branch: str, onto: str = "main") -> RebaseResult:
        already = self._git("merge-base", "--is-ancestor", onto, branch, check=False)
        if already.returncode == 0:
            return RebaseResult("up-to-date")
        # `git rebase <onto> <branch>` checks out `branch` and replays it.
        proc = self._git("rebase", onto, branch, check=False)
        if proc.returncode == 0:
            return RebaseResult("clean")
        # Same lines edited on both sides: abort so the branch is left exactly
        # where it was (never mid-rebase), and report the conflict upward --
        # the caller routes to agent-blocked (S17), never auto-resolves.
        self._git("rebase", "--abort", check=False)
        return RebaseResult("conflict")

    def revert_pr(self, pr_number: int) -> None:
        sha, parents = self._find_pr_commit(pr_number)
        self._git("checkout", "main")
        if len(parents) > 1:  # a true merge commit: revert against mainline 1
            self._git("revert", "--no-edit", "-m", "1", sha)
        else:  # squash/rebase merge: a plain single-parent commit
            self._git("revert", "--no-edit", sha)

    # ---- the pieces -------------------------------------------------------- #

    def _find_pr_commit(self, pr_number: int) -> tuple[str, list[str]]:
        """The commit on main that landed PR `pr_number`, plus its parents.
        Word-bounded match so #9 never matches #91."""
        pattern = re.compile(
            rf"(?:\(#{pr_number}\)|Merge pull request #{pr_number}(?:\D|$))"
        )
        log = self._git("log", "main", "--format=%H%x00%P%x00%s").stdout
        for line in log.splitlines():
            sha, parents, subject = line.split("\x00", 2)
            if pattern.search(subject):
                return sha, parents.split()
        raise ValueError(
            f"no commit on main records PR #{pr_number} "
            "(no 'Merge pull request #N' or '(#N)' subject)"
        )
