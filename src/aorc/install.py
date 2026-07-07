"""S18 -- GitHub App install: the surface that ties AORC to a repo.

Four pieces, all orchestrator-side:

- **App manifest.** `APP_PERMISSIONS` / `APP_WEBHOOK_EVENTS` / `app_manifest`
  declare the fine-grained permission set and webhook subscriptions in
  GitHub's App Manifest flow format -- the registration payload the real App
  is created from. (Actually registering the App and receiving HTTP webhook
  deliveries is an external GitHub surface; `route_webhook` below is the
  deterministic payload -> entry-point mapping that receiver calls.)

- **Install-time behavior** (`InstallHandler.on_install`): create the
  Projects board with the six standard columns, create the pipeline labels,
  open the `.aorc.yml` config PR (template + the push-to-main auto-rollback
  workflow), and run the first-run backfill sweep immediately so the backlog
  starts organizing itself (PRD B23). Idempotent: a duplicate installation
  webhook finds the config PR already open and re-runs only the (naturally
  idempotent) re-sync.

- **The awaiting-config gate** (`ConfigGate` + `ConfigGatedWakeLoop`): any
  issue reaching dispatch before `.aorc.yml` is merged to main is parked
  under `awaiting-config` (Blocked column) instead of dispatched -- the
  pipeline never builds against baked-in defaults, the exact
  toolchain-hallucination the config file exists to prevent. The gate fails
  closed on a malformed or partial config with a clear reason (missing
  `setup`/`test` blocks the build pipeline even after the merge). Once the
  gate opens, the next wake releases the queue through the normal dispatch
  selector.

- **Webhook routing** (`route_webhook`): maps delivered events to the S17
  merge-time handlers -- `MergeTimeHandler.on_pr_merged` (never the bare
  `WakeLoop.on_pr_merged` sweep), `.on_pr_comment`, and `.on_main_broken`
  via the `aorc-main-broken` repository_dispatch that the generated rollback
  workflow fires after reverting a red main.

A comment answering a clarification question arrives as a plain issue
comment; routing it back into S11's clarification stage is not wired here --
the comment triggers a plain wake and the issue re-enters via backfill once
its terminal label is cleared (v1 behavior, noted for honesty).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import yaml

from .clarification import ClarificationStage
from .config import AorcConfig, ConfigError, build_blockers, parse_config
from .decomposition import DecompositionStage
from .dispatch import DEFAULT_CONCURRENCY, select_dispatch
from .harness import ContainerHandle, ContainerHarness
from .interfaces import GitHubClient, Issue, LLMClient
from .pipeline import AWAITING_CONFIG_LABEL, HELD_LABEL, LABEL_COLUMN
from .wake import WakeLoop, WakeReport

if TYPE_CHECKING:  # only for annotations; merge.py is a heavier import
    from .credentials import CredentialBroker
    from .merge import MergeTimeHandler

# --------------------------------------------------------------------------- #
# App manifest: fine-grained permissions + webhook registration
# --------------------------------------------------------------------------- #

APP_NAME = "aorc"

# Exactly the PRD's fine-grained set -- and a superset of the per-issue token
# ceiling (credentials.MINIMAL_PERMISSIONS), which must be mintable under it.
APP_PERMISSIONS = {
    "issues": "write",
    "pull_requests": "write",
    "contents": "write",
    "projects": "write",
    "actions": "write",
}

# Issue events (opened/edited/labeled/comment), PR events (merged/opened/
# review-comment), push-to-main (auto-rollback CI), and repository_dispatch:
# the channel the rollback workflow uses to report the offending PR back.
APP_WEBHOOK_EVENTS = (
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review_comment",
    "push",
    "repository_dispatch",
)


def app_manifest(webhook_url: str, *, name: str = APP_NAME) -> dict:
    """The GitHub App Manifest flow payload the real App is registered from."""
    return {
        "name": name,
        "url": "https://github.com/apps/" + name,
        "public": False,
        "hook_attributes": {"url": webhook_url},
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": list(APP_WEBHOOK_EVENTS),
    }


# --------------------------------------------------------------------------- #
# Install artifacts: board columns, labels, config template, rollback workflow
# --------------------------------------------------------------------------- #

STANDARD_COLUMNS = (
    "Backlog",
    "Needs Clarification",
    "In Progress",
    "Blocked",
    "In Review",
    "Done",
)

# Every label the pipeline can ever set, created up front so a label write
# never 404s. Keys must cover pipeline.PIPELINE_LABELS + the two queue labels.
INSTALL_LABELS: dict[str, tuple[str, str]] = {
    "in-design": ("1d76db", "AORC: design stage in progress"),
    "in-test": ("1d76db", "AORC: test-authoring stage in progress"),
    "in-code": ("1d76db", "AORC: implementation stage in progress"),
    "in-review": ("0e8a16", "AORC: review stage / PR open"),
    "needs-clarification": ("d93f0b", "AORC: waiting on a human answer"),
    "agent-blocked": ("b60205", "AORC: needs human intervention"),
    HELD_LABEL: ("c5def5", "AORC: dispatch-queue hold, auto-released"),
    AWAITING_CONFIG_LABEL: ("fbca04", "AORC: waiting for the .aorc.yml PR to merge"),
}

CONFIG_PATH = ".aorc.yml"
CONFIG_PR_BRANCH = "aorc/config"
WORKFLOW_PATH = ".github/workflows/aorc-rollback.yml"

# No real model names here (architecture invariant #2): the template ships
# placeholders the repo owner must fill in before merging.
CONFIG_TEMPLATE = """\
# AORC configuration -- TEMPLATE. Every value below is an example: edit it
# for this repo before merging. The build pipeline will NOT run until this
# file is merged to main (issues pile up under "awaiting-config" until then).
llm:
  primary:    { provider: claude, model: <primary-model>, api_key: $ANTHROPIC_KEY }
  escalation: { provider: claude, model: <escalation-model>, api_key: $ANTHROPIC_KEY }

