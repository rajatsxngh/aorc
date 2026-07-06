# S6 — Tester + test-critic + red/error gate + interface coverage

## What to build

The spec-encoding stages, kept strictly separate from implementation.

- **Tester agent** (separate from the coder) writes one failing test per task from the design doc, against the design's `interface` only. It must never see implementation code — it tests behavior, not internal details — and must not invent functions the design doc did not specify. Failing tests are committed to the branch before the coder runs, creating a hard red-green boundary.
- **Test-critic agent** (a different agent from the tester, no incentive to pass its own work) reviews the tests against the design doc *before* the coder runs, rejecting any test that references a function not in the design interface or doesn't correspond to a spec.
- **Interface-coverage gate — BEFORE the coder.** Pure static set-comparison: every function in the design's `interface` must be referenced by at least one test (design functions ⊆ functions-under-test). No execution needed. Guards against a shallow suite that silently skips part of the spec.
- **Red vs. error distinction.** A clean assertion failure ("red" — referenced code doesn't exist yet or returns the wrong value; expected in TDD) proceeds to the coder. A test error/crash (broken import, reference to a function not in the design interface, malformed test) stops and returns to tester/design. Only clean red proceeds.

## Acceptance criteria

- [ ] Tester writes one failing test per task, against design `interface` only, never sees impl
- [ ] Failing tests committed to branch before coder runs
- [ ] Test-critic rejects off-spec tests / functions not in the design interface
- [ ] Interface-coverage: static assert design fns ⊆ tested fns, before coder
- [ ] Red (clean assertion fail) → proceed; error/crash → back to tester/design
- [ ] Tester and critic are distinct agents

## Blocked by

- S5 (design doc + schema)
