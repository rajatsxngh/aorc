# S12 — Epic decomposition

## What to build

The refinement path for oversized/epic issues, run orchestrator-side.

- An epic triggers the design agent to read the epic and produce a PRD; the prd-to-issues process then creates concrete, scoped sub-issues on GitHub. Run grill-me on a vague epic *before* decomposing, so sub-issues are based on a clear understanding of intent.
- **Decompose before sequencing:** epics are fully decomposed (and sub-issues re-triaged to actionable) before any sequencing decision, so every real unit of work is on the board first.
- **Idempotency marker:** tag each sub-issue with a deterministic marker in its body (e.g. `aorc-parent: #42`, `aorc-sub-index: 3`). Before creating sub-issue N of parent #42, search for that exact marker; if it exists, **skip**. Re-running decomposition (via re-sync) only creates what's missing — never duplicates, never delete-and-redo.
- Sub-issues re-enter triage and the normal pipeline.
- **Parent auto-close:** the parent epic closes automatically (a GitHub Actions rule) when all its linked sub-issues are closed.
- **Depth:** no behavioral cap (a runaway epics-into-epics loop is halted by the cost caps in S13). Apply a diagnostic-only depth label (e.g. `depth:3`) per level so a cost-cap trip is legible as a non-converging decomposition.

## Acceptance criteria

- [ ] Epic → PRD → prd-to-issues creates scoped sub-issues linked to the parent
- [ ] grill-me runs on a vague epic before decomposition
- [ ] Deterministic marker per sub-issue; re-run creates only what's missing (idempotent, no duplicates)
- [ ] Sub-issues re-enter triage
- [ ] Parent epic auto-closes when all sub-issues close
- [ ] Diagnostic `depth:N` label applied per level; no behavioral depth cap

## Blocked by

- S3 (triage identifies epics), S11 (grill-me on vague epics)
