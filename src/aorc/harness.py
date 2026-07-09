"""S4 -- Container harness + checkpoint spine. S20 -- Checkpoint injection
+ in-flight file-claim registry. S10 -- real collision verdict. S13 --
wall-clock compute guard wired into teardown.

The per-issue execution harness that S5-S8 run inside: one actionable issue,
one isolated container, one git worktree. Nothing carries across issues.

Worktree/branch creation shells out to real `git` -- git is the substrate
itself, not a provider SDK, so this doesn't cross architecture invariant #1
(which forbids depending on provider/GitHub SDKs directly). Actual container
dispatch (Docker/Actions) sits behind the `ContainerRuntime` seam, mirroring
the `GitHubClient`/`LLMClient` pattern, so harness orchestration logic is
testable without a real Docker daemon or Actions runner.

`Checkpoint` is injected into `ContainerHarness` (default: wired to the
harness's own `GitHubClient` and, if given, the same `GraphifyClient`), and
carries the collaborators real collision detection needs -- the
`GitHubClient` (open PRs), a `GraphifyClient` (import/call blast radius),
and an `InFlightRegistry` of other in-flight issues' claimed file lists,
recorded per-issue at the checkpoint and cleared on teardown. See
`Checkpoint.verdict` for the real hold/proceed rule; `dispatch.py` covers the
up-front selector (concurrency ceiling + declared blockers) that runs
*before* a container ever starts.

`ContainerHarness.enforce_wall_clock` wires `guards.ComputeGuard`'s pure
wall-clock decision into a real kill: label `agent-blocked`, ping, and
teardown through the same branch-preserving path any other agent-blocked
outcome uses. `guards.CostGuard` (the other half of S13, cost circuit
breakers) is issue/run/day scoped rather than container scoped, so it isn't
threaded through this class -- callers use it directly alongside whichever
stage they're running.
"""

from __future__ import annotations

import base64
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .credentials import assert_env_clean
from .graphify import GraphifyClient
from .guards import BLOCKED_LABEL, TIMEOUT_PING_MARKER, ComputeGuard, ComputeVerdict
from .interfaces import GitHubClient
from .pipeline import LABEL_COLUMN, branch_name

# The three fixed branch-cleanup outcomes -- no agent judgment involved.
_CLEANUP_OUTCOMES = {"merged", "agent-blocked", "held"}


@dataclass
class ContainerHandle:
    issue_number: int
    branch: str
    worktree_path: str
    container_id: str
    status: str = "running"  # "running" | "stopped"


@dataclass
class CheckpointReport:
    """What a container reports at the post-Design checkpoint before it is
    allowed to proceed to Tester/Coder/Reviewer."""

    issue_number: int
    files: list[str] = field(default_factory=list)


def container_name_for(issue_number: int) -> str:
    """The one naming convention both `DockerContainerRuntime.start` (which
    names the container this way) and `tester.ContainerTestRunner` (S27,
    which `docker exec`s into it) rely on -- no separate handle registry
    needed to link a driver's toolchain run back to the container the
    harness already started for the same issue."""
    return f"aorc-issue-{issue_number}"


_WORKTREE_ISSUE_RE = re.compile(r"issue-(\d+)$")


def issue_number_from_worktree_path(path: str) -> int | None:
    """Inverse of `WorktreeManager.path_for`: recovers the issue number from
    a per-issue worktree path (the `cwd` every stage's `TestRunner` runs
    against), so `ContainerTestRunner` can resolve the matching
    `container_name_for` without threading a handle through the driver."""
    match = _WORKTREE_ISSUE_RE.search(os.path.basename(path.rstrip("/")))
    return int(match.group(1)) if match else None


class ContainerRuntime(ABC):
    """Dispatch/teardown of the per-issue build container. `env` is the
    container's complete credential surface (S15): the per-issue GitHub
    token and the LLM key, built by `credentials.CredentialBroker` -- the
    App private key can never appear here because the broker fails closed
    before it ever would."""

    @abstractmethod
    def start(
        self,
        issue_number: int,
        branch: str,
        worktree_path: str,
        env: dict[str, str] | None = None,
    ) -> ContainerHandle: ...

    @abstractmethod
    def teardown(self, handle: ContainerHandle) -> None: ...


