# S27 — Run the build inside the container (v2: make isolation real)

`ConfigGatedWakeLoop.dispatch_issue` starts a per-issue Docker container
(`DockerContainerRuntime.start`: mounts the worktree at `/workspace`, injects
the broker-built env) — and then the container runs **no command at all**.
The actual design/test/code/review sequence runs orchestrator-side via
`PipelineDriver`, and every toolchain invocation (`setup`/`test`/`lint`/
coverage) executes on the **host** through `SubprocessTestRunner`. The
container isolation boundary is currently cosmetic: untrusted, LLM-generated
code from `.aorc.yml`-configured commands runs with the orchestrator's own
user, filesystem, and network. This is the documented S22 known limitation
(`driver.py` module docstring, `wake.py:dispatch_issue`) and pairs with
`issues/README.md`'s "Known open limitation (post-v1)" — token push
mediation also requires the real in-container execution path, "which v1
never builds. First work item for any v2."

## What to build (v2 scope)

The per-issue build must actually execute inside the container the harness
already starts:

1. A `ContainerTestRunner` implementing the existing `TestRunner` seam
   (`run(cwd, command) -> TestRunResult`) that executes the command via
   `docker exec` in the issue's container against the mounted `/workspace`,
   instead of a host subprocess. The driver and stages don't change —
   `TestRunner` is already the injection point (invariant #1 holds).
2. Composition root (`__main__.py`) wires `ContainerTestRunner` into the
   stage constructors when a real runtime is in play; `SubprocessTestRunner`
   remains for tests and a possible explicit `--no-container` dev escape
   hatch.
3. The driver (or dispatcher) must guarantee the container is up before the
   first toolchain run and that the runner targets the right per-issue
   container — today `dispatch_issue` starts the container and then runs the
   driver with no link between the two.
4. Revisit the `issues/README.md` post-v1 limitation: with commands running
   in-container, decide what the container may reach (network egress, token
   scope) — this ticket is the prerequisite for orchestrator-side push
   mediation / the scrubbing egress proxy described there.

Out of scope: moving the LLM agent loop itself into the container (the PRD's
full in-agent execution path). This ticket moves only the toolchain
(setup/test/lint/coverage/smoke) inside; stages still orchestrate from the
host.

## Acceptance criteria

- [ ] `ContainerTestRunner` conforms to the `TestRunner` seam; all four
      stages run unmodified against it
- [ ] `python -m aorc run-issue N` executes `setup`/`test`/`lint` inside the
      issue's `AORC_BASE_IMAGE` container, not on the host — no
      `SubprocessTestRunner` in the live composition path
- [ ] Toolchain failures inside the container surface as the same
      `TestRunResult` (returncode/stdout/stderr) the fix loop already
      consumes
- [ ] Docker-gated integration test (S19 pattern: skip clean without a
      daemon) proves a command's effects land in the mounted worktree and
      host-side `write_worktree_file` mirrors remain visible in-container
- [ ] Unit suite still runs with zero Docker dependency
      (`MockTestRunner`/`SubprocessTestRunner` untouched)
- [ ] S22 known-limitation wording in `driver.py`/`wake.py` and the
      `issues/README.md` v2 note are updated to reflect the closed gap

## Blocked by

S21, S22 (in place). Independent of S23–S25, but the README's push-mediation
limitation additionally needs S23's real minter to be fully closed.
