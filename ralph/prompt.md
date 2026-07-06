ISSUES
Local issue files from `issues/` are provided at the start of context. Parse them to understand the open issues. Each file is one vertical slice (S1–S18) with "What to build", "Acceptance criteria", and "Blocked by" sections.

Work ONLY on issues that are NOT marked `HITL: true` in their frontmatter/header. Issues marked `HITL: true` are human-supervised and must be skipped by the loop — leave them untouched. If the only remaining issues are HITL ones, output NO MORE TASKS.

You've also been passed a file containing the last few commits. Review these to understand what work prior iterations already completed. Do not redo completed work.

If all non-HITL tasks are complete (their issue files have been moved to `issues/done/`), output NO MORE TASKS.

TASK SELECTION
Pick the SINGLE next task. Respect the "Blocked by" field: never start an issue whose blockers are not yet in `issues/done/`. Among unblocked issues, prioritize in this order:

1. Critical bugfixes (a previously-committed slice whose tests now fail)
2. Development infrastructure (project scaffolding, test harness, dev scripts) — a precursor to everything else
3. Tracer-bullet feature slices, lowest slice number first (S1, then S2, ...)
4. Polish and quick wins
5. Refactors

PROJECT STATE — READ THIS CAREFULLY
This is a Python project being built FROM SCRATCH. On the earliest iterations the repository is nearly empty — there is no orchestrator file, no existing framework, no LangGraph, no pre-existing modules. Do NOT assume any file exists until you have looked. Explore the repo (`ls`, read what's there) before editing. If a piece of scaffolding a task needs does not exist yet, creating it IS the task (that is why "development infrastructure" is priority 2).

Follow the architecture in the issue files and in `AORC-PRD-v1-FINAL-tagged.md` at the repo root. Two hard rules from the PRD that must hold in all code you write:
- Orchestrator logic depends only on the `GitHubClient` and `LLMClient` interfaces, never on a provider SDK or the GitHub SDK directly.
- No hardcoded model names or secrets in code; configuration comes from `.aorc.yml`.

IMPLEMENTATION
Use test-driven development: write the failing test FIRST, run it, watch it fail for the right reason, then write the minimum code to make it pass. You may invoke the /tdd skill. Keep each iteration to ONE slice's worth of behavior.

FEEDBACK LOOPS
Before committing, run the project's tests and they must pass:

* Run: `pytest -q`

If there is not yet a test runner configured (very early iterations), setting one up is itself a valid "development infrastructure" task — do that as the task, commit it, and stop the iteration. Never commit code whose tests do not pass. If tests are red and you cannot get them green this iteration, do NOT commit; instead write your findings into the issue file (see THE ISSUE) and stop.

COMMIT
Make exactly one git commit at the end of a successful iteration. The commit message must include:

1. Which slice (e.g. "S1") and what behavior was implemented
2. Key decisions made
3. Files changed
4. Blockers or notes for the next iteration

HONESTY IN COMMITS
Never claim a test, check, or integration exists unless you actually created it in this repo this iteration. Do not write "exercised by integration tests" unless those test files really exist in tests/. If you only wrote unit tests against mocks, say so plainly: "real adapter NOT yet integration-tested — mocks only." Overstating what is proven is a defect.

THE ISSUE
If the task's acceptance criteria are fully met, move its issue file to `issues/done/`.
If the task is only partially complete, leave the file in place and append a "## Progress notes" section recording exactly what was done and what remains, so the next iteration continues cleanly.

FINAL RULES
ONLY WORK ON A SINGLE TASK PER ITERATION.
NEVER modify a test to make it pass — fix the code.
NEVER touch an issue marked `HITL: true`.
If unsure whether the repo is in a safe state to proceed, stop and output NO MORE TASKS rather than guessing.