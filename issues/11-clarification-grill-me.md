# S11 — Clarification (grill-me)

## What to build

The refinement path for vague issues, run orchestrator-side (no build container).

- A vague issue triggers the grill-me skill (relentless interrogation of the problem statement), which posts targeted clarifying questions **one at a time** as GitHub comments by the App bot account — a conversation, not a form dump.
- Label the issue `needs-clarification` (→ Needs Clarification column, distinct from `agent-blocked`).
- A webhook listener watches for new comments on `needs-clarification` issues.
- **Resume is permission-gated AND content-evaluated.** A reply resumes only if it comes from someone with write/triage access (gated on GitHub permission/author-association, not identity). A new comment does not blindly resume: the bot re-runs its clarity evaluation on the reply's *content*. Actually answers the open questions → resume (re-enter triage). "thanks!"/partial → post the next question / restate what's missing, stay in `needs-clarification`.
- **Timeout (nudge, then block; both windows config).** After window 1 (default 7 days): post one nudge comment. After window 2 (another 7 days): label `agent-blocked` (reason: clarification timed out) and move out of the active waiting list. Windows set to infinity restore wait-forever behavior.

## Acceptance criteria

- [ ] Questions posted one at a time via the bot account; `needs-clarification` label applied
- [ ] Comment webhook resumes only on a write/triage-permissioned reply that actually answers
- [ ] Partial/off-topic reply → next question or restate, stays `needs-clarification`
- [ ] Nudge at window 1 (default 7d); block at window 2 (+7d); windows configurable incl. infinity
- [ ] Resumed issues re-enter triage
- [ ] Runs orchestrator-side, tests mock `GitHubClient`

## Blocked by

- S3 (triage identifies vague issues)
