"""S17 -- Merge-time handling + auto-rollback.

Everything that happens *around* a merge, plus self-healing when a bad merge
lands. Human merge stays the gate in v1 (`auto: false`); nothing here merges
a PR.

- **Issue auto-close.** A merged AORC PR closes its issue (deterministic
  `aorc/issue-<n>` head -> issue mapping, no DB), moves it to Done, and
  deletes the branch (the "merged" case of the three fixed S4 cleanup rules).
  Deduped by the same `(issue, "pr-merged", head_sha)` comment marker S16's
  webhook dedup uses.
- **Stale approved PR (PRD B16).** When any PR merges, every *other* open
  AORC PR whose file set overlaps the merged one is rebased onto the new
  HEAD, re-tested, and the reviewer re-runs (via `ReviewerStage.run(pr=...)`,
  which re-enters S7's bounded fix loop on any breakage instead of growing a
  second retry mechanism). Still green -> stays approved, waiting for the
  human. A true rebase conflict -> `agent-blocked`; the PR is *never*
  auto-closed as superseded and never blindly left stale.
- **Merge conflict at PR-open (PRD B17)** lives in `ReviewerStage` (where the
  PR is opened), behind the same `GitOps` seam -- see `reviewer.py`.
- **Human PR feedback (PRD B22).** A human's PR comment is routed
  mechanically by classified intent: **code** -> the coder re-enters the fix
  loop with the comment as feedback (tests stay locked -- the coder can only
  ever commit the design doc's `files`); **spec** -> kick back to
  design/tester (relabel `in-design`, re-dispatch; the container re-runs
  forward, and re-writing spec+tests is legitimate there). The agent's own
  comments are filtered by author + AORC marker prefix so the reviewer's
  trail can never self-trigger; each human comment is handled exactly once
  via a `feedback_marker` comment (same no-DB idempotency style as S16).
- **Auto-rollback (PRD B18, safety rule 9).** When main goes red after a
  merge, the offending PR is reverted (through the `GitOps` seam -- the real
  trigger is S18's push-to-main Actions workflow) and every in-flight
  container is compared against the reverted file set using its *committed*
  checkpoint claim (the design doc's `files` on the issue branch, the same
  stateless source S10's registry rebuild uses): overlap, direct or Graphify
  blast-radius, -> tear down through the branch-preserving path + re-queue
  (Design preserved, fresh token, re-runs against corrected HEAD); no
  overlap -> continue; no committed design doc yet -> conservatively
  re-queue (can't prove non-overlap).

`GitOps` is a seam in the same style as S15's injectable minter: rebase and
revert are git-substrate operations (not a provider SDK, so no invariant is
crossed), but the real implementation is the wiring slice's job (S18's
Actions workflow / S19's integration tests). Only `MockGitOps` exists here;
no real adapter is claimed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .coder import CoderStage, failing_test_summary
from .design import DesignDoc, design_doc_path, parse_design_response
from .graphify import GraphifyClient
from .guards import BLOCKED_LABEL
from .harness import WorktreeManager, _normalize_paths, cleanup_branch
from .interfaces import Comment, GitHubClient, LLMClient, Message, PullRequest
from .pipeline import DONE_COLUMN, LABEL_COLUMN, branch_name, current_pipeline_label
from .reviewer import ReviewerStage
from .tester import TestRunner
from .wake import WakeLoop, WakeReport, claim_event, issue_for_branch

# The mock GitHub client (and the real App) post as this login; anything
# authored by it is the pipeline talking to itself, never human feedback.
AGENT_AUTHOR = "aorc[bot]"
_AORC_MARKER_PREFIX = "<!-- aorc:"

MERGE_TIME_MARKER = "<!-- aorc:merge-time -->"
SPEC_KICKBACK_MARKER = "<!-- aorc:spec-kickback -->"
ROLLBACK_MARKER = "<!-- aorc:rollback -->"

_FEEDBACK_SYSTEM_PROMPT = (
    "You route a human's pull-request review comment for an autonomous "
    "pipeline. Reply with exactly one word: 'code' if the comment asks for an "
    "implementation change (the specification and its tests are still "
    "correct), or 'spec' if it implies the specification or the tests "
    "themselves are wrong and must be redone."
)


# --------------------------------------------------------------------------- #
# GitOps seam: rebase + revert (git substrate, mirroring S15's minter seam)
# --------------------------------------------------------------------------- #


@dataclass
class RebaseResult:
    status: str  # "up-to-date" | "clean" | "conflict"


class GitOps(ABC):
    """Merge-time git operations. The discriminator is mechanical -- git
    reports a conflict or it doesn't -- so no LLM judgment ever routes it."""

    @abstractmethod
    def rebase(self, branch: str, onto: str = "main") -> RebaseResult:
        """Attempt to rebase `branch` onto `onto`. "up-to-date" -> nothing to
        do; "clean" -> rebase applied automatically; "conflict" -> the rebase
        was aborted, same lines edited on both sides."""

    @abstractmethod
    def revert_pr(self, pr_number: int) -> None:
        """Revert the given merged PR on main (auto-rollback)."""


