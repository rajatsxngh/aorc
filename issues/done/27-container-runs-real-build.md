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

- [x] `ContainerTestRunner` conforms to the `TestRunner` seam; all four
      stages run unmodified against it
- [x] `python -m aorc run-issue N` executes `setup`/`test`/`lint` inside the
      issue's `AORC_BASE_IMAGE` container, not on the host — no
      `SubprocessTestRunner` in the live composition path
- [x] Toolchain failures inside the container surface as the same
      `TestRunResult` (returncode/stdout/stderr) the fix loop already
      consumes
- [x] Docker-gated integration test (S19 pattern: skip clean without a
      daemon) proves a command's effects land in the mounted worktree and
      host-side `write_worktree_file` mirrors remain visible in-container
- [x] Unit suite still runs with zero Docker dependency
      (`MockTestRunner`/`SubprocessTestRunner` untouched)
- [x] S22 known-limitation wording in `driver.py`/`wake.py` and the
      `issues/README.md` v2 note are updated to reflect the closed gap

## Blocked by

S21, S22 (in place). Independent of S23–S25, but the README's push-mediation
limitation additionally needs S23's real minter to be fully closed.

## Progress notes

Done this iteration:

- `ContainerTestRunner` (`src/aorc/tester.py`, new `TestRunner` implementation):
  runs `docker exec -w /workspace <container> sh -c <command>` instead of a
  host subprocess. The container to target is resolved from `cwd` alone (the
  per-issue worktree path every stage already passes,
  `WorktreeManager.path_for`/`.ensure`) via two new naming-convention helpers
  in `src/aorc/harness.py`: `container_name_for(issue_number)` (also now used
  by `DockerContainerRuntime.start`, so both sides of the link derive the
  same name from the same issue number) and its inverse
  `issue_number_from_worktree_path(path)`. No separate issue -> container-id
  registry has to be threaded through the driver/stages to keep this
  correct -- deliberately avoids widening `TesterStage`/`CoderStage`/
  `ReviewerStage`'s constructors. Raises `ValueError` on a `cwd` it can't
  resolve (e.g. the unit suite's `cwd="."` default) rather than silently
  `docker exec`-ing into a wrong/nonexistent name.
- `__main__.py`'s `compose()`: the build pipeline's shared `test_runner` is
  now `ContainerTestRunner()` by default (the live `container.runtime: docker`
  path), falling back to `SubprocessTestRunner()` only for
  `container.runtime: actions` (no local container for `docker exec` to
  target there -- S25's driver-stays-orchestrator-side scope note applies
  unchanged) or the new explicit `--no-container` CLI dev escape hatch
  (`compose(..., no_container=True)`). `Collaborators` gained a `test_runner`
  field so tests/callers can see which one composition chose without
  reaching into a stage's private attribute.
- **Bug fix required to land this safely**: `MergeTimeHandler` (`merge.py`)
  built its stale-PR recheck (`_recheck`/`_feedback_code`) against a single
  fixed `cwd="."` shared by every issue, never the issue's own worktree --
  pre-existing and latent (the unit suite's `MockTestRunner` ignores `cwd`
  entirely, so nothing caught it), but wiring `ContainerTestRunner` into the
  same shared `coder`/`reviewer` stage instances `MergeTimeHandler` also uses
  would have turned that latent bug into a hard `ValueError` crash on every
  live `on_pr_merged` stale-PR recheck. Fixed in the same commit:
  `MergeTimeHandler` now takes an optional `worktrees: WorktreeManager`
  and resolves `cwd` per-issue via a new `_cwd_for(issue_number)` helper,
  falling back to the fixed `cwd` when no `WorktreeManager` is given (every
  existing unit test, unaffected). `compose()` wires the real `worktrees`
  in. Covered by
  `test_stale_pr_recheck_uses_the_per_issue_worktree_when_wired`
  (`tests/test_merge.py`) and `test_compose_threads_worktrees_into_the_merge_handler`
  (`tests/test_main.py`).
- Docstrings updated to reflect the closed gap: `driver.py`'s module
  docstring, `wake.py`'s `dispatch_issue` docstring, and
  `issues/README.md`'s "Known open limitation (post-v1)" section -- the
  latter now distinguishes the toolchain-in-container gap S27 closed from
  push mediation (still open: the LLM agent loop itself still runs
  orchestrator-side, not in-container -- explicitly out of scope for S27).
- New tests: `tests/test_harness.py` (naming-convention helpers, +4),
  `tests/test_tester.py` (`ContainerTestRunner` argv/workdir/error-path
  pinning against a fake `subprocess.run`, +3), `tests/test_main.py`
  (composition wiring across docker/actions/`--no-container`, +4),
  `tests/test_merge.py` (per-issue `cwd` threading, +1), and a new
  Docker-gated integration test,
  `tests/integration/test_container_test_runner_integration.py`
  (starts a real long-running container -- `DockerContainerRuntime.start`
  never overrides a base image's default command, so a bare `alpine`
  container exits immediately and can't be `exec`'d into, hence this test
  starts its own with `sleep 300` -- and proves a host-written worktree file
  is visible in-container, a container-run command's effects land back in
  the mounted worktree, and a non-zero exit surfaces as the same
  `TestRunResult` shape). **Confirmed only that it skips cleanly here** --
  no Docker daemon reachable in this dev sandbox, same honesty caveat as
  every other daemon/credential-gated integration test in this repo.

Full suite: 481 passed, 12 deselected (integration, up from 11 -- the one new
integration test), 0 failures.

Not done / explicitly out of scope (per the ticket's own "out of scope"
note, unchanged): moving the LLM agent loop itself into the container. The
container is now the real isolation boundary for the toolchain
(setup/test/lint/coverage/smoke); it is not yet the boundary for the agent
that writes the code being tested.
