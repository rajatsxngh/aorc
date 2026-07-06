"""S13 -- Cost + compute guards: the hard, unattended-safety limits.

Three cost circuit-breaker levels (per-issue, per-run, daily) plus a
container wall-clock limit. Whichever trips first stops the agent, per the
issue. All thresholds are config values (`.aorc.yml`), never hardcoded.

Same seam-boundary style as S10's injected in-flight registry and S11's
injected `elapsed_days`: neither `LLMClient`/`GitHubClient` nor
`ContainerRuntime` has any notion of "money spent so far" or "minutes
elapsed", so a real running total/elapsed time is the caller's job to
measure and pass in here. In particular the *daily* total spans wake
cycles, which needs a persistence store this repo doesn't have yet (the
same genuine-infrastructure-gap caveat prior slices documented for their
own live-wiring gaps) -- `CostGuard` trusts whatever running `CostTotals`
it's handed.

Clean-stop vs. hard-stop is a caller contract, not something this module
enforces by itself: a plain (non-overshoot) `block-issue` verdict means
"finish the current stage's commit, then stop at the next artifact
boundary"; `overshoot=True` means stop now, mid-stage -- a stuck fix loop
blowing 1.5x the per-issue cap doesn't get to finish its stage first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .interfaces import GitHubClient
from .pipeline import LABEL_COLUMN

BLOCKED_LABEL = "agent-blocked"

DEFAULT_PER_ISSUE_CAP = 5.0
DEFAULT_PER_RUN_CAP = 50.0
DEFAULT_DAILY_CAP = 100.0
DEFAULT_OVERSHOOT_MULTIPLIER = 1.5
DEFAULT_WALL_CLOCK_MINUTES = 45.0

COST_PING_MARKER = "<!-- aorc:cost-cap-ping -->"
TIMEOUT_PING_MARKER = "<!-- aorc:timeout-ping -->"


@dataclass
class CostTotals:
    """Running spend the caller has accumulated so far. `per_issue` and
    `run` are naturally scoped to one issue / one wake cycle; `daily` is the
    caller's running total across the day."""

    per_issue: float = 0.0
    run: float = 0.0
    daily: float = 0.0


@dataclass
class CostVerdict:
    action: str  # "continue" | "block-issue" | "pause-run" | "halt-daily"
    overshoot: bool = False
    totals: CostTotals = field(default_factory=CostTotals)


class CostGuard:
    """Three-level circuit breaker. A `CostGuard` instance is scoped to one
    wake cycle (mirroring the stateless-orchestrator model S16 documents):
    once a run-level or daily-level cap trips, every subsequent `record`
    call -- for any issue -- comes back paused/halted, modeling "pause/halt
    the whole system" without needing cross-issue state beyond this one
    instance's lifetime.

    Checked most-severe first (daily, then run, then per-issue) so a spend
    that crosses two thresholds at once reports the wider-blast-radius
    action.
    """

    def __init__(
        self,
        github: GitHubClient,
        *,
        per_issue_cap: float = DEFAULT_PER_ISSUE_CAP,
        per_run_cap: float = DEFAULT_PER_RUN_CAP,
        daily_cap: float = DEFAULT_DAILY_CAP,
        overshoot_multiplier: float = DEFAULT_OVERSHOOT_MULTIPLIER,
    ) -> None:
        self._github = github
        self._per_issue_cap = per_issue_cap
        self._per_run_cap = per_run_cap
        self._daily_cap = daily_cap
        self._overshoot_multiplier = overshoot_multiplier
        self._halted = False
        self._paused = False

    def record(self, issue_number: int, amount: float, totals: CostTotals) -> CostVerdict:
        totals.per_issue += amount
        totals.run += amount
        totals.daily += amount

        if self._halted or totals.daily >= self._daily_cap:
            self._halted = True
            self._block(issue_number, "daily cost cap")
            return CostVerdict(action="halt-daily", totals=totals)

        if self._paused or totals.run >= self._per_run_cap:
            self._paused = True
            self._block(issue_number, "per-run cost cap")
            return CostVerdict(action="pause-run", totals=totals)

        if totals.per_issue >= self._per_issue_cap * self._overshoot_multiplier:
            self._block(issue_number, "cost cap (single-stage overshoot)")
            return CostVerdict(action="block-issue", overshoot=True, totals=totals)

        if totals.per_issue >= self._per_issue_cap:
            self._block(issue_number, "cost cap")
            return CostVerdict(action="block-issue", totals=totals)

        return CostVerdict(action="continue", totals=totals)

    def _block(self, issue_number: int, reason: str) -> None:
        self._github.add_label(issue_number, BLOCKED_LABEL)
        self._github.set_board_column(issue_number, LABEL_COLUMN[BLOCKED_LABEL])
        self._github.post_comment(
            issue_number,
            f"{COST_PING_MARKER}\nStopping: {reason} reached. Labeling "
            f"`{BLOCKED_LABEL}` (reason: {reason}).",
        )


@dataclass
class ComputeVerdict:
    action: str  # "continue" | "kill"


class ComputeGuard:
    """Container wall-clock limit. `elapsed_minutes` is caller-supplied, the
    same way S11's `check_timeout` takes `elapsed_days` -- `ContainerRuntime`
    has no notion of how long a container has been running. Pure decision
    only; `harness.ContainerHarness.enforce_wall_clock` owns the actual
    kill + labeling + branch-preserving teardown."""

    def __init__(self, *, wall_clock_minutes: float = DEFAULT_WALL_CLOCK_MINUTES) -> None:
        self._wall_clock_minutes = wall_clock_minutes

    def check(self, elapsed_minutes: float) -> ComputeVerdict:
        if elapsed_minutes >= self._wall_clock_minutes:
            return ComputeVerdict(action="kill")
        return ComputeVerdict(action="continue")
