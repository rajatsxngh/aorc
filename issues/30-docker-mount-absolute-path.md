# S30 — Docker `-v` mount must use an absolute host path for the worktree

A live `python -m aorc run-issue` against real Docker surfaced a bug the
mock suite could not see: `DockerContainerRuntime.start` builds the bind
mount as `-v {worktree_path}:/workspace` with whatever path it was handed,
and the composition root wires `WorktreeManager` with the **relative**
`worktrees_dir` `.aorc-worktrees` — so the mount argument comes out as
`.aorc-worktrees/issue-<n>:/workspace`. Docker's `-v` flag requires an
absolute host path for a bind mount; a relative source is rejected and
`docker run` fails with exit status 125 before the container ever starts.

Confirmed by diagnosis: the identical `docker run` invocation with an
absolute source (`$(pwd)/.aorc-worktrees/issue-<n>:/workspace`) succeeds;
the relative one fails.

Why every test passed anyway: the unit suite fakes `subprocess.run` (it
checks argv/env-file hygiene, never Docker's path rules), and the
Docker-gated integration test happens to pass pytest's `tmp_path`, which is
already absolute — so the relative-path case never reached a real daemon.

## What to build

1. In `DockerContainerRuntime.start`, resolve the worktree path to an
   absolute host path (`os.path.abspath(...)`) before building the `-v`
   mount argument. Audit for any other place a worktree path is passed to a
   docker mount (there is none today — `ContainerTestRunner` uses
   `docker exec` against the already-mounted `/workspace`).

## Acceptance criteria

- [x] Unit test on the `docker run` argv construction: calling `start` with
      a **relative** worktree path produces a `-v` mount whose source is an
      absolute path (fails before the fix, passes after)
- [x] The Docker-gated integration test
      (`tests/integration/test_docker_integration.py`) passes a relative
      worktree path and asserts the container's actual mount source is
      absolute
- [x] Full suite green

## Blocked by

Nothing.

## Progress notes

Fixed in the same change that filed this ticket: `DockerContainerRuntime.
start` now resolves `worktree_path` with `os.path.abspath` before building
the `-v` argument. Unit test
(`test_docker_start_mounts_worktree_at_absolute_path`) was written first
and observed failing (`isabs('.aorc-worktrees/issue-7')` false) before the
fix. Unlike S23–S25, the integration test WAS run against a real Docker
daemon this iteration: it now passes a relative worktree path and passed
live, confirming the resolved mount actually starts a container.