class MockContainerRuntime(ContainerRuntime):
    """In-memory `ContainerRuntime` for unit tests; records every call like
    `MockGitHubClient` does."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.envs: dict[int, dict[str, str] | None] = {}
        self._next_id = 1

    def start(
        self,
        issue_number: int,
        branch: str,
        worktree_path: str,
        env: dict[str, str] | None = None,
    ) -> ContainerHandle:
        handle = ContainerHandle(
            issue_number=issue_number,
            branch=branch,
            worktree_path=worktree_path,
            container_id=f"mock-container-{self._next_id}",
        )
        self._next_id += 1
        self.calls.append(("start", issue_number, branch))
        self.envs[issue_number] = env
        return handle

    def teardown(self, handle: ContainerHandle) -> None:
        handle.status = "stopped"
        self.calls.append(("teardown", handle.issue_number))


class DockerContainerRuntime(ContainerRuntime):
    """Real per-issue container dispatch via the Docker CLI, from the
    pre-baked base image (Claude Code + skills + MCPs installed). Exercised
    by `tests/integration/test_docker_integration.py` against a real Docker
    daemon (daemon-gated, separate from the unit suite, which uses
    `MockContainerRuntime`); argv/env-file hygiene is unit-tested."""

    def __init__(self, base_image: str) -> None:
        self._base_image = base_image

    def start(
        self,
        issue_number: int,
        branch: str,
        worktree_path: str,
        env: dict[str, str] | None = None,
    ) -> ContainerHandle:
        name = container_name_for(issue_number)
        # Docker's `-v` rejects a relative host source (exit 125); the
        # composition root wires WorktreeManager with the relative
        # `.aorc-worktrees`, so resolve here, at the mount boundary.
        mount_source = os.path.abspath(worktree_path)
        # Secrets must never ride in argv: `docker run -e KEY=value` exposes
        # the value to every user on the host via `ps`. Deliver env through a
        # 0600 temp env-file instead, removed the moment `docker run` returns
        # (docker reads it synchronously). Values must be single-line -- true
        # of everything the S15 broker mints (tokens/keys).
        env_args: list[str] = []
        env_file: str | None = None
        if env:
            fd, env_file = tempfile.mkstemp(prefix="aorc-env-")  # 0600 by default
            with os.fdopen(fd, "w") as fh:
                for key, value in env.items():
                    fh.write(f"{key}={value}\n")
            env_args = ["--env-file", env_file]
        try:
            subprocess.run(
                [
                    "docker", "run", "-d", "--name", name,
                    *env_args,
                    "-v", f"{mount_source}:/workspace",
                    "-w", "/workspace",
                    self._base_image,
                    # S33: without a pinned command the container's lifetime
                    # is the image's default CMD -- a stock image exits
                    # instantly and every later `docker exec` toolchain run
                    # (ContainerTestRunner, S27) fails "container is not
                    # running". `tail -f /dev/null` is busybox-compatible;
                    # teardown's `docker rm -f` still ends it.
                    "tail", "-f", "/dev/null",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            if env_file is not None:
                os.unlink(env_file)
        return ContainerHandle(
            issue_number=issue_number,
            branch=branch,
            worktree_path=worktree_path,
            container_id=name,
        )

    def teardown(self, handle: ContainerHandle) -> None:
        subprocess.run(["docker", "rm", "-f", handle.container_id], check=True, capture_output=True, text=True)
        handle.status = "stopped"


# --------------------------------------------------------------------------- #
# S35: the target-repo clone every real worktree hangs off
# --------------------------------------------------------------------------- #
# Live bug: the composition root defaulted repo_dir="." (the orchestrator's
# own cwd), so per-issue worktrees were checkouts of AORC itself and the
# container ran pytest against AORC's own test suite. Worktrees must only
# ever be built from a checkout of the TARGET repo (`AORC_REPO`); when
# repo_dir isn't one, AORC materializes its own clone.


class TargetRepoError(Exception):
    """The local checkout AORC would build worktrees from is not -- and could
    not be made into -- a clone of the target repo. Fail closed: dispatching
    from the wrong tree runs the wrong project's toolchain."""


