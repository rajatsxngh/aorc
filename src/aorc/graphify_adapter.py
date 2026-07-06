"""Real `GraphifyClient` over Graphify's MCP server.

The MCP SDK is imported lazily so the orchestrator core and its tests run
with zero third-party deps; only a real call touches `mcp`. Query
failure/timeout is caught here and turned into `BlastRadiusResult(ok=False)`
-- the explicit signal a consumer must treat conservatively -- rather than
an exception every caller has to remember to catch.
"""

from __future__ import annotations

from .graphify import BlastRadiusResult, GraphifyClient


class MCPGraphifyClient(GraphifyClient):
    """`GraphifyClient` adapter that talks to a Graphify MCP server."""

    def __init__(self, server_url: str, *, timeout: float = 10.0) -> None:
        self._server_url = server_url
        self._timeout = timeout
        self._session = None

    def _ensure_session(self):
        if self._session is None:
            import mcp  # lazy: only when a real call is made

            self._session = mcp.ClientSession(self._server_url)
        return self._session

    def build(self) -> None:
        self._ensure_session().call_tool("graphify.build", {})

    def reindex(self) -> None:
        self._ensure_session().call_tool("graphify.reindex", {})

    def blast_radius(self, files: list[str]) -> BlastRadiusResult:
        try:
            session = self._ensure_session()
            response = session.call_tool(
                "graphify.blast_radius", {"files": files}, timeout=self._timeout
            )
            return BlastRadiusResult(ok=True, files=set(response.get("files", [])))
        except Exception as exc:  # noqa: BLE001 -- any MCP/network failure is
            # exactly the "query failed" signal this adapter exists to produce.
            return BlastRadiusResult(ok=False, error=str(exc))
