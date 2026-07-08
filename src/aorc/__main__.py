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
import functools
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import webhook
from .coder import CoderStage
from .config import AorcConfig, ConfigError, load_config
from .credentials import CredentialBroker
from .design import DesignStage
from .driver import PipelineDriver
from .escalation import BackoffLLMClient
from .gitops import LocalGitOps
from .harness import ContainerRuntime, WorktreeManager
from .install import ConfigGatedWakeLoop, InstallHandler, route_webhook
from .interfaces import GitHubClient, LLMClient
from .llm import build_llm_client
from .merge import MergeTimeHandler
from .reviewer import ReviewerStage
from .tester import ContainerTestRunner, SubprocessTestRunner, TestRunner, TesterStage

DEFAULT_CONFIG_PATH = ".aorc.yml"
DEFAULT_WORKTREES_DIR = ".aorc-worktrees"
DEFAULT_SERVE_HOST = "0.0.0.0"
DEFAULT_SERVE_PORT = 8080

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
REPO_ENV = "AORC_REPO"
BASE_IMAGE_ENV = "AORC_BASE_IMAGE"
APP_ID_ENV = "AORC_GITHUB_APP_ID"
APP_PRIVATE_KEY_PATH_ENV = "AORC_GITHUB_APP_PRIVATE_KEY_PATH"
WEBHOOK_SECRET_ENV = "AORC_WEBHOOK_SECRET"

