# S3 — Triage (orchestrator-side)

## What to build

A cheap, orchestrator-side first-pass classifier. On each issue event a triage agent classifies an issue as `actionable` or `not-ready` — deliberately a fast, low-cost guess, **not** the authoritative gate (Design decides authoritatively, later in S5).

- **Actionable** = has a bounded, testable definition of done (Design can produce a bounded interface and the tester can write a finite pass/fail test set). Defined by testability, not by an LLM's judgment of size.
- **Not-ready** carries a reason tag for the human only: `vague` (needs clarification) or `epic` (needs decomposition; hints: a `- [ ]` task list or an `epic` label). These tags do not define separate pipelines — both route to the one shared refinement path.
- **Idempotent re-triage:** issues produced by the refinement path (clarified issues, decomposed sub-issues) re-enter triage. Most pass as actionable in one cheap step; only still-vague/oversized ones loop again.

Runs orchestrator-side with no build container. Mock `LLMClient` for controlled vagueness in tests.

## Acceptance criteria

- [ ] `actionable | not-ready(+reason)` output from issue JSON
- [ ] Reason tag `vague` or `epic` applied on not-ready
- [ ] Edge cases handled: task list but no epic label, epic label but no task list, empty body
- [ ] Re-triage is idempotent (refinement-path output re-enters cleanly)
- [ ] Runs with no container; tests mock `LLMClient`

## Blocked by

- S1 (`LLMClient`)