class MockGitOps(GitOps):
    """In-memory `GitOps` for the unit suite; records every call. `conflicts`
    branches always conflict; `moved` branches rebase clean exactly once
    (main moved under them), then report up-to-date -- mirroring a real
    rebase having been applied."""

    def __init__(self, conflicts=None, moved=None) -> None:
        self.conflicts = set(conflicts or [])
        self.moved = set(moved or [])
        self.calls: list[tuple] = []

    def rebase(self, branch: str, onto: str = "main") -> RebaseResult:
        self.calls.append(("rebase", branch, onto))
        if branch in self.conflicts:
            return RebaseResult("conflict")
        if branch in self.moved:
            self.moved.discard(branch)
            return RebaseResult("clean")
        return RebaseResult("up-to-date")

    def revert_pr(self, pr_number: int) -> None:
        self.calls.append(("revert", pr_number))


# --------------------------------------------------------------------------- #
# Pure pieces
# --------------------------------------------------------------------------- #


def files_overlap(a, b) -> bool:
    """Normalized-path intersection -- the same canonical form the S10
    collision check uses, so a file spelled two ways still overlaps."""
    return bool(_normalize_paths(a) & _normalize_paths(b))


def rollback_verdict(
    reverted_files,
    checkpoint_files,
    graphify: GraphifyClient | None = None,
) -> str:
    """PRD B18: "re-queue" or "continue" for one in-flight container after a
    revert lands. `checkpoint_files=None` means no checkpoint was reached
    yet -- conservatively re-queue (non-overlap can't be proven). Overlap is
    direct path-intersect or, when Graphify is available, either side's
    blast radius reaching the other; a failed query holds, never proceeds
    (same conservatism as `Checkpoint._collides`)."""
    if checkpoint_files is None:
        return "re-queue"
    mine = _normalize_paths(checkpoint_files)
    reverted = _normalize_paths(reverted_files)
    if mine & reverted:
        return "re-queue"
    if graphify is None:
        return "continue"
    forward = graphify.blast_radius(sorted(reverted))
    if not forward.ok or _normalize_paths(forward.files) & mine:
        return "re-queue"
    backward = graphify.blast_radius(sorted(mine))
    if not backward.ok or _normalize_paths(backward.files) & reverted:
        return "re-queue"
    return "continue"


def feedback_marker(comment_id: int) -> str:
    return f"<!-- aorc:feedback id={comment_id} -->"


def is_agent_comment(comment: Comment, bot_author: str = AGENT_AUTHOR) -> bool:
    """The self-trigger filter: the pipeline's own comments (reviewer trail,
    idempotency markers) are identified by author, plus the AORC marker
    prefix as a belt-and-braces check -- routing must never loop on them."""
    return comment.author == bot_author or comment.body.lstrip().startswith(
        _AORC_MARKER_PREFIX
    )