# Required -- the build pipeline refuses to run without these two:
setup: <command that installs this repo, e.g. pip install -e .>
test: <command that runs the test suite, e.g. pytest -q>

# Strongly recommended: whole-app smoke examples. Removing this block
# permanently disqualifies the repo from auto-merge (PRs still open).
smoke:
  - { input: <example-input>, expect: <expected-output> }

merge:
  auto: false # never enable before the graduation gate is met
"""

INSTALL_PR_BODY = """\
## Configure AORC

This PR adds `.aorc.yml` (AORC's per-repo configuration) and the
push-to-main auto-rollback workflow.

**The values shown are a template, not working defaults.** Edit `setup`,
`test`, and the `llm` block for this repo, then merge. The build pipeline
will not run until this file is merged to main -- AORC never guesses a
toolchain, so until then, triaged issues are held under `awaiting-config`.

**One-time smoke warning:** if you remove the `smoke:` block, the pipeline
still runs and opens PRs (the reviewer's smoke gate is skipped), but the
repo is permanently disqualified from auto-merge until smoke examples
exist -- AORC never unattended-merges an app it cannot whole-app-verify.
"""

# The S17 wiring obligation: the Actions workflow that detects a red main
# after a merge, reverts the offending commit, and reports the PR number back
# to the orchestrator via repository_dispatch (routed to
# MergeTimeHandler.on_main_broken by `route_webhook`). Test commands come
# from `.aorc.yml` at run time -- never baked-in defaults.
ROLLBACK_WORKFLOW = """\
name: aorc-rollback
"on":
  push:
    branches: [main]
jobs:
  verify-main:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Run setup + tests from .aorc.yml
        run: |
          SETUP="$(python3 -c 'import yaml; print(yaml.safe_load(open(".aorc.yml")).get("setup") or "true")')"
          TEST="$(python3 -c 'import yaml; print(yaml.safe_load(open(".aorc.yml")).get("test") or "true")')"
          bash -ec "$SETUP"
          bash -ec "$TEST"
      - name: Revert the offending merge and notify the orchestrator
        if: failure()
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          git config user.name "aorc[bot]"
          git config user.email "aorc-bot@users.noreply.github.com"
          git revert --no-edit -m 1 HEAD || git revert --no-edit HEAD
          git push origin main
          PR="$(git log --format=%s -n 1 HEAD~1 | grep -oE '#[0-9]+' | head -n 1 | tr -d '#')"
          if [ -n "$PR" ]; then
            gh api "repos/${{ github.repository }}/dispatches" \\
              -f event_type=aorc-main-broken \\
              -F "client_payload[pr_number]=$PR"
          fi
"""


# --------------------------------------------------------------------------- #
# ConfigGate: fail-closed `.aorc.yml` validation (PRD B23)
# --------------------------------------------------------------------------- #


@dataclass
class ConfigStatus:
    ready: bool
    reason: str | None = None
    config: AorcConfig | None = None


class ConfigGate:
    """Reads `.aorc.yml` from main through the GitHubClient seam and decides
    whether the build pipeline may run. Fails closed: absent, malformed, or
    missing-required-fields all block with a clear reason -- never guessed
    defaults. Re-checked fresh on every call (stateless, like every wake)."""

    def __init__(self, github: GitHubClient, *, ref: str = "main") -> None:
        self._github = github
        self._ref = ref

    def check(self) -> ConfigStatus:
        content = self._github.get_file(CONFIG_PATH, self._ref)
        if content is None:
            return ConfigStatus(
                False,
                f"{CONFIG_PATH} is not on {self._ref} yet -- the install config "
                "PR must merge before anything builds",
            )
        try:
            raw = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return ConfigStatus(
                False,
                f"{CONFIG_PATH} is not valid YAML (failing closed, no guessed "
                f"defaults): {exc}",
            )
        if not isinstance(raw, dict):
            return ConfigStatus(
                False, f"{CONFIG_PATH} must be a YAML mapping (failing closed)"
            )
        try:
            config = parse_config(raw)
        except ConfigError as exc:
            return ConfigStatus(
                False,
                f"{CONFIG_PATH} is invalid (failing closed, no guessed "
                f"defaults): {exc}",
            )
        blockers = build_blockers(config)
        if blockers:
            return ConfigStatus(
                False,
                f"{CONFIG_PATH} blocks the build pipeline: " + "; ".join(blockers),
                config,
            )
        return ConfigStatus(True, None, config)


# --------------------------------------------------------------------------- #
# The config-gated wake loop
# --------------------------------------------------------------------------- #

AWAITING_CONFIG_MARKER = "<!-- aorc:awaiting-config -->"


class ConfigGatedWakeLoop(WakeLoop):
    """A `WakeLoop` whose only dispatch path is gated on `ConfigGate`: until
    `.aorc.yml` is merged (and build-valid), every issue reaching dispatch is
    parked under `awaiting-config` instead of containerized. Once the gate
    opens, the next wake releases the queue through the normal selector."""

    def __init__(
        self,
        github: GitHubClient,
        harness: ContainerHarness,
        broker: "CredentialBroker",
        repo: str,
        *,
        llm: LLMClient | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            github, harness, broker, repo, llm=llm, concurrency=concurrency, clock=clock
        )
        self._gate = ConfigGate(self.github)
        # Triage/clarify/decompose all run at install time (orchestrator-side,
        # no build container, so the config gate does not apply to them).
        self._clarification = ClarificationStage(llm, self.github) if llm else None
        self._decomposition = DecompositionStage(llm, self.github) if llm else None

    # ---- gating ------------------------------------------------------------ #

    def dispatch_issue(self, issue_number: int) -> ContainerHandle | None:
        status = self._gate.check()
        if status.ready:
            return super().dispatch_issue(issue_number)
        self._hold_awaiting_config(issue_number, status.reason)
        return None

    def _hold_awaiting_config(self, issue_number: int, reason: str | None) -> None:
        self.github.add_label(issue_number, AWAITING_CONFIG_LABEL)
        self.github.set_board_column(issue_number, LABEL_COLUMN[AWAITING_CONFIG_LABEL])
        comments = self.github.list_comments(issue_number)
        if not any(AWAITING_CONFIG_MARKER in c.body for c in comments):
            self.github.post_comment(
                issue_number, f"{AWAITING_CONFIG_MARKER}\nHeld before dispatch: {reason}"
            )

    def _already_in_flow(self, issue: Issue) -> bool:
        # awaiting-config issues are already triaged and queued: the backfill
        # re-sync must not feed them through triage again.
        return AWAITING_CONFIG_LABEL in issue.labels or super()._already_in_flow(issue)

    def _route_not_ready(self, issue: Issue, result) -> None:
        """Backfill routes a not-ready verdict onward immediately (PRD
        install-time behavior): an epic goes to decomposition (S12), anything
        else to the grill-me clarification stage (S11). Both are idempotent
        on re-runs -- clarified issues carry `needs-clarification` (skipped
        as in-flow), and decomposition dedupes sub-issues by marker."""
        if self._decomposition is None:
            return
        if result.reason == "epic":
            self._decomposition.decompose(issue)
        else:
            self._clarification.start(issue)

    def _sweep_held(self, held: list[Issue]) -> list[int]:
        # While nothing can build, the held queue stays parked in GitHub --
        # sweeping it would only churn labels into awaiting-config.
        if not self._gate.check().ready:
            return []
        return super()._sweep_held(held)

    # ---- release ------------------------------------------------------------ #

    def wake(self, *, now: float | None = None) -> WakeReport:
        report = super().wake(now=now)
        report.released.extend(self._release_awaiting())
        return report

    def _release_awaiting(self) -> list[int]:
        """Once the gate opens, feed the awaiting-config queue through the
        normal dispatch selector (capacity + collision + declared-blocker
        checks all still apply); declared-blocked ones move to the held
        queue the ordinary sweep owns."""
        if not self._gate.check().ready:
            return []
        waiting = [
            issue
            for issue in self.github.list_issues("open")
            if AWAITING_CONFIG_LABEL in issue.labels
            and issue.number not in self.in_flight
        ]
        if not waiting:
            return []
        decision = select_dispatch(
            waiting,
            self.github,
            in_flight=len(self.in_flight),
            concurrency=self._concurrency,
        )
        for number in decision.to_dispatch:
            self.github.remove_label(number, AWAITING_CONFIG_LABEL)
            super().dispatch_issue(number)
        for number in decision.blocked:
            self.github.remove_label(number, AWAITING_CONFIG_LABEL)
            self.hold(number)
        return decision.to_dispatch


# --------------------------------------------------------------------------- #
# InstallHandler: on-install behavior (board, labels, config PR, backfill)
# --------------------------------------------------------------------------- #


@dataclass
class InstallReport:
    config_pr: int
    board_columns: list[str]
    backfill: Any  # wake.BackfillReport
    held_awaiting_config: list[int]


class InstallHandler:
    """The installation webhook's target. All writes go through the loop's
    (already scrub-wrapped) client -- same composition-root discipline as
    S16/S17."""

    def __init__(self, loop: ConfigGatedWakeLoop) -> None:
        self._loop = loop

    @property
    def github(self) -> GitHubClient:
        return self._loop.github

    def on_install(self) -> InstallReport:
        github = self.github
        github.create_board(list(STANDARD_COLUMNS))
        for name, (color, description) in INSTALL_LABELS.items():
            github.create_label(name, color, description)
        config_pr = self._ensure_config_pr()
        backfill = self._loop.backfill()
        held = sorted(
            issue.number
            for issue in github.list_issues("open")
            if AWAITING_CONFIG_LABEL in issue.labels
        )
        return InstallReport(
            config_pr=config_pr,
            board_columns=list(STANDARD_COLUMNS),
            backfill=backfill,
            held_awaiting_config=held,
        )

    def _ensure_config_pr(self) -> int:
        """Open the config PR exactly once; a duplicate installation webhook
        finds it already open (or the config already merged) and no-ops."""
        github = self.github
        for pr in github.list_pull_requests("open"):
            if pr.head == CONFIG_PR_BRANCH:
                return pr.number
        if github.get_file(CONFIG_PATH, "main") is not None:
            return 0  # config already merged: nothing to open
        github.commit_file(
            CONFIG_PR_BRANCH,
            CONFIG_PATH,
            CONFIG_TEMPLATE,
            "aorc: add .aorc.yml configuration template",
        )
        github.commit_file(
            CONFIG_PR_BRANCH,
            WORKFLOW_PATH,
            ROLLBACK_WORKFLOW,
            "aorc: add push-to-main auto-rollback workflow",
        )
        pr = github.open_pull_request(
            "Configure AORC (.aorc.yml)", INSTALL_PR_BODY, CONFIG_PR_BRANCH
        )
        return pr.number


# --------------------------------------------------------------------------- #
# Webhook routing: delivered payloads -> the right entry point
# --------------------------------------------------------------------------- #

_ISSUE_ACTIONS = {"opened", "edited", "labeled", "reopened"}


def route_webhook(
    event: str,
    payload: dict,
    *,
    handler: "MergeTimeHandler",
    loop: WakeLoop,
    installer: InstallHandler | None = None,
) -> str | None:
    """Map one webhook delivery to its entry point; returns what was routed
    (None = event AORC doesn't act on). PR merges go to the *full* S17
    handler, `MergeTimeHandler.on_pr_merged` -- never the bare
    `WakeLoop.on_pr_merged` sweep, which the handler already runs internally
    (both claim the same dedup key, so wiring both would silently skip the
    S17 merge-time behavior)."""
    action = payload.get("action")
    if event == "installation" and action == "created":
        if installer is None:
            return None
        installer.on_install()
        return "install"
    if event == "pull_request" and action == "closed":
        pr = payload["pull_request"]
        if not pr.get("merged"):
            return "ignored"  # closed without merging: branch kept, no wake
        handler.on_pr_merged(pr["number"], pr["head"]["sha"])
        return "pr-merged"
    if event == "issue_comment" and action == "created":
        issue = payload["issue"]
        if "pull_request" in issue:  # a comment on a PR's conversation
            handler.on_pr_comment(issue["number"], payload["comment"]["id"])
            return "pr-comment"
        loop.wake()  # plain issue comment: cheap re-sync (see module note)
        return "wake"
    if event == "pull_request_review_comment" and action == "created":
        handler.on_pr_comment(payload["pull_request"]["number"], payload["comment"]["id"])
        return "pr-comment"
    if event == "issues" and action in _ISSUE_ACTIONS:
        loop.backfill()  # idempotent re-sync triages the new/changed issue
        return "backfill"
    if event == "push":
        # Main moved. Red-main detection belongs to the generated Actions
        # workflow (it runs the repo's own test suite); the orchestrator only
        # acts on the workflow's aorc-main-broken repository_dispatch below.
        return "ci" if payload.get("ref") == "refs/heads/main" else "ignored"
    if event == "repository_dispatch" and action == "aorc-main-broken":
        handler.on_main_broken(int(payload["client_payload"]["pr_number"]))
        return "rollback"
    return None
