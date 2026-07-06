"""S10 -- Dispatch selector: the cheap, up-front gate before a container is
ever started.

Deliberately dumb by design: no collision prediction from issue text here --
that needs real reported file lists and belongs to `Checkpoint`
(`harness.py`), which runs *after* Design. This module only enforces the two
signals that are reliable before any work has started:

  - declared dependencies (`blocked by #N` in the issue body, or an
    un-decomposed epic -- an epic must go through S12 decomposition into
    real sub-issues before it ever reaches the build pipeline)
  - the global concurrency ceiling (default 5, configurable via
    `.aorc.yml`'s `dispatch.concurrency`)

Candidates are assumed already-triaged actionable issues, in priority order
(lowest issue number / oldest first is the caller's choice -- this module
just partitions whatever order it's given).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .interfaces import GitHubClient, Issue
from .triage import looks_like_epic

_BLOCKED_BY_RE = re.compile(r"blocked by #(\d+)", re.IGNORECASE)

DEFAULT_CONCURRENCY = 5


def declared_blockers(issue: Issue) -> list[int]:
    """Issue numbers referenced by a `blocked by #N` marker in the body."""
    return [int(n) for n in _BLOCKED_BY_RE.findall(issue.body)]


def is_declared_blocked(issue: Issue, github: GitHubClient) -> bool:
    """The one reliable up-front signal: an explicit `blocked by #N` that
    hasn't closed yet, or an epic that hasn't been decomposed into
    sub-issues yet (an epic is never dispatched pre-decomposition)."""
    if looks_like_epic(issue):
        return True
    for number in declared_blockers(issue):
        try:
            blocker = github.get_issue(number)
        except KeyError:
            continue  # dangling reference -- nothing to actually wait on
        if blocker.state != "closed":
            return True
    return False


@dataclass
class DispatchDecision:
    to_dispatch: list[int] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)
    blocked: list[int] = field(default_factory=list)


def select_dispatch(
    candidates: list[Issue],
    github: GitHubClient,
    *,
    in_flight: int = 0,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> DispatchDecision:
    """Partition candidates into dispatch-now / queue / blocked, honoring
    declared blockers first and then the concurrency ceiling in the given
    order. Blocked issues never occupy a queue slot."""
    decision = DispatchDecision()
    capacity = max(concurrency - in_flight, 0)
    for issue in candidates:
        if is_declared_blocked(issue, github):
            decision.blocked.append(issue.number)
            continue
        if len(decision.to_dispatch) < capacity:
            decision.to_dispatch.append(issue.number)
        else:
            decision.queued.append(issue.number)
    return decision