# S25: the orchestrator-side token `ActionsContainerRuntime` authenticates its
# own workflow_dispatch/cancel calls with -- wider than
# `credentials.MINIMAL_PERMISSIONS` (that ceiling bounds per-issue *container*
# tokens only) and minted once here, never per issue.
ACTIONS_RUNTIME_PERMISSIONS = {"actions": "write"}


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
    """S23's `--dev-pat-minter` escape hatch: every mint request returns this
    one fixed PAT regardless of repo/permissions asked for, bypassing the
    real App-JWT exchange entirely. Demoted from S21's *default* stand-in to
    an explicit opt-in now that `build_app_token_minter` (app_token.py) is
    the real thing -- useful for a dev loop with no GitHub App registered at
    all, never for a live run."""

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
    merge_handler: MergeTimeHandler | None = None
    # S27: the `TestRunner` wired into the build pipeline's stages (`None`
    # under the same setup/test gate `driver` is `None` under) -- surfaced
    # here so callers/tests can confirm which one composition chose without
    # reaching into a stage's private attribute.
    test_runner: TestRunner | None = None

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
    merge_handler: MergeTimeHandler | None = None,
    repo_dir: str = ".",
    worktrees_dir: str = DEFAULT_WORKTREES_DIR,
    base_image: str | None = None,
    dev_pat_minter: bool = False,
    no_container: bool = False,
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
        if config.container_runtime == "actions":
            from .github.actions_runtime import ActionsContainerRuntime

            if dev_pat_minter:
                # Same dev escape hatch as the broker's `--dev-pat-minter`:
                # no App registered, so the orchestrator's own PAT
                # authenticates the dispatch/cancel calls too. Never use live.
                actions_token = _require_env(GITHUB_TOKEN_ENV)
            else:
                from .github.app_token import build_app_token_minter

                app_id = _require_env(APP_ID_ENV)
                key_path = _require_env(APP_PRIVATE_KEY_PATH_ENV)
                private_key = Path(key_path).read_text()
                actions_token = build_app_token_minter(app_id)(
                    private_key, repo, ACTIONS_RUNTIME_PERMISSIONS
                )
            runtime = ActionsContainerRuntime(repo, config.container_workflow_file, actions_token)
        else:
            from .harness import DockerContainerRuntime

            image = base_image or _require_env(BASE_IMAGE_ENV)
            runtime = DockerContainerRuntime(image)
    if worktrees is None:
        worktrees = WorktreeManager(repo_dir, worktrees_dir)
    if broker is None:
        if dev_pat_minter:
            # `--dev-pat-minter` escape hatch: no App private key exists, so
            # the broker holds an empty one -- it is never consulted by the
            # passthrough minter, and `container_env`'s leak check no-ops on
            # an empty/falsy key by construction.
            token = _require_env(GITHUB_TOKEN_ENV)
            broker = CredentialBroker(
                private_key="",
                minter=pat_passthrough_minter(token),
                llm_api_key=config.primary.api_key,
            )
        else:
            # S23: the real App-JWT -> installation-token exchange. The key
            # *material* is read here, at the composition root, and handed
            # only to `CredentialBroker` -- nothing downstream ever sees the
            # path or the key itself (invariant #2).
            from .github.app_token import build_app_token_minter

            app_id = _require_env(APP_ID_ENV)
            key_path = _require_env(APP_PRIVATE_KEY_PATH_ENV)
            private_key = Path(key_path).read_text()
            broker = CredentialBroker(
                private_key=private_key,
                minter=build_app_token_minter(app_id),
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
    # Reused below for MergeTimeHandler's stale-PR re-review (S17); stay None
    # when the build pipeline itself isn't configured yet (same gate as the
    # driver -- there can be no AORC-opened PRs to re-review without one).
    coder_stage = None
    reviewer_stage = None
    test_runner = None
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
        # S27: the toolchain (setup/test/lint/coverage/smoke) runs inside the
        # issue's own Docker container via `ContainerTestRunner` -- the real
        # isolation boundary the harness's container was always meant to be
        # -- unless the config selects `container.runtime: actions` (that
        # runtime has no local container for `docker exec` to target; S25's
        # driver-stays-orchestrator-side scope applies there unchanged) or
        # the caller passes `--no-container`, the explicit dev escape hatch
        # back to the host subprocess.
        test_runner = (
            SubprocessTestRunner()
            if no_container or config.container_runtime == "actions"
            else ContainerTestRunner()
        )
        coder_stage = CoderStage(
            llm,
            loop.github,
            test_runner,
            setup_command=config.setup,
            test_command=config.test,
            lint_command=config.lint,
        )
        reviewer_stage = ReviewerStage(
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
        )
        driver = PipelineDriver(
            loop.github,
            worktrees,
            DesignStage(llm, loop.github),
            TesterStage(llm, critic_llm, loop.github, test_runner, test_command=config.test),
            coder_stage,
            reviewer_stage,
        )
    loop.driver = driver
    if merge_handler is None:
        # S24 wiring: the webhook receiver's `route_webhook` call needs a
        # real `MergeTimeHandler` to route pr-merged/pr-comment/rollback
        # deliveries into. Built here (not by S17/S18, which only defined
        # the class) since this is the first place a live MergeTimeHandler
        # is actually composed.
        merge_handler = MergeTimeHandler(
            loop,
            LocalGitOps(repo_dir),
            coder=coder_stage,
            reviewer=reviewer_stage,
            test_runner=test_runner,
            test_command=config.test if test_runner is not None else None,
            feedback_llm=llm,
            worktrees=worktrees,
        )
    return Collaborators(
        llm=llm,
        loop=loop,
        installer=installer,
        driver=driver,
        merge_handler=merge_handler,
        test_runner=test_runner,
    )


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
    parser.add_argument(
        "--dev-pat-minter",
        action="store_true",
        help=(
            "dev escape hatch: mint every per-issue container token as the fixed "
            f"${GITHUB_TOKEN_ENV} PAT instead of the real GitHub App exchange "
            f"(no ${APP_ID_ENV}/${APP_PRIVATE_KEY_PATH_ENV} needed). Never use live."
        ),
    )
    parser.add_argument(
        "--no-container",
        action="store_true",
        help=(
            "dev escape hatch: run the setup/test/lint toolchain on the host via "
            "SubprocessTestRunner instead of `docker exec`-ing into the issue's "
            "container. Never use live -- untrusted, LLM-generated commands then "
            "run with the orchestrator's own user/filesystem/network."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="run the S18 install flow (board, labels, config PR, backfill)")
    sub.add_parser("backfill", help="re-sync: triage every open issue not already in the flow")
    sub.add_parser("wake", help="one cron tick: held-queue sweep + token-expiry pass")
    run_issue = sub.add_parser("run-issue", help="dispatch a single actionable issue")
    run_issue.add_argument("issue_number", type=int)
    serve = sub.add_parser(
        "serve",
        help=(
            "run the webhook receiver: HMAC-verified GitHub deliveries routed through "
            f"route_webhook (needs ${WEBHOOK_SECRET_ENV})"
        ),
    )
    serve.add_argument("--host", default=DEFAULT_SERVE_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT)
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
            collaborators = compose(
                config, repo, dev_pat_minter=args.dev_pat_minter, no_container=args.no_container
            )
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
        # `ConfigGatedWakeLoop.dispatch_issue` returns None when the config
        # gate is closed: the issue was parked under awaiting-config and no
        # container was started, so don't claim otherwise.
        outcome = loop.dispatch_issue(args.issue_number)
        if outcome is not None:
            print(f"dispatched issue #{args.issue_number}")
            # S31: surface the pipeline's outcome -- before this, the
            # DriverResult was discarded and a blocked stage looked identical
            # to a clean run.
            if outcome.result is not None:
                result = outcome.result
                print(f"pipeline: stage={result.stage} status={result.status}")
                if result.reason:
                    print(f"reason:\n{result.reason}")
        else:
            print(f"parked issue #{args.issue_number} (awaiting-config)")
    elif args.command == "serve":
        # Read once, here, at the composition root -- never logged, never
        # threaded further than this local variable (invariant #2).
        try:
            secret = _require_env(WEBHOOK_SECRET_ENV)
        except StartupError as exc:
            print(f"aorc: {exc}", file=sys.stderr)
            return 1
        route = functools.partial(
            route_webhook,
            handler=collaborators.merge_handler,
            loop=loop,
            installer=collaborators.installer,
        )
        server = webhook.serve(secret, route, host=args.host, port=args.port)
        print(f"serve: listening on {args.host}:{args.port}")
        server.serve_forever()
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
