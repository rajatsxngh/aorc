"""S21 -- Composition root / entry point: `python -m aorc <subcommand>`.

This module is the **one and only** place real SDK-backed adapters are
constructed -- `SdkGitHubClient`, a real `LLMClient` (via `build_llm_client`),
`DockerContainerRuntime` -- so that orchestrator core (everything under
`src/aorc/` except `*_adapter.py` files) keeps depending only on the
`GitHubClient`/`LLMClient` interfaces (architecture invariant #1, pinned by
`tests/test_no_sdk_imports.py`).

Every collaborator `compose()` builds is overridable by keyword, so tests
drive each subcommand end-to-end against in-memory mocks
(`MockGitHubClient`/`MockLLMClient`/`MockContainerRuntime`) with zero
third-party deps -- no env vars, no real config file, no network.

Fails closed: an absent/malformed `.aorc.yml` (`ConfigError`, raised by
`config.load_config`) or a missing required environment variable
(`StartupError`) prints a clear message and exits non-zero before anything
is constructed -- never a partial startup on guessed defaults (invariant #2:
no hardcoded model names or secrets; both come from `.aorc.yml` and the env
slots it references).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from .coder import CoderStage
from .config import AorcConfig, ConfigError, load_config
from .credentials import CredentialBroker
from .design import DesignStage
from .driver import PipelineDriver
from .escalation import BackoffLLMClient
from .gitops import LocalGitOps
from .harness import ContainerRuntime, WorktreeManager
from .install import ConfigGatedWakeLoop, InstallHandler
from .interfaces import GitHubClient, LLMClient
from .llm import build_llm_client
from .reviewer import ReviewerStage
from .tester import SubprocessTestRunner, TesterStage

DEFAULT_CONFIG_PATH = ".aorc.yml"
DEFAULT_WORKTREES_DIR = ".aorc-worktrees"

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
REPO_ENV = "AORC_REPO"
BASE_IMAGE_ENV = "AORC_BASE_IMAGE"


class StartupError(Exception):
    """A required environment variable is absent. Raised instead of
    proceeding on a guessed default -- caught at the CLI boundary into a
    clean, non-zero exit with no partial startup."""


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise StartupError(f"required environment variable ${name} is not set")
    return value


def pat_passthrough_minter(token: str):
    """The S23 stand-in for the real App-JWT -> installation-token exchange
    (issues/23-real-token-minter.md): until that exchange exists, every mint
    request returns this one fixed PAT regardless of repo/permissions asked
    for. This function is the single, clearly-marked injection point --
    swapping in the real S23 minter is a one-line change at its one call
    site in `compose()` below."""

    def minter(private_key: str, repo: str, permissions: dict) -> str:
        return token

    return minter


@dataclass
class Collaborators:
    """Everything a subcommand needs, already composed. `github` is the
    loop's own (scrub-wrapped) client -- never an unwrapped reference."""

    llm: LLMClient | None
    loop: ConfigGatedWakeLoop
    installer: InstallHandler
    driver: PipelineDriver | None = None

    @property
    def github(self) -> GitHubClient:
        return self.loop.github