def classify_feedback_intent(body: str, llm: LLMClient) -> str | None:
    """One LLM call, mechanically parsed: exactly "code" or "spec", anything
    else is `None` (unrouted -- the delivery is left unclaimed so a retry can
    classify it again). The *routing* on the result is pure."""
    completion = llm.complete(
        [Message("system", _FEEDBACK_SYSTEM_PROMPT), Message("user", body)]
    )
    text = completion.text.strip().lower()
    if text.startswith("code"):
        return "code"
    if text.startswith("spec"):
        return "spec"
    return None


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


@dataclass
class MergeReport:
    duplicate: bool = False
    closed_issue: int | None = None
    stale: dict[int, str] = field(default_factory=dict)  # PR -> "approved" | "agent-blocked"
    wake: WakeReport | None = None


@dataclass
class FeedbackReport:
    action: str  # "code" | "spec" | "ignored" | "duplicate" | "unrouted"
    issue: int | None = None
    fixed: bool | None = None  # code route only


@dataclass
class RollbackReport:
    reverted_pr: int
    requeued: list[int] = field(default_factory=list)
    continued: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# The merge-time handler: S17's three webhook-facing entry points
# --------------------------------------------------------------------------- #


class MergeTimeHandler:
    """Wired around a `WakeLoop` (same composition-root discipline as S16:
    all GitHub writes go through `loop.github`, already scrubbed; all
    re-dispatches go through `loop.dispatch_issue`, so the broker stays the
    only env source). The real webhook receiver / push-to-main Actions
    workflow that invokes these is S18's install surface."""

    def __init__(
        self,
        loop: WakeLoop,
        gitops: GitOps,
        *,
        coder: CoderStage | None = None,
        reviewer: ReviewerStage | None = None,
        test_runner: TestRunner | None = None,
        test_command: str | None = None,
        feedback_llm: LLMClient | None = None,
        graphify: GraphifyClient | None = None,
        bot_author: str = AGENT_AUTHOR,
        cwd: str = ".",
        worktrees: WorktreeManager | None = None,
    ) -> None:
        self._loop = loop
        self._gitops = gitops
        self._coder = coder
        self._reviewer = reviewer
        self._test_runner = test_runner
        self._test_command = test_command
        self._feedback_llm = feedback_llm
        self._graphify = graphify
        self._bot_author = bot_author
        self._cwd = cwd
        # S27: when a real `WorktreeManager` is given (the live composition
        # path), the stale-PR recheck runs its toolchain against the actual
        # per-issue worktree `ContainerTestRunner` can resolve a container
        # from -- not the single fixed `cwd` every issue used to share
        # (silently wrong for anything but a same-directory host runner).
        self._worktrees = worktrees

    @property
    def _github(self) -> GitHubClient:
        return self._loop.github  # the loop's client is already scrub-wrapped

    # ---- entry points ----------------------------------------------------- #

    def on_pr_merged(self, pr_number: int, head_sha: str) -> MergeReport:
        """The full merge event: close the merged AORC issue (-> Done, branch
        deleted), re-check every overlapping open AORC PR for staleness,
        reindex Graphify (the repo just changed), then run the S16 wake
        (held-queue sweep + token-expiry pass)."""
        merged = self._github.get_pull_request(pr_number)
        report = MergeReport()
        issue_number = issue_for_branch(merged.head)
        if issue_number is not None:
            if not claim_event(self._github, issue_number, "pr-merged", head_sha):
                return MergeReport(duplicate=True)
            self._close_merged_issue(issue_number)
            report.closed_issue = issue_number
        if self._graphify is not None:
            self._graphify.reindex()
        report.stale = self._recheck_stale_prs(merged)
        report.wake = self._loop.wake()
        return report

    def on_pr_comment(self, pr_number: int, comment_id: int) -> FeedbackReport:
        """Route one PR comment. Agent-authored -> ignored (no self-trigger);
        already claimed -> duplicate; unclassifiable -> unrouted and left
        unclaimed so a later delivery retries. Only a routed comment is
        marked handled."""
        if self._feedback_llm is None:
            raise ValueError("on_pr_comment requires a feedback LLMClient")
        comments = self._github.list_comments(pr_number)
        comment = next((c for c in comments if c.id == comment_id), None)
        if comment is None or is_agent_comment(comment, self._bot_author):
            return FeedbackReport(action="ignored")
        marker = feedback_marker(comment_id)
        if any(marker in c.body for c in comments):
            return FeedbackReport(action="duplicate")
        pr = self._github.get_pull_request(pr_number)
        issue_number = issue_for_branch(pr.head)
        if issue_number is None:
            return FeedbackReport(action="ignored")  # not an AORC PR
        intent = classify_feedback_intent(comment.body, self._feedback_llm)
        if intent is None:
            return FeedbackReport(action="unrouted")
        self._github.post_comment(pr_number, marker)
        if intent == "code":
            return self._feedback_code(issue_number, comment)
        return self._feedback_spec(issue_number)

    def on_main_broken(self, pr_number: int) -> RollbackReport:
        """Auto-rollback: main went red after `pr_number` merged. Revert it,
        then triage every in-flight container by `rollback_verdict` against
        its committed checkpoint claim. Re-queue = teardown through the
        branch-preserving "held" path (Design intact) + immediate re-dispatch
        with a fresh token against the corrected HEAD."""
        pr = self._github.get_pull_request(pr_number)
        self._gitops.revert_pr(pr_number)
        if self._graphify is not None:
            self._graphify.reindex()
        report = RollbackReport(reverted_pr=pr_number)
        offending_issue = issue_for_branch(pr.head)
        if offending_issue is not None:
            self._github.post_comment(
                offending_issue,
                f"{ROLLBACK_MARKER}\nMain's test suite went red after PR "
                f"#{pr_number} merged; the PR was auto-reverted.",
            )
        for issue_number, (handle, _token) in sorted(self._loop.in_flight.items()):
            claimed = self._checkpoint_claim(issue_number)
            if rollback_verdict(pr.files, claimed, self._graphify) == "re-queue":
                self._loop.harness.teardown(handle, outcome="held")
                del self._loop.in_flight[issue_number]
                self._loop.dispatch_issue(issue_number)
                report.requeued.append(issue_number)
            else:
                report.continued.append(issue_number)
        return report

    # ---- the pieces -------------------------------------------------------- #

    def _close_merged_issue(self, issue_number: int) -> None:
        self._github.close_issue(issue_number)
        self._github.set_board_column(issue_number, DONE_COLUMN)
        cleanup_branch(self._github, issue_number, "merged")

    def _recheck_stale_prs(self, merged: PullRequest) -> dict[int, str]:
        outcomes: dict[int, str] = {}
        for pr in self._github.list_pull_requests(state="open"):
            issue_number = issue_for_branch(pr.head)
            if issue_number is None or pr.number == merged.number:
                continue
            if not files_overlap(pr.files, merged.files):
                continue
            outcomes[pr.number] = self._recheck(pr, issue_number)
        return outcomes

    def _recheck(self, pr: PullRequest, issue_number: int) -> str:
        """One stale PR: rebase -> re-test -> (fix loop if broke) -> reviewer
        re-run. Every failure exit blocks the *issue*; the PR always stays
        open (never auto-closed as superseded)."""
        if self._reviewer is None or self._test_runner is None or self._test_command is None:
            raise ValueError(
                "stale-PR recheck requires reviewer, test_runner and test_command"
            )
        rebase = self._gitops.rebase(pr.head, pr.base)
        if rebase.status == "conflict":
            self._block(issue_number, "merge conflict with main while rebasing the open PR")
            return "agent-blocked"
        design = self._design_for(issue_number)
        if design is None:
            self._block(issue_number, "open PR has no parseable design doc to re-review against")
            return "agent-blocked"
        cwd = self._cwd_for(issue_number)
        result = self._test_runner.run(cwd, self._test_command)
        if result.returncode != 0:
            if self._coder is None:
                raise ValueError("stale-PR recheck requires a coder for the fix loop")
            fix = self._coder.run(
                issue_number,
                design,
                repo_files=self._current_files(issue_number, design),
                cwd=cwd,
                review_feedback=failing_test_summary(result),
            )
            if fix.status != "proceed":
                self._block(issue_number, "fix loop exhausted re-testing against the new HEAD")
                return "agent-blocked"
        review = self._reviewer.run(
            issue_number,
            design,
            self._github.get_issue(issue_number).body,
            cwd=cwd,
            pr=pr,
        )
        if review.status != "proceed":
            self._block(issue_number, "reviewer re-run failed against the new HEAD")
            return "agent-blocked"
        return "approved"

    def _feedback_code(self, issue_number: int, comment: Comment) -> FeedbackReport:
        """Code intent: the coder re-enters S7's bounded fix loop with the
        human's comment as feedback. Tests stay locked structurally -- the
        coder can only commit paths in the design doc's `files`."""
        if self._coder is None:
            raise ValueError("code-intent feedback requires a coder")
        design = self._design_for(issue_number)
        if design is None:
            self._block(issue_number, "no parseable design doc to run the fix loop against")
            return FeedbackReport(action="code", issue=issue_number, fixed=False)
        result = self._coder.run(
            issue_number,
            design,
            repo_files=self._current_files(issue_number, design),
            cwd=self._cwd_for(issue_number),
            review_feedback=comment.body,
        )
        if result.status != "proceed":
            self._block(issue_number, "fix loop exhausted on human PR feedback")
            return FeedbackReport(action="code", issue=issue_number, fixed=False)
        return FeedbackReport(action="code", issue=issue_number, fixed=True)

    def _feedback_spec(self, issue_number: int) -> FeedbackReport:
        """Spec intent: kick back to design/tester -- relabel `in-design` and
        re-dispatch; the container re-runs forward from Design (overwriting
        the design doc and re-issuing tests is legitimate on this path)."""
        current = current_pipeline_label(self._github.get_issue(issue_number).labels)
        if current is not None:
            self._github.remove_label(issue_number, current)
        self._github.add_label(issue_number, "in-design")
        self._github.set_board_column(issue_number, LABEL_COLUMN["in-design"])
        self._github.post_comment(
            issue_number,
            f"{SPEC_KICKBACK_MARKER}\nHuman PR feedback implies the spec/tests "
            "are wrong -- kicked back to design/tester; the pipeline re-runs "
            "forward from Design.",
        )
        if issue_number not in self._loop.in_flight:
            self._loop.dispatch_issue(issue_number)
        return FeedbackReport(action="spec", issue=issue_number)

    def _block(self, issue_number: int, reason: str) -> None:
        self._github.add_label(issue_number, BLOCKED_LABEL)
        self._github.set_board_column(issue_number, LABEL_COLUMN[BLOCKED_LABEL])
        self._github.post_comment(
            issue_number,
            f"{MERGE_TIME_MARKER}\nStopping: {reason}. Labeling `{BLOCKED_LABEL}`.",
        )

    def _cwd_for(self, issue_number: int) -> str:
        """The per-issue worktree when a real `WorktreeManager` is wired in
        (S27's live composition path); the fixed `cwd` default otherwise --
        every existing unit test's `MockTestRunner` ignores `cwd` entirely,
        so this is a no-op change for the unit suite."""
        return self._worktrees.ensure(issue_number) if self._worktrees is not None else self._cwd

    def _design_for(self, issue_number: int) -> DesignDoc | None:
        """The committed checkpoint claim, re-read from GitHub -- the same
        stateless source `design.rebuild_in_flight_registry` uses."""
        raw = self._github.get_file(design_doc_path(issue_number), branch_name(issue_number))
        return parse_design_response(raw) if raw is not None else None

    def _checkpoint_claim(self, issue_number: int) -> list[str] | None:
        design = self._design_for(issue_number)
        return list(design.files) if design is not None else None

    def _current_files(self, issue_number: int, design: DesignDoc) -> dict[str, str]:
        branch = branch_name(issue_number)
        return {path: self._github.get_file(path, branch) or "" for path in design.files}
