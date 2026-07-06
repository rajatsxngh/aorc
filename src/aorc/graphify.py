"""S9 -- Graphify integration: the orchestrator-owned knowledge graph.

Graphify is consumed as an existing tool over its MCP interface, not
rebuilt. One graph per repo, orchestrator-owned: built once on startup,
re-indexed after every merged PR so blast-radius stays accurate as the
codebase evolves. Agents (the design agent, S10's collision checker) only
ever query it read-only.

Query failure/timeout is a first-class signal, never a silent empty
result -- `BlastRadiusResult.ok` distinguishes "no dependents" from
"couldn't ask". This module only defines the seam and that distinction;
what a consumer *does* with `ok=False` (S10 holds on it) is that
consumer's policy, not this module's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class BlastRadiusResult:
    """The result of a blast-radius query. `ok=False` must be handled
    conservatively by the caller -- it is not equivalent to `files=set()`,
    which means "queried fine, nothing depends on these files"."""

    ok: bool
    files: set[str] = field(default_factory=set)
    error: str | None = None


class GraphifyClient(ABC):
    """Read-only knowledge-graph seam over Graphify's MCP interface: which
    files import/call which."""

    @abstractmethod
    def build(self) -> None:
        """Build the graph from the current repo state. Called once on
        orchestrator startup."""

    @abstractmethod
    def reindex(self) -> None:
        """Rebuild the graph after a merged PR changes the repo."""

    @abstractmethod
    def blast_radius(self, files: list[str]) -> BlastRadiusResult:
        """Every file whose import/call graph reaches any of `files`."""


class MockGraphifyClient(GraphifyClient):
    """In-memory `GraphifyClient` for unit tests.

    `edges` maps a file to the set of files that depend on it (its blast
    radius) -- tests configure the graph shape directly, no real parsing
    needed. Set `fail_next` to make the next `blast_radius` call return the
    explicit failure signal instead, to exercise that path without a real
    MCP server.
    """

    def __init__(self, edges: dict[str, set[str]] | None = None) -> None:
        self._edges = {k: set(v) for k, v in (edges or {}).items()}
        self.build_calls = 0
        self.reindex_calls = 0
        self.fail_next = False

    def build(self) -> None:
        self.build_calls += 1

    def reindex(self) -> None:
        self.reindex_calls += 1

    def blast_radius(self, files: list[str]) -> BlastRadiusResult:
        if self.fail_next:
            self.fail_next = False
            return BlastRadiusResult(ok=False, error="mock graphify query failure")
        hits: set[str] = set()
        for f in files:
            hits |= self._edges.get(f, set())
        return BlastRadiusResult(ok=True, files=hits)
