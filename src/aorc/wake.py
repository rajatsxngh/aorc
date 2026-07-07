"""S16 -- Liveness & idempotency: the stateless orchestrator wake loop.

The orchestrator holds no memory between invocations -- on every wake it
re-reads all state from GitHub (`rebuild_state`) and acts on that fresh
picture. GitHub is the single source of truth; the only process-local state
`WakeLoop` keeps is the in-flight container handles + their issue tokens,
which are live runtime objects that *cannot* be re-read from GitHub (and die
with the process -- a restarted orchestrator's containers are re-dispatched
by the sweep, resuming from their last committed artifact).

Three liveness mechanisms, all funneling through `wake()`:

- **Held-issue wake.** `on_pr_merged` (primary: a held issue is usually
  waiting on a conflicting issue to merge, and that *is* a merge event) and
  `cron_tick` (backstop: webhooks are at-least-once *and* droppable) both
  sweep the `aorc-held` queue and re-dispatch anything newly unblocked. The
  cron path no-ops on an empty queue -- pure reads, nearly free.
- **Webhook double-fire dedup.** `claim_event` records the idempotency tuple
  `(issue_number, stage, head_sha)` as a comment marker (no DB); a duplicate
  delivery is a no-op. `should_run_stage` layers the artifact-presence check
  on top -- a container's first question at any stage is "does this stage's
  artifact already exist on the branch?" -- catching the race the key can't.
  Accepted v1 limitation: no atomic compare-and-swap without a DB, so two
  containers can still spin up before either commits; the artifact check
  makes that *wasteful* (one redundant pass), never *wrong* (no double work
  survives to merge).
- **Backfill = re-sync.** `backfill` lists every open issue, feeds each
  not-yet-in-flow one into triage as if newly opened, and dispatches
  actionable ones through the selector (5 at a time by default). Re-runnable
  anytime AORC falls behind; issues already in the pipeline are skipped, so
  re-running only picks up what's missing.

This module is also the composition root the S15 credential model was built
for (`WakeLoop.compose`): the real `GitHubClient` is wrapped in
`ScrubbingGitHubClient` exactly once and that wrapped instance is the only
reference the loop *and* its harness ever hold; every dispatch's env comes
from `CredentialBroker.container_env` (and `ContainerHarness.dispatch`
independently leak-checks whatever reaches it); and every wake runs
`handle_token_expiry` over every in-flight container -- "re-queue" feeds the
same re-dispatch path with a freshly minted token, never a refresh.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .credentials import (
    CredentialBroker,
    IssueToken,
    ScrubbingGitHubClient,
    handle_token_expiry,
)
from .dispatch import DEFAULT_CONCURRENCY, select_dispatch
from .harness import ContainerHandle, ContainerHarness, ContainerRuntime
from .interfaces import GitHubClient, Issue, LLMClient
from .pipeline import (
    HELD_LABEL,
    LABEL_COLUMN,
    ArtifactChecker,
    current_pipeline_label,
)
from .triage import triage

if TYPE_CHECKING:  # annotation only -- driver.py imports several stage
    # modules that don't need to be a hard runtime dependency of every
    # WakeLoop user (e.g. the zero-dep unit suite building a bare loop).
    from .driver import PipelineDriver

_BRANCH_RE = re.compile(r"^aorc/issue-(\d+)$")


def issue_for_branch(branch: str) -> int | None:
    """Inverse of `pipeline.branch_name`: the deterministic branch scheme is
    what lets a stateless orchestrator map a PR back to its issue with no DB
    (PRD B24). Non-AORC branches map to None."""
    match = _BRANCH_RE.match(branch)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- #
# Webhook double-fire dedup (PRD B6)
# --------------------------------------------------------------------------- #


def event_marker(issue_number: int, stage: str, head_sha: str) -> str:
    return f"<!-- aorc:event issue={issue_number} stage={stage} sha={head_sha} -->"


def seen_event(github: GitHubClient, issue_number: int, stage: str, head_sha: str) -> bool:
    marker = event_marker(issue_number, stage, head_sha)
    return any(marker in c.body for c in github.list_comments(issue_number))


def claim_event(github: GitHubClient, issue_number: int, stage: str, head_sha: str) -> bool:
    """The idempotency key: True exactly once per (issue, stage, head_sha)
    tuple; a duplicate delivery gets False and the caller no-ops. Check and
    record are two calls (no atomic compare-and-swap without a DB -- the
    accepted v1 window); the artifact check below catches what slips through."""
    if seen_event(github, issue_number, stage, head_sha):
        return False
    github.post_comment(issue_number, event_marker(issue_number, stage, head_sha))
    return True


def stage_artifact_present(github: GitHubClient, issue_number: int, stage: str) -> bool:
    """Has this stage already committed its artifact to the issue branch?
    A container's first action at any stage -- and the wake loop's guard
    before re-running one."""
    return ArtifactChecker(github).artifact_exists(issue_number, stage)


def should_run_stage(
    github: GitHubClient, issue_number: int, stage: str, head_sha: str
) -> bool:
    """Both dedup layers in order: the event key (same webhook twice), then
    artifact presence (two containers raced past the key -- the survivor's
    committed artifact makes the loser a no-op: wasteful, never wrong)."""
    if not claim_event(github, issue_number, stage, head_sha):
        return False
    if stage_artifact_present(github, issue_number, stage):
        return False
    return True


# --------------------------------------------------------------------------- #
# Stateless rebuild: the working picture, re-read fresh on every wake
# --------------------------------------------------------------------------- #


@dataclass
class WakeState:
    """One wake's view of GitHub, rebuilt from scratch every time -- never
    cached across wakes."""

    held: list[Issue] = field(default_factory=list)
    in_pipeline: list[Issue] = field(default_factory=list)
    backlog: list[Issue] = field(default_factory=list)


def rebuild_state(github: GitHubClient) -> WakeState:
    state = WakeState()
    for issue in github.list_issues("open"):
        if HELD_LABEL in issue.labels:
            state.held.append(issue)
        elif current_pipeline_label(issue.labels):
            state.in_pipeline.append(issue)
        else:
            state.backlog.append(issue)
    return state


# --------------------------------------------------------------------------- #
# The wake loop
# --------------------------------------------------------------------------- #


@dataclass
class WakeReport:
    requeued: list[int] = field(default_factory=list)  # expired-token re-dispatches
    released: list[int] = field(default_factory=list)  # held -> dispatched
    duplicate: bool = False  # webhook delivery was a dedup no-op


@dataclass
class BackfillReport:
    triaged: list[int] = field(default_factory=list)
    dispatched: list[int] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)  # actionable, over capacity
    held: list[int] = field(default_factory=list)  # declared-blocked


class WakeLoop:
    """The stateless orchestrator's entry points: merge webhook, cron tick,
    backfill/re-sync. Build via `compose` in production so the scrubbing
    wrap happens exactly once at the root."""

    def __init__(
        self,
        github: GitHubClient,
        harness: ContainerHarness,
        broker: CredentialBroker,
        repo: str,
        *,
        llm: LLMClient | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Defensive wrap for hand-assembled loops; `compose` already passes a
        # wrapped client (and never double-wraps -- scrub is idempotent, but
        # one layer is the contract).
        self.github = (
            github
            if isinstance(github, ScrubbingGitHubClient)
            else ScrubbingGitHubClient(github)
        )
        self.harness = harness
        self._broker = broker
        self._repo = repo
        self._llm = llm
        self._concurrency = concurrency
        self._clock = clock
        # issue -> (handle, token): live runtime objects, not pipeline state.
        self.in_flight: dict[int, tuple[ContainerHandle, IssueToken]] = {}
        # S22: optional build-pipeline driver, set post-construction by the
        # composition root once `.aorc.yml`'s setup/test commands are known.
        # `None` here (the default for every hand-assembled/test loop)
        # reproduces the pre-S22 behavior exactly -- dispatch only mints a
        # token and starts a container.
        self.driver: "PipelineDriver | None" = None

    @classmethod
    def compose(
        cls,
        github: GitHubClient,
        runtime: ContainerRuntime,
        worktrees,
        broker: CredentialBroker,
        repo: str,
        *,
        llm: LLMClient | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        clock: Callable[[], float] = time.time,
    ) -> "WakeLoop":
        """The composition root (S15 wiring): wrap the real client in
        `ScrubbingGitHubClient` once, here, and hand that same instance to
        both the loop and the harness -- no code path downstream ever holds
        an unwrapped reference."""
        wrapped = ScrubbingGitHubClient(github)
        harness = ContainerHarness(runtime, worktrees, wrapped)
        return cls(
            wrapped, harness, broker, repo, llm=llm, concurrency=concurrency, clock=clock
        )

    # ---- entry points ----------------------------------------------------- #

    def on_pr_merged(self, pr_number: int, head_sha: str) -> WakeReport:
        """Primary held-issue wake. Deduped by (issue, "pr-merged", head_sha)
        when the PR maps to an AORC issue; a human PR's merge has no issue to
        pin a marker on, but the sweep itself is naturally idempotent (a
        released issue is no longer held, so a replay finds nothing to do).

        This is the *bare* sweep entry point. The full merge webhook handler
        is `merge.MergeTimeHandler.on_pr_merged` (S17): issue auto-close,
        stale-PR re-check, then this sweep -- wire webhooks to that, not
        here (it claims the same dedup key, so wiring both double-fires
        nothing but skips the S17 behavior)."""
        pr = self.github.get_pull_request(pr_number)
        issue_number = issue_for_branch(pr.head)
        if issue_number is not None and not claim_event(
            self.github, issue_number, "pr-merged", head_sha
        ):
            return WakeReport(duplicate=True)
        return self.wake()

    def cron_tick(self) -> WakeReport:
        """Backstop held-issue wake (every 10-15 min): same sweep as the
        webhook path. On an empty queue this is pure reads -- nearly free."""
        return self.wake()

    def wake(self, *, now: float | None = None) -> WakeReport:
        """One wake, whatever triggered it: check every in-flight token for
        expiry first (S15 wiring), then rebuild the picture from GitHub and
        sweep the held queue."""
        now = self._clock() if now is None else now
        report = WakeReport()
        report.requeued = self._expiry_pass(now)
        state = rebuild_state(self.github)
        report.released = self._sweep_held(state.held)
        return report

    def backfill(self) -> BackfillReport:
        """First-run backfill = re-sync (PRD B7): every open issue not
        already in the flow goes through triage as if newly opened;
        actionable ones enter the dispatch queue, `concurrency` (default 5)
        at a time; declared-blocked ones are held for the sweep to release."""
        if self._llm is None:
            raise ValueError("backfill requires an LLMClient to run triage")
        report = BackfillReport()
        candidates: list[Issue] = []
        for issue in self.github.list_issues("open"):
            if issue.number in self.in_flight:
                continue
            if self._already_in_flow(issue):
                continue  # already in the flow -- re-sync only adds what's missing
            report.triaged.append(issue.number)
            result = triage(issue, self._llm)
            if result.status == "actionable":
                candidates.append(issue)
            else:
                self._route_not_ready(issue, result)
        decision = select_dispatch(
            candidates,
            self.github,
            in_flight=len(self.in_flight),
            concurrency=self._concurrency,
        )
        for number in decision.to_dispatch:
            self.dispatch_issue(number)
        for number in decision.blocked:
            self.hold(number)
        report.dispatched = decision.to_dispatch
        report.queued = decision.queued  # unlabeled: the next wake re-evaluates
        report.held = decision.blocked
        return report

    # ---- the pieces -------------------------------------------------------- #

    def _route_not_ready(self, issue: Issue, result) -> None:
        """Hook: what to do with a not-ready triage verdict during backfill.
        The bare S16 loop leaves it in the backlog; S18's install-time loop
        routes it onward (vague -> clarification, epic -> decomposition)."""

    def _already_in_flow(self, issue: Issue) -> bool:
        """An issue the backfill re-sync must not re-triage: held or carrying
        a pipeline label. Subclasses widen this (S18's config-gated loop adds
        the awaiting-config queue)."""
        return HELD_LABEL in issue.labels or bool(current_pipeline_label(issue.labels))

    def dispatch_issue(self, issue_number: int) -> ContainerHandle:
        """The only dispatch path: mint -> broker-built env -> harness. No
        hand-built env dict exists anywhere in the loop.

        S22: if a `driver` is configured, it runs the actual
        design/test/code/review sequence right here, orchestrator-side --
        the container the harness just started runs no command of its own
        (known limitation, `issues/README.md`). `driver` is `None` for every
        loop that doesn't set it, which reproduces the exact pre-S22
        behavior (mint + start, nothing else)."""
        token = self._broker.mint(issue_number, self._repo)
        env = self._broker.container_env(token)
        handle = self.harness.dispatch(issue_number, env)
        self.in_flight[issue_number] = (handle, token)
        if self.driver is not None:
            self.driver.run(issue_number)
        return handle

    def hold(self, issue_number: int) -> None:
        self.github.add_label(issue_number, HELD_LABEL)
        self.github.set_board_column(issue_number, LABEL_COLUMN[HELD_LABEL])

    def _expiry_pass(self, now: float) -> list[int]:
        """`handle_token_expiry` for every in-flight container, every wake.
        "re-queue" = torn down through the branch-preserving path; re-dispatch
        immediately with a freshly minted token -- the container resumes from
        its last committed artifact. The expired token is dropped, never
        refreshed (no such path exists)."""
        requeued = []
        for issue_number, (handle, token) in sorted(self.in_flight.items()):
            if handle_token_expiry(self.harness, handle, token, now=now) == "re-queue":
                del self.in_flight[issue_number]
                self.dispatch_issue(issue_number)
                requeued.append(issue_number)
        return requeued

    def _sweep_held(self, held: list[Issue]) -> list[int]:
        """Re-evaluate the held queue and re-dispatch what's now unblocked,
        capacity permitting. Still-blocked issues keep their label for the
        next wake; over-capacity ones do too (the queue lives in GitHub)."""
        if not held:
            return []
        candidates = [i for i in held if i.number not in self.in_flight]
        decision = select_dispatch(
            candidates,
            self.github,
            in_flight=len(self.in_flight),
            concurrency=self._concurrency,
        )
        for number in decision.to_dispatch:
            self.github.remove_label(number, HELD_LABEL)
            self.dispatch_issue(number)
        return decision.to_dispatch
