# S17 — Merge-time handling + auto-rollback

## What to build

Everything that happens around merge, and self-healing when a bad merge lands. Human merge is the gate in v1 (`auto: false`); auto-merge stays deferred behind the graduation gate.

- **Issue auto-close:** the GitHub issue closes automatically when its PR is merged (→ Done column).
- **Merge conflict at PR-open.** When main has moved: attempt a rebase. Clean rebase (main changed files this issue didn't touch) → do it automatically, re-run the reviewer against the rebased result, open the PR. True conflict (same lines edited) → `agent-blocked` (reason: merge conflict with main); the coder must not freelance a resolution. Discriminator is mechanical (git reports conflict or not).
- **Stale approved PR.** When any PR merges, every open AORC PR whose file set overlaps the merged one is rebased/re-tested against the new HEAD and the reviewer re-runs. Still passes → stays approved, waiting for the human. Broke → back into the bounded fix loop. Never auto-close as superseded; never blindly leave stale.
- **Human PR feedback (v1).** The agent watches its own PR for the human's review comments and acts, routed mechanically by intent: a comment about **code** → coder re-enters the fix loop (tests stay locked); a comment implying the **spec/tests are wrong** → kick back to design/tester, then re-run forward. The coder never edits a locked test. Requires distinguishing the human's comments from the reviewer-agent's own (avoid self-triggered loops).
- **Auto-rollback.** After a merge, if main's test suite goes red, an Actions workflow on push-to-main automatically reverts the offending PR. For each in-flight container, compare the reverted file set against its reported checkpoint `files`: overlap (direct or blast-radius) → tear down + re-queue (Design preserved, re-runs against corrected HEAD); no overlap → continue; no checkpoint reached yet → conservatively kill + re-queue.

## Acceptance criteria

- [ ] Issue auto-closes on PR merge (→ Done)
- [ ] Clean rebase auto-applied + reviewer re-run; true conflict → `agent-blocked` (mechanical git discriminator)
- [ ] On any merge, overlapping open AORC PRs rebased/re-tested/re-reviewed; broke → fix loop, else stays approved
- [ ] Human PR comment routed by intent: code → coder loop (tests locked); spec → design/tester then forward
- [ ] Human comments distinguished from reviewer-agent comments (no self-trigger)
- [ ] Auto-rollback reverts a main-breaking PR; in-flight containers handled by overlap (kill+re-queue / continue / conservative kill)

## Blocked by

- S8 (PR pipeline), S10 (checkpoint file lists / in-flight set)
