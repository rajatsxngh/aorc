# S13 — Cost + compute guards

## What to build

The hard, unattended-safety limits. All are config values; start conservative and tune on real data.

**Cost circuit-breaker (three levels, all ping-on-hit):**
- **Per-issue:** ~$3–5 across an issue's full pipeline → stop, label `agent-blocked` (reason: cost cap), ping. Primary guard; catches one stuck agent.
- **Per-run (one wake cycle):** ~$50 → pause the whole system, ping. Catches a systemic bug.
- **Daily:** ~$100/day → halt everything until manually reset. Backstop.

**Cost-cap mid-issue behavior (clean-stop).** On per-issue trip mid-pipeline, finish the current stage's commit, then stop at the natural artifact boundary → `agent-blocked` (reason: cost cap), ping; resume later from a *valid* committed artifact. **Overshoot guard:** if a single stage is blowing the budget (stuck fix loop), hard-stop at 1.5× the per-issue cap. Rationale: a mid-stage hard-kill would leave a half-written design doc that fails the strict schema and masquerade as a *clarity* problem — clean-stop avoids the wrong signal.

**Container compute limits:**
- **Wall-clock:** 30–45 min per container → kill, `agent-blocked`, ping (well under the 6-hr Actions ceiling).
- **Memory/CPU:** default GitHub runner (~2 CPU / 7 GB); never opt into larger runners.
- Whichever trips first — cost cap or timeout — stops the agent.

## Acceptance criteria

- [ ] Per-issue / per-run / daily caps each fire and ping with the right action (block / pause / halt)
- [ ] Mid-issue cost trip clean-stops at the artifact boundary, lands `agent-blocked` (reason: cost cap)
- [ ] Single-stage overshoot hard-stops at 1.5× per-issue cap
- [ ] Wall-clock timeout (30–45 min) kills the container → `agent-blocked`
- [ ] Runner size fixed at default; no larger-runner opt-in
- [ ] Each threshold lands the issue in `agent-blocked` with the correct reason

## Blocked by

- S4 (container harness to bound)