def compose(
    config: AorcConfig,
    repo: str,
    *,
    github: GitHubClient | None = None,
    runtime: ContainerRuntime | None = None,
    worktrees: WorktreeManager | None = None,
    broker: CredentialBroker | None = None,
    llm: LLMClient | None = None,
    driver: PipelineDriver | None = None,
    repo_dir: str = ".",
    worktrees_dir: str = DEFAULT_WORKTREES_DIR,
    base_image: str | None = None,
) -> Collaborators:
    """Build the real collaborators and hand back the composed
    `ConfigGatedWakeLoop` + `InstallHandler`. Every parameter is overridable
    so a caller (tests, `--dev` escape hatches) can substitute mocks or
    alternate adapters without touching this function's body -- the real
    adapters are only constructed for the parameters left `None`.

    Wraps the real `GitHubClient` in `ScrubbingGitHubClient` exactly once,
    via `ConfigGatedWakeLoop.compose` (the S16 composition-root discipline):
    no code path downstream ever holds an unwrapped reference.
    """
    if github is None:
        from .github.sdk_adapter import SdkGitHubClient

        github = SdkGitHubClient(_require_env(GITHUB_TOKEN_ENV), repo)
    if llm is None:
        # S14 wiring: every real triage/clarification/decomposition call the
        # loop makes goes through the backoff decorator, so a transient
        # provider error is retried on schedule instead of counting as a
        # real failure. Caller-supplied `llm` overrides (tests) are used
        # as-is -- backoff is a real-adapter concern only.
        llm = BackoffLLMClient(build_llm_client(config.primary))
    if runtime is None:
        from .harness import DockerContainerRuntime

        image = base_image or _require_env(BASE_IMAGE_ENV)
        runtime = DockerContainerRuntime(image)
    if worktrees is None:
        worktrees = WorktreeManager(repo_dir, worktrees_dir)
    if broker is None:
        # Interim S23 stand-in (see `pat_passthrough_minter`): no App private
        # key exists yet, so the broker holds an empty one -- it is never
        # consulted by the passthrough minter, and `container_env`'s leak
        # check no-ops on an empty/falsy key by construction.
        token = _require_env(GITHUB_TOKEN_ENV)
        broker = CredentialBroker(
            private_key="",
            minter=pat_passthrough_minter(token),
            llm_api_key=config.primary.api_key,
        )
    loop = ConfigGatedWakeLoop.compose(
        github,
        runtime,
        worktrees,
        broker,
        repo,
        llm=llm,
        concurrency=config.dispatch_concurrency,
    )
    installer = InstallHandler(loop)
    if driver is None and config.setup and config.test:
        # S22: only buildable once `.aorc.yml`'s required build fields are
        # known (guarded the same way `config.build_blockers` gates the
        # rest of the build pipeline) -- `compose()` also runs for
        # `install`, before any real `.aorc.yml` may exist yet, when this
        # simply stays `None`. `ConfigGatedWakeLoop.dispatch_issue` already
        # never runs while the gate is closed, so a `None` driver here never
        # actually gets called with a real setup/test command missing.
        critic_llm = (
            BackoffLLMClient(build_llm_client(config.escalation))
            if config.escalation is not None
            else llm
        )
        test_runner = SubprocessTestRunner()
        coder_stage = CoderStage(
            llm,
            loop.github,
            test_runner,
            setup_command=config.setup,
            test_command=config.test,
            lint_command=config.lint,
        )
        driver = PipelineDriver(
            loop.github,
            worktrees,
            DesignStage(llm, loop.github),
            TesterStage(llm, critic_llm, loop.github, test_runner, test_command=config.test),
            coder_stage,
            ReviewerStage(
                critic_llm,
                coder_stage,
                loop.github,
                test_runner,
                coverage_command=config.coverage_command,
                coverage_floor=config.coverage_floor,
                # No `smoke_command` template exists in `.aorc.yml`'s schema
                # today (only the `smoke:` examples list) -- the smoke gate
                # stays skipped live until that config field exists, exactly
                # as documented in `install.py`'s config-PR template.
                smoke_examples=config.smoke,
                gitops=LocalGitOps(repo_dir),
            ),
        )
    loop.driver = driver
    return Collaborators(llm=llm, loop=loop, installer=installer, driver=driver)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aorc")
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help="path to .aorc.yml (default: %(default)s)"
    )
    parser.add_argument(
        "--repo", default=None, help=f"owner/repo; defaults to ${REPO_ENV}"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="run the S18 install flow (board, labels, config PR, backfill)")
    sub.add_parser("backfill", help="re-sync: triage every open issue not already in the flow")
    sub.add_parser("wake", help="one cron tick: held-queue sweep + token-expiry pass")
    run_issue = sub.add_parser("run-issue", help="dispatch a single actionable issue")
    run_issue.add_argument("issue_number", type=int)
    return parser


def run(argv: list[str] | None = None, *, collaborators: Collaborators | None = None) -> int:
    """Parse args, compose collaborators (unless supplied, e.g. by a test),
    and execute one subcommand. Returns the process exit code -- never
    raises for a startup/config failure, so `main()` can `sys.exit` it
    directly."""
    args = build_argparser().parse_args(argv)

    if collaborators is None:
        try:
            config = load_config(args.config)
            repo = args.repo or _require_env(REPO_ENV)
            collaborators = compose(config, repo)
        except (ConfigError, StartupError) as exc:
            print(f"aorc: {exc}", file=sys.stderr)
            return 1

    loop = collaborators.loop
    if args.command == "install":
        report = collaborators.installer.on_install()
        print(f"install: config PR #{report.config_pr}, board {report.board_columns}")
    elif args.command == "backfill":
        report = loop.backfill()
        print(
            f"backfill: dispatched={report.dispatched} "
            f"held={report.held} queued={report.queued}"
        )
    elif args.command == "wake":
        report = loop.cron_tick()
        print(f"wake: released={report.released} requeued={report.requeued}")
    elif args.command == "run-issue":
        loop.dispatch_issue(args.issue_number)
        print(f"dispatched issue #{args.issue_number}")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
