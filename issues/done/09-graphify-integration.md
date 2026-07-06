# S9 — Graphify integration

## What to build

Wire in the Graphify knowledge graph as an existing tool consumed via its MCP interface (not rebuilt). It is the one standing piece of infrastructure — orchestrator-owned, one graph per repo.

- Build the graph on orchestrator startup so it understands which modules depend on which.
- Re-index after every merged PR, so blast-radius calculations stay accurate as the codebase evolves.
- Agents query it read-only; the design agent uses blast-radius queries during S5.
- Provide a query surface the collision checker (S10) consumes: "do issue A's files sit in issue B's import/call blast radius?"
- Query failure/timeout is a first-class signal the consumer must handle conservatively (S10 treats it as a hold).

The graph DB may require a self-hosted runtime; treat it as orchestrator-owned shared infrastructure.

## Acceptance criteria

- [ ] Graph built on startup via the Graphify MCP interface
- [ ] Re-index triggered on every merged PR
- [ ] Read-only blast-radius query surface exposed for Design and the collision checker
- [ ] Query failure/timeout surfaced as an explicit signal (not silently empty)

## Blocked by

- S1 (client abstractions / MCP wiring)
