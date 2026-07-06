# S5 — Design stage + strict schema + actionability gate

## What to build

The first pipeline stage inside the container, and the authoritative actionability gate. A design agent reads the issue (+ any clarification Q&A + relevant repo files + Graphify blast-radius queries once S9 lands) and emits a design doc in a strict, machine-readable schema — not freeform. This is the single biggest determinism lever: tester and coder must consume it identically every time.

Required schema fields:
- `interface` — functions with `name` / `inputs` / `outputs`
- `test_specs` — behaviors to test
- `task_list` — ordered implementation steps (coder executes in order, no reordering)
- `files` — exact files touched (drives the checkpoint; the orchestrator reads this field)
- `confidence` — flag

The design agent runs with fresh, scoped context (issue + Q&A + relevant repo files only). The design doc is committed to the issue's working branch as the persistent, resumable record.

**Actionability gate — Design decides.** Design attempts to produce a bounded interface + finite test specs + ordered task list. Can bound it → proceed to checkpoint. Cannot bound it (task list explodes, interface can't be pinned, low confidence) → refuse, return to the refinement path.

**Invalid-schema routing (mechanical, no LLM judgment):**
- Parses but low `confidence` → `needs-clarification` (gate working as intended).
- Fails to parse / missing required fields (a *format* miss) → retry the design agent N times, then `agent-blocked` — **not** `needs-clarification`.

## Acceptance criteria

- [ ] Design agent emits the strict schema with all five required fields
- [ ] Scoped context only; no cross-stage context carried in
- [ ] Design doc committed to `aorc/issue-<n>` branch
- [ ] Schema validator: valid → proceed; parse-fail → retry-N-then-`agent-blocked`; low-confidence → `needs-clarification`
- [ ] Discriminator is a pure schema check — no LLM judgment
- [ ] `files` field feeds the S4 checkpoint report

## Blocked by

- S4 (container harness + checkpoint)
