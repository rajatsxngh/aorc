# S4 — Container harness + checkpoint spine

## What to build

The per-issue execution harness that later pipeline stages (S5–S8) run inside. One actionable issue is dispatched to one isolated container; nothing carries across issues.

- Dispatch a single actionable issue as an Actions job → Docker container from a pre-baked base image (Claude Code + skills + MCPs installed).
- Create a git worktree per issue; each container works on its own worktree. Branch name `aorc/issue-<number>` (deterministic, greppable — lets the orchestrator reconstruct branch↔issue with no DB). One branch per issue, reused across re-dispatches.
- **Checkpoint mechanic:** the container runs Design first, then stops at a checkpoint and reports its exact file list to the orchestrator, waiting for a `proceed`/`hold` verdict before continuing. In this slice the verdict is trivially `proceed` (only one issue in flight); real collision logic arrives in S10.
- Container teardown after the pipeline completes or on a hold. One container = exactly one issue.
- Branch cleanup, three fixed cases (no agent judgment): merged → delete; `agent-blocked` → keep; held → keep.

This slice builds the harness/plumbing; the checkpoint returns `proceed` unconditionally until S10 wires in collision.

## Acceptance criteria

- [ ] Single issue dispatched to one container from the base image
- [ ] Per-issue git worktree; branch `aorc/issue-<number>` created and reused on re-dispatch
- [ ] Checkpoint: container reports file list and waits for orchestrator verdict (trivial `proceed` here)
- [ ] Container torn down on completion or hold; one container ↔ one issue
- [ ] Branch cleanup follows the three fixed cases

## Blocked by

- S2 (labels), S3 (triage → dispatch)