class WorktreeSyncConflict(Exception):
    """S45: rebasing an issue's worktree branch onto the current remote main
    hit a true conflict. Never auto-resolved (same discipline as
    `LocalGitOps.rebase`): the rebase is aborted, the worktree left where it
    was, and dispatch maps this to agent-blocked/re-hold."""


DEFAULT_CLONE_DIR = os.path.join(".aorc", "clone")

_REMOTE_SLUG_RE = re.compile(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?/?$")


def origin_repo_slug(repo_dir: str) -> str | None:
    """The `owner/repo` slug of `repo_dir`'s `origin` remote, or None when
    the directory isn't a git repo / has no origin. Handles the common URL
    forms (https, ssh, with/without `.git`)."""
    proc = subprocess.run(
        ["git", "-C", repo_dir, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    match = _REMOTE_SLUG_RE.search(proc.stdout.strip())
    return match.group(1) if match else None


def _git_auth_env(token: str | None) -> dict[str, str] | None:
    """Deliver the token through git's env-config channel: never in argv
    (`ps`-visible, S19 discipline) and never persisted into the clone's
    `.git/config` -- the remote URL stays credential-free."""
    if not token:
        return None
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    )
    return env


def ensure_target_clone(
    repo_dir: str,
    repo: str,
    token: str | None = None,
    clone_dir: str = DEFAULT_CLONE_DIR,
    clone_url: str | None = None,
) -> str:
    """Resolve the directory worktrees are built from to a checkout of
    `repo` (the `owner/repo` target):

    - `repo_dir` already a checkout of `repo` -> used as-is.
    - otherwise -> the mismatch is surfaced on stderr and AORC's own clone
      at `clone_dir` is used: reused (with a fetch) when it matches,
      created fresh when absent. A `clone_dir` occupied by anything else,
      or a failed clone, raises `TargetRepoError` -- fail closed, never
      dispatch from the wrong tree.

    `clone_url` overrides the default `https://github.com/{repo}.git`
    (test seam: a local bare repo)."""
    found = origin_repo_slug(repo_dir)
    if found == repo:
        return repo_dir
    print(
        f"aorc: repo_dir {repo_dir!r} is a checkout of {found or '(no git repo)'!r}, "
        f"expected {repo!r} -- using target clone at {clone_dir!r}",
        file=sys.stderr,
    )
    existing = origin_repo_slug(clone_dir)
    if existing == repo:
        # Freshen on reuse (S26 spirit); a failed fetch (offline, expired
        # token) leaves a stale-but-correct clone, not a dead run.
        subprocess.run(
            ["git", "-C", clone_dir, "fetch", "origin"],
            capture_output=True,
            text=True,
            env=_git_auth_env(token),
        )
        return clone_dir
    if os.path.isdir(clone_dir):
        raise TargetRepoError(
            f"clone dir {clone_dir!r} exists but is a checkout of "
            f"{existing or '(no git repo)'!r}, expected {repo!r} -- remove it "
            "or point AORC at a checkout of the target repo"
        )
    url = clone_url or f"https://github.com/{repo}.git"
    parent = os.path.dirname(clone_dir)
    if parent:
        os.makedirs(parent, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", url, clone_dir],
        capture_output=True,
        text=True,
        env=_git_auth_env(token),
    )
    if proc.returncode != 0:
        raise TargetRepoError(
            f"repo_dir {repo_dir!r} is a checkout of {found or '(no git repo)'!r}, "
            f"expected {repo!r}, and cloning {url!r} into {clone_dir!r} failed: "
            f"{proc.stderr.strip()}"
        )
    return clone_dir


class WorktreeManager:
    """One git worktree per issue, on branch `aorc/issue-<n>`, created once
    and reused across re-dispatches.

    S26 -- fetch-on-resume: a missing/fresh worktree is rebuilt from the
    fetched `origin` remote rather than the local clone's possibly-stale
    branch state, so a cold start (fresh checkout, deleted worktree dir, or
    an orchestrator restarted on a different machine) sees every commit
    that reached the branch via the GitHub API since this clone last synced.
    An already-present worktree is left untouched -- S22's mirror-on-commit
    path keeps a hot worktree in sync without a remote round-trip, so
    resuming into a worktree dir that already exists never needs a fetch.
    """

    def __init__(self, repo_dir: str, worktrees_dir: str) -> None:
        self._repo_dir = repo_dir
        # S35: anchor to the orchestrator's cwd at construction. `_git` runs
        # with cwd=repo_dir (now possibly the .aorc/clone checkout), so a
        # relative worktrees_dir would resolve against the clone in git's
        # eyes while `path_for`/`os.path.isdir` resolve against the cwd --
        # a split-brain that was masked when repo_dir was always ".".
        self._worktrees_dir = os.path.abspath(worktrees_dir)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self._repo_dir, capture_output=True, text=True, check=check
        )

    def path_for(self, issue_number: int) -> str:
        return os.path.join(self._worktrees_dir, f"issue-{issue_number}")

    def _has_remote(self) -> bool:
        return "origin" in self._git("remote", check=False).stdout.split()

    def _worktree_git(self, path: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", path, *args], capture_output=True, text=True
        )

    def _sync_with_main(self, path: str, branch: str) -> None:
        """S45: bring an existing worktree up to the current remote main.
        Fetch, then rebase the worktree's branch onto `origin/main`."""
        self._git("fetch", "origin", check=False)
        if not self._ref_exists("origin/main"):
            return
        # A dirty tree makes `git rebase` refuse outright, and uncommitted
        # S22-mirrored writes are expected here. When the branch exists on
        # the remote, every mirrored write was also API-committed there, so
        # resetting to the fetched tip loses nothing. Otherwise (the branch
        # never pushed -- mock-backed worlds, a pre-first-commit dispatch)
        # the uncommitted writes are the only copy: --autostash carries them
        # across the rebase instead.
        if self._ref_exists(f"origin/{branch}"):
            self._worktree_git(path, "reset", "--hard", f"origin/{branch}")
        proc = self._worktree_git(path, "rebase", "--autostash", "origin/main")
        if proc.returncode != 0:
            # Same lines changed on both sides: abort so the worktree is left
            # exactly where it was (never mid-rebase) and surface the typed
            # conflict -- dispatch routes it, nothing here auto-resolves.
            self._worktree_git(path, "rebase", "--abort")
            raise WorktreeSyncConflict(
                f"rebasing {branch} onto origin/main conflicts: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )

    def _ref_exists(self, ref: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0

    def ensure(self, issue_number: int) -> str:
        """Create the branch + worktree if this is the first dispatch (or
        the worktree dir was deleted/never made on this machine); on a
        re-dispatch, sync the existing worktree with the CURRENT remote
        main (S45) instead of reusing it blindly -- a held issue released
        after another issue merged must build on the merged main, or the
        coder's full-file rewrites silently drop the merged changes."""
        path = self.path_for(issue_number)
        if os.path.isdir(path):
            if self._has_remote():
                self._sync_with_main(path, branch_name(issue_number))
            return path

        branch = branch_name(issue_number)
        has_remote = self._has_remote()
        if has_remote:
            self._git("fetch", "origin", check=False)

        remote_ref = f"origin/{branch}"
        if has_remote and self._ref_exists(remote_ref):
            # Branch already has commits on the remote (e.g. the API-driven
            # tester/coder writes from an earlier attempt) -- (re)point the
            # local branch at the fetched tip rather than trusting whatever
            # this clone last saw, then build the worktree from that.
            self._git("branch", "-f", branch, remote_ref)
            self._git("worktree", "add", path, branch)
            return path

        if self._ref_exists(branch):
            self._git("worktree", "add", path, branch)
        else:
            # Brand new branch: base it on the fetched remote default branch
            # when one exists, not this clone's possibly-stale local HEAD.
            base = "origin/main" if has_remote and self._ref_exists("origin/main") else "HEAD"
            self._git("worktree", "add", "-b", branch, path, base)
        return path


def write_worktree_file(cwd: str, path: str, content: str) -> None:
    """S22 split-brain fix: mirrors a `GitHubClient.commit_file` write into
    the local worktree, using the exact bytes just committed, so the very
    next `TestRunner.run(cwd, ...)` sees it. `GitHubClient` has no
    fetch/pull surface (it is a content API, not a git-plumbing seam), and a
    real fetch would only work against a live remote anyway -- not
    `MockGitHubClient`'s in-memory store. Since the caller already holds the
    committed content, writing it straight to `cwd` keeps the toolchain's
    view in sync with what the pipeline believes is committed without
    depending on git-remote semantics at all.

    `cwd == "."` is `CoderStage.run`/`TesterStage.run`'s own default when no
    real worktree was ever supplied -- true of most of the unit suite, which
    drives these stages with `MockTestRunner` and never reads the
    filesystem. Skip the write in that case rather than mirroring test
    content into whatever directory the process happens to be running
    from -- a real dispatch always passes `WorktreeManager.ensure(...)`'s
    absolute path, never `"."`.
    """
    if cwd == ".":
        return
    full_path = os.path.join(cwd, path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as fh:
        fh.write(content)


def read_worktree_file(cwd: str, path: str) -> str | None:
    """The read half of the S22 mirror: what the worktree currently holds at
    `path`, or `None` if nothing does. `cwd == "."` means no real worktree
    was ever supplied (the unit-suite default) -- reading the process's own
    directory would be a lie, so mirror `write_worktree_file`'s skip."""
    if cwd == ".":
        return None
    full_path = os.path.join(cwd, path)
    if not os.path.isfile(full_path):
        return None
    with open(full_path, "r", encoding="utf-8") as fh:
        return fh.read()


class InFlightRegistry:
    """Tracks each dispatched issue's claimed file list as reported at its
    checkpoint, so collision logic can ask "what files are claimed by
    in-flight issues other than mine". An issue's claim is cleared on
    teardown -- nothing outlives the issue it belongs to."""

    def __init__(self) -> None:
        self._claims: dict[int, list[str]] = {}

    def record(self, issue_number: int, files: list[str]) -> None:
        self._claims[issue_number] = list(files)

    def claimed_by_others(self, issue_number: int) -> dict[int, list[str]]:
        return {n: files for n, files in self._claims.items() if n != issue_number}

    def clear(self, issue_number: int) -> None:
        self._claims.pop(issue_number, None)


def _normalize_path(path: str) -> str:
    """Canonical repo-relative POSIX form, so the same file spelled two ways
    (`./x`, `x//y`, a leading `/`, `a/./b`) intersects correctly. Collision
    inputs come from three sources (container reports, the registry, PR file
    lists) with no shared format guarantee -- a mismatch here would silently
    yield "proceed" on a real collision."""
    return posixpath.normpath(path.replace("\\", "/")).lstrip("/")


def _normalize_paths(files) -> set[str]:
    return {_normalize_path(f) for f in files}


class Checkpoint:
    """Post-Design checkpoint: the container reports its exact file list and
    waits for a verdict before continuing.

    S10's real collision rule: hold if the reported files exactly intersect
    another in-flight issue's claim or an open, unmerged AORC PR's changed
    files (the collision set), OR if Graphify says one side's files sit in
    the other's import/call blast radius. A failed/uncertain Graphify query
    is conservative -- it holds, it never silently proceeds. When no
    `graphify` collaborator is configured, the check degrades to
    path-intersect only (there is nothing to query).
    """

    def __init__(
        self,
        github: GitHubClient | None = None,
        registry: InFlightRegistry | None = None,
        graphify: GraphifyClient | None = None,
    ) -> None:
        self.github = github
        self.registry = registry if registry is not None else InFlightRegistry()
        self.graphify = graphify

    def _collision_set(self, report: CheckpointReport) -> set[str]:
        """Every file claimed by someone other than this issue: other
        in-flight containers' registered claims, plus open unmerged AORC
        PRs' changed files (an open PR occupies its files just like a live
        container -- two issues could otherwise pass clean and collide at
        merge)."""
        others: set[str] = set()
        for files in self.registry.claimed_by_others(report.issue_number).values():
            others.update(_normalize_paths(files))
        if self.github is not None:
            my_branch = branch_name(report.issue_number)
            for pr in self.github.list_pull_requests(state="open"):
                if pr.head == my_branch:
                    continue
                others.update(_normalize_paths(pr.files))
        return others

    def _collides(self, my_files: set[str], others: set[str]) -> bool:
        if my_files & others:
            return True
        if not others:
            return False  # nothing else in flight -- nothing to collide with
        if self.graphify is None:
            return False  # no blast-radius signal available; path-intersect only
        forward = self.graphify.blast_radius(list(my_files))
        if not forward.ok:
            return True  # query failed/uncertain -> conservative hold
        if _normalize_paths(forward.files) & others:
            return True
        backward = self.graphify.blast_radius(list(others))
        if not backward.ok:
            return True
        return bool(_normalize_paths(backward.files) & my_files)

    def verdict(self, report: CheckpointReport) -> str:
        others = self._collision_set(report)
        collides = self._collides(_normalize_paths(report.files), others)
        self.registry.record(report.issue_number, report.files)
        return "hold" if collides else "proceed"


def cleanup_branch(github: GitHubClient, issue_number: int, outcome: str) -> None:
    """Three fixed cases, no agent judgment: merged -> delete the branch;
    agent-blocked / held -> keep it (resumable from the checkpoint)."""
    if outcome not in _CLEANUP_OUTCOMES:
        raise ValueError(f"unknown branch-cleanup outcome: {outcome!r}")
    if outcome == "merged":
        github.delete_branch(branch_name(issue_number))


class ContainerHarness:
    """Ties worktree creation, container dispatch, the checkpoint, and
    teardown+branch-cleanup together. One container per issue; nothing
    carries across issues."""

    def __init__(
        self,
        runtime: ContainerRuntime,
        worktrees: WorktreeManager,
        github: GitHubClient,
        checkpoint: Checkpoint | None = None,
        graphify: GraphifyClient | None = None,
        compute_guard: ComputeGuard | None = None,
    ) -> None:
        self._runtime = runtime
        self._worktrees = worktrees
        self._github = github
        self._checkpoint = (
            checkpoint if checkpoint is not None else Checkpoint(github=github, graphify=graphify)
        )
        self._compute_guard = compute_guard if compute_guard is not None else ComputeGuard()

    def dispatch(
        self, issue_number: int, env: dict[str, str] | None = None
    ) -> ContainerHandle:
        """`env` is the container's credential surface, built by
        `credentials.CredentialBroker.container_env` (S15) -- per-issue
        GitHub token + LLM key only, never the App private key. Every
        incoming env is re-checked here (S16): a hand-built dict carrying
        anything key-shaped raises `CredentialLeakError` before the runtime
        ever sees it, so the broker is structurally the only viable source."""
        if env is not None:
            assert_env_clean(env)
        branch = branch_name(issue_number)
        path = self._worktrees.ensure(issue_number)
        return self._runtime.start(issue_number, branch, path, env)

    def checkpoint(self, report: CheckpointReport) -> str:
        return self._checkpoint.verdict(report)

    def adopt_registry(self, registry: InFlightRegistry) -> None:
        """S43: swap in a registry rebuilt from GitHub
        (`design.rebuild_in_flight_registry`) at the start of a wake/backfill
        -- the in-process one dies with the orchestrator, so collision
        detection across a restart depends on this."""
        self._checkpoint.registry = registry

    def teardown(self, handle: ContainerHandle, outcome: str) -> None:
        self._runtime.teardown(handle)
        self._checkpoint.registry.clear(handle.issue_number)
        cleanup_branch(self._github, handle.issue_number, outcome)

    def enforce_wall_clock(self, handle: ContainerHandle, elapsed_minutes: float) -> ComputeVerdict:
        """S13 -- container compute limit. `elapsed_minutes` is caller-
        supplied (see `ComputeGuard`). On a trip: label `agent-blocked`
        (reason: wall-clock timeout), ping, and tear down through the same
        teardown path a normal `agent-blocked` outcome uses -- branch kept,
        resumable from the last committed artifact."""
        verdict = self._compute_guard.check(elapsed_minutes)
        if verdict.action == "kill":
            self._github.add_label(handle.issue_number, BLOCKED_LABEL)
            self._github.set_board_column(handle.issue_number, LABEL_COLUMN[BLOCKED_LABEL])
            self._github.post_comment(
                handle.issue_number,
                f"{TIMEOUT_PING_MARKER}\nStopping: container wall-clock limit "
                f"reached after {elapsed_minutes:.0f} min. Labeling "
                f"`{BLOCKED_LABEL}` (reason: wall-clock timeout).",
            )
            self.teardown(handle, outcome="agent-blocked")
        return verdict
