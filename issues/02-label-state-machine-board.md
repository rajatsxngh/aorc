# S2 — Label state machine + board derivation

## What to build

Pipeline state stored exclusively in GitHub labels — no external database. The Projects board column is *derived* from the label via a fixed lookup; agents never set a column directly, so label and board can never disagree.

Fixed lookup:

| Label | Column |
|---|---|
| (none / triaged) | Backlog |
| `needs-clarification` | Needs Clarification |
| `in-design`, `in-test`, `in-code` | In Progress |
| `in-review` | In Review |
| `agent-blocked` | Blocked |
| (issue closed) | Done |

`needs-clarification` (waiting on a human answer) and `agent-blocked` (a stuck agent needing debugging) are deliberately separate columns — they demand different human actions.

A stage is complete only when its artifact exists (design doc committed, test file committed, PR opened) — the label alone is not sufficient. On orchestrator restart, reconstruct pipeline state by reading labels **and** checking for artifacts, then resume correctly. Given label events, the machine emits the correct next stage and the derived column.

## Acceptance criteria

- [ ] Label→column lookup implemented as the single fixed mapping above
- [ ] State machine: label event → next stage + derived column
- [ ] Artifact-presence check gates stage completion (label-only never advances state)
- [ ] Crash recovery: reconstruct state from labels + artifacts on restart
- [ ] Every label transition covered by tests using the `GitHubClient` mock

## Blocked by

- S1 (`GitHubClient`)
