"""S18 -- GitHub App install: manifest, install-time behavior (board + labels
+ config PR + immediate backfill), the awaiting-config dispatch gate, config
build-validation, auto-merge disqualification, the local-LLM fail-fast check,
and webhook routing to the S17 merge-time handlers."""

from __future__ import annotations

import json

import yaml
import pytest

from aorc.config import ModelSlot, auto_merge_allowed, build_blockers, parse_config
from aorc.credentials import MINIMAL_PERMISSIONS, CredentialBroker
from aorc.github.mock import MockGitHubClient
from aorc.harness import MockContainerRuntime
from aorc.install import (
    APP_PERMISSIONS,
    APP_WEBHOOK_EVENTS,
    CONFIG_PATH,
    CONFIG_PR_BRANCH,
    CONFIG_TEMPLATE,
    ROLLBACK_WORKFLOW,
    STANDARD_COLUMNS,
    WORKFLOW_PATH,
    ConfigGate,
    ConfigGatedWakeLoop,
    InstallHandler,
    app_manifest,
    route_webhook,
)
from aorc.interfaces import FailFastProviderError, Issue
from aorc.llm import assert_local_llm_reachable, build_llm_client
from aorc.llm.mock import MockLLMClient
from aorc.pipeline import AWAITING_CONFIG_LABEL, HELD_LABEL, LABEL_COLUMN

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEfakefakefakefakefakefakefake\n"
    "-----END RSA PRIVATE KEY-----"
)

VALID_CONFIG = """
llm:
  primary: { provider: claude, model: test-model }
setup: pip install -e .
test: pytest -q
"""


class CountingMinter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, private_key: str, repo: str, permissions: dict) -> str:
        self.calls.append((repo, dict(permissions)))
        return "ghs_" + f"{len(self.calls):04d}" + "a" * 32


class FakeWorktrees:
    def ensure(self, issue_number: int) -> str:
        return f"/worktrees/issue-{issue_number}"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_gated(issues=None, *, llm=None, concurrency=5):
    gh = MockGitHubClient(issues=issues)
    runtime = MockContainerRuntime()
    clock = Clock()
    broker = CredentialBroker(
        PRIVATE_KEY, CountingMinter(), llm_api_key="sk-" + "p" * 20, clock=clock
    )
    loop = ConfigGatedWakeLoop.compose(
        gh,
        runtime,
        FakeWorktrees(),
        broker,
        repo="acme/widget",
        llm=llm,
        concurrency=concurrency,
        clock=clock,
    )
    return gh, runtime, loop


def merge_config(gh, content: str = VALID_CONFIG) -> None:
    """Simulate the config PR having been merged: `.aorc.yml` lands on main."""
    gh.add_file("main", CONFIG_PATH, content)


# --------------------------------------------------------------------------- #
# App manifest: permissions + webhook registration
# --------------------------------------------------------------------------- #


def test_app_manifest_declares_the_fine_grained_permission_set():
    assert APP_PERMISSIONS == {
        "issues": "write",
        "pull_requests": "write",
        "contents": "write",
        "projects": "write",
        "actions": "write",
    }
    manifest = app_manifest("https://aorc.example/webhook")
    assert manifest["default_permissions"] == APP_PERMISSIONS
    assert manifest["hook_attributes"]["url"] == "https://aorc.example/webhook"


def test_app_manifest_registers_all_listed_webhook_events():
    manifest = app_manifest("https://aorc.example/webhook")
    for event in (
        "issues",
        "issue_comment",
        "pull_request",
        "pull_request_review_comment",
        "push",
        "repository_dispatch",
    ):
        assert event in manifest["default_events"]
    assert list(manifest["default_events"]) == list(APP_WEBHOOK_EVENTS)


def test_broker_token_ceiling_fits_inside_the_app_permission_set():
    # The per-issue token ceiling (S15) must be mintable under the App's own
    # permissions -- a scope the App doesn't hold could never be exchanged.
    levels = {"read": 0, "write": 1, "admin": 2}
    for scope, level in MINIMAL_PERMISSIONS.items():
        assert scope in APP_PERMISSIONS
        assert levels[level] <= levels[APP_PERMISSIONS[scope]]


# --------------------------------------------------------------------------- #
# Install-time behavior
# --------------------------------------------------------------------------- #


def make_installer(issues=None, *, llm=None):
    gh, runtime, loop = make_gated(issues, llm=llm or MockLLMClient(default="actionable"))
    return gh, runtime, InstallHandler(loop)


def test_on_install_creates_the_board_with_the_six_standard_columns():
    gh, _runtime, installer = make_installer()
    installer.on_install()
    assert ("create_board", list(STANDARD_COLUMNS)) in gh.calls
    assert list(STANDARD_COLUMNS) == [
        "Backlog",
        "Needs Clarification",
        "In Progress",
        "Blocked",
        "In Review",
        "Done",
    ]


def test_standard_columns_cover_every_derived_label_column():
    # Every column the S2 state machine can derive must exist on the board.
    assert set(LABEL_COLUMN.values()) <= set(STANDARD_COLUMNS)


def test_on_install_creates_every_pipeline_label():
    gh, _runtime, installer = make_installer()
    installer.on_install()
    for label in (
        "in-design",
        "in-test",
        "in-code",
        "in-review",
        "needs-clarification",
        "agent-blocked",
        HELD_LABEL,
        AWAITING_CONFIG_LABEL,
    ):
        assert label in gh.created_labels


def test_on_install_opens_the_config_pr_with_template_and_rollback_workflow():
    gh, _runtime, installer = make_installer()
    report = installer.on_install()
    pr = gh.pulls[report.config_pr]
    assert pr.head == CONFIG_PR_BRANCH
    assert gh.get_file(CONFIG_PATH, CONFIG_PR_BRANCH) == CONFIG_TEMPLATE
    assert gh.get_file(WORKFLOW_PATH, CONFIG_PR_BRANCH) == ROLLBACK_WORKFLOW


def test_install_pr_body_states_template_defaults_and_build_hold():
    gh, _runtime, installer = make_installer()
    report = installer.on_install()
    body = gh.pulls[report.config_pr].body.lower()
    assert "template" in body
    assert "until" in body and "merged" in body  # won't build until merged


def test_install_pr_body_carries_the_one_time_smoke_warning():
    gh, _runtime, installer = make_installer()
    report = installer.on_install()
    body = gh.pulls[report.config_pr].body.lower()
    assert "smoke" in body
    assert "auto-merge" in body


def test_config_template_is_valid_yaml():
    assert isinstance(yaml.safe_load(CONFIG_TEMPLATE), dict)


def test_on_install_runs_the_backfill_sweep_immediately():
    issues = [Issue(number=n, title=f"t{n}", body="do a bounded thing") for n in (1, 2, 3)]
    _gh, _runtime, installer = make_installer(issues)
    report = installer.on_install()
    assert sorted(report.backfill.triaged) == [1, 2, 3]


def test_on_install_routes_vague_issues_to_clarification():
    llm = MockLLMClient(responses=["not-ready", "What exactly should happen?"])
    issues = [Issue(number=1, title="t", body="somehow improve things")]
    gh, _runtime, installer = make_installer(issues, llm=llm)
    installer.on_install()
    assert "needs-clarification" in gh.issues[1].labels
    assert any("What exactly should happen?" in c.body for c in gh.comments.get(1, []))


def test_on_install_decomposes_epics_into_sub_issues():
    plan = json.dumps(
        {
            "prd": "big feature",
            "confidence": 0.95,
            "sub_issues": [{"title": "part 1", "body": "do part 1"}],
        }
    )
    llm = MockLLMClient(responses=["not-ready", plan])
    issues = [Issue(number=1, title="epic", body="- [ ] part 1\n- [ ] part 2")]
    gh, _runtime, installer = make_installer(issues, llm=llm)
    installer.on_install()
    assert any(issue.title == "part 1" for issue in gh.issues.values())


def test_dispatch_ready_issues_hold_awaiting_config_and_never_build():
    issues = [Issue(number=1, title="t", body="do a bounded thing")]
    gh, runtime, installer = make_installer(issues)
    report = installer.on_install()
    # Never build on baked-in defaults: no container was started.
    assert not any(c[0] == "start" for c in runtime.calls)
    assert AWAITING_CONFIG_LABEL in gh.issues[1].labels
    assert gh.board[1] == "Blocked"
    assert report.held_awaiting_config == [1]


def test_on_install_is_idempotent_on_duplicate_delivery():
    gh, _runtime, installer = make_installer()
    first = installer.on_install()
    second = installer.on_install()
    assert second.config_pr == first.config_pr
    prs = [p for p in gh.pulls.values() if p.head == CONFIG_PR_BRANCH]
    assert len(prs) == 1


# --------------------------------------------------------------------------- #
# ConfigGate: fail-closed validation (B23)
# --------------------------------------------------------------------------- #


def test_gate_not_ready_before_the_config_pr_merges():
    gh = MockGitHubClient()
    status = ConfigGate(gh).check()
    assert not status.ready
    assert ".aorc.yml" in status.reason


def test_gate_fails_closed_on_malformed_yaml_with_a_clear_error():
    gh = MockGitHubClient()
    merge_config(gh, "llm: [unclosed")
    status = ConfigGate(gh).check()
    assert not status.ready
    assert "aorc.yml" in status.reason.lower()


def test_gate_fails_closed_on_partial_config():
    gh = MockGitHubClient()
    merge_config(gh, "setup: make install\ntest: make test\n")  # no llm block
    status = ConfigGate(gh).check()
    assert not status.ready


def test_gate_blocks_build_when_setup_or_test_missing():
    gh = MockGitHubClient()
    merge_config(gh, "llm:\n  primary: { provider: claude, model: m }\n")
    status = ConfigGate(gh).check()
    assert not status.ready
    assert "setup" in status.reason and "test" in status.reason


def test_gate_ready_on_a_complete_config():
    gh = MockGitHubClient()
    merge_config(gh)
    status = ConfigGate(gh).check()
    assert status.ready
    assert status.config is not None and status.config.test == "pytest -q"


# --------------------------------------------------------------------------- #
# The gated loop: hold until merged, then release
# --------------------------------------------------------------------------- #


def test_gated_dispatch_holds_when_config_is_absent():
    gh, runtime, loop = make_gated([Issue(number=1, title="t", body="do a bounded thing")])
    handle = loop.dispatch_issue(1)
    assert handle is None
    assert AWAITING_CONFIG_LABEL in gh.issues[1].labels
    assert not any(c[0] == "start" for c in runtime.calls)
    assert 1 not in loop.in_flight


def test_gated_dispatch_dispatches_normally_once_config_is_ready():
    gh, runtime, loop = make_gated([Issue(number=1, title="t", body="do a bounded thing")])
    merge_config(gh)
    handle = loop.dispatch_issue(1)
    assert handle is not None
    assert ("start", 1, "aorc/issue-1") in runtime.calls


def test_wake_releases_awaiting_config_issues_after_the_config_pr_merges():
    gh, runtime, loop = make_gated(
        [Issue(number=n, title=f"t{n}", body="do a bounded thing") for n in (1, 2)]
    )
    loop.dispatch_issue(1)
    loop.dispatch_issue(2)
    assert not any(c[0] == "start" for c in runtime.calls)

    merge_config(gh)
    report = loop.wake()

    assert sorted(report.released) == [1, 2]
    assert AWAITING_CONFIG_LABEL not in gh.issues[1].labels
    assert AWAITING_CONFIG_LABEL not in gh.issues[2].labels
    assert {c for c in runtime.calls if c[0] == "start"} == {
        ("start", 1, "aorc/issue-1"),
        ("start", 2, "aorc/issue-2"),
    }


def test_wake_keeps_holding_while_config_is_still_absent():
    gh, runtime, loop = make_gated([Issue(number=1, title="t", body="do a bounded thing")])
    loop.dispatch_issue(1)
    report = loop.wake()
    assert report.released == []
    assert AWAITING_CONFIG_LABEL in gh.issues[1].labels
    assert not any(c[0] == "start" for c in runtime.calls)


def test_backfill_never_retriages_awaiting_config_issues():
    llm = MockLLMClient(default="actionable")
    gh, _runtime, loop = make_gated(
        [Issue(number=1, title="t", body="do a bounded thing")], llm=llm
    )
    loop.backfill()
    calls_after_first = len(llm.calls)
    report = loop.backfill()
    assert report.triaged == []
    assert len(llm.calls) == calls_after_first


def test_awaiting_config_maps_to_the_blocked_column():
    assert LABEL_COLUMN[AWAITING_CONFIG_LABEL] == "Blocked"


# --------------------------------------------------------------------------- #
# Config build-validation + auto-merge disqualification (B27)
# --------------------------------------------------------------------------- #


def _config(raw_yaml: str):
    return parse_config(yaml.safe_load(raw_yaml))


def test_build_blockers_lists_missing_setup_and_test():
    cfg = _config("llm:\n  primary: { provider: claude, model: m }\n")
    blockers = build_blockers(cfg)
    assert any("setup" in b for b in blockers)
    assert any("test" in b for b in blockers)


def test_build_blockers_empty_on_a_complete_config():
    assert build_blockers(_config(VALID_CONFIG)) == []


def test_missing_smoke_block_permanently_disqualifies_auto_merge():
    cfg = _config(
        "llm:\n  primary: { provider: claude, model: m }\n"
        "setup: s\ntest: t\nmerge: { auto: true }\n"
    )
    assert cfg.merge_auto is True
    assert auto_merge_allowed(cfg) is False


def test_auto_merge_allowed_requires_both_smoke_and_opt_in():
    with_smoke = (
        "llm:\n  primary: { provider: claude, model: m }\n"
        "setup: s\ntest: t\n"
        "smoke:\n  - { input: a, expect: b }\n"
    )
    assert auto_merge_allowed(_config(with_smoke + "merge: { auto: true }\n")) is True
    assert auto_merge_allowed(_config(with_smoke)) is False  # auto defaults off


# --------------------------------------------------------------------------- #
# Local-LLM constraint: fail fast on a cloud runner
# --------------------------------------------------------------------------- #


def test_local_base_url_on_a_cloud_runner_fails_fast():
    slot = ModelSlot(provider="ollama", model="m", base_url="http://localhost:11434")
    with pytest.raises(FailFastProviderError) as exc:
        assert_local_llm_reachable(slot, runner_environment="github-hosted")
    assert "self-hosted" in str(exc.value)


def test_host_docker_internal_counts_as_local():
    slot = ModelSlot(
        provider="ollama", model="m", base_url="http://host.docker.internal:11434"
    )
    with pytest.raises(FailFastProviderError):
        assert_local_llm_reachable(slot, runner_environment="github-hosted")


def test_local_base_url_on_a_self_hosted_runner_is_fine():
    slot = ModelSlot(provider="ollama", model="m", base_url="http://127.0.0.1:11434")
    assert_local_llm_reachable(slot, runner_environment="self-hosted")


def test_hosted_provider_without_base_url_is_fine_anywhere():
    slot = ModelSlot(provider="claude", model="m")
    assert_local_llm_reachable(slot, runner_environment="github-hosted")


def test_build_llm_client_performs_the_fail_fast_check():
    slot = ModelSlot(provider="ollama", model="m", base_url="http://localhost:11434")
    with pytest.raises(FailFastProviderError):
        build_llm_client(slot, runner_environment="github-hosted")


# --------------------------------------------------------------------------- #
# Webhook routing: events -> the S17 handlers (not the bare WakeLoop sweep)
# --------------------------------------------------------------------------- #


class SpyMergeHandler:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def on_pr_merged(self, pr_number, head_sha):
        self.calls.append(("pr-merged", pr_number, head_sha))

    def on_pr_comment(self, pr_number, comment_id):
        self.calls.append(("pr-comment", pr_number, comment_id))

    def on_main_broken(self, pr_number):
        self.calls.append(("main-broken", pr_number))


class SpyLoop:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def wake(self):
        self.calls.append("wake")

    def backfill(self):
        self.calls.append("backfill")


class SpyInstaller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def on_install(self):
        self.calls.append("install")


def _route(event, payload):
    handler, loop, installer = SpyMergeHandler(), SpyLoop(), SpyInstaller()
    outcome = route_webhook(
        event, payload, handler=handler, loop=loop, installer=installer
    )
    return outcome, handler, loop, installer


def test_merged_pr_routes_to_the_full_merge_handler():
    outcome, handler, _loop, _inst = _route(
        "pull_request",
        {
            "action": "closed",
            "pull_request": {"number": 5, "merged": True, "head": {"sha": "abc"}},
        },
    )
    assert outcome == "pr-merged"
    assert handler.calls == [("pr-merged", 5, "abc")]


def test_closed_unmerged_pr_is_ignored():
    outcome, handler, _loop, _inst = _route(
        "pull_request",
        {
            "action": "closed",
            "pull_request": {"number": 5, "merged": False, "head": {"sha": "abc"}},
        },
    )
    assert outcome == "ignored"
    assert handler.calls == []


def test_pr_issue_comment_routes_to_on_pr_comment():
    outcome, handler, _loop, _inst = _route(
        "issue_comment",
        {
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "..."}},
            "comment": {"id": 33},
        },
    )
    assert outcome == "pr-comment"
    assert handler.calls == [("pr-comment", 7, 33)]


def test_review_comment_routes_to_on_pr_comment():
    outcome, handler, _loop, _inst = _route(
        "pull_request_review_comment",
        {"action": "created", "pull_request": {"number": 5}, "comment": {"id": 44}},
    )
    assert outcome == "pr-comment"
    assert handler.calls == [("pr-comment", 5, 44)]


def test_plain_issue_comment_triggers_a_wake():
    outcome, handler, loop, _inst = _route(
        "issue_comment",
        {"action": "created", "issue": {"number": 7}, "comment": {"id": 33}},
    )
    assert outcome == "wake"
    assert handler.calls == []
    assert loop.calls == ["wake"]


def test_issue_events_trigger_the_backfill_resync():
    for action in ("opened", "edited", "labeled", "reopened"):
        outcome, _handler, loop, _inst = _route(
            "issues", {"action": action, "issue": {"number": 3}}
        )
        assert outcome == "backfill"
        assert loop.calls == ["backfill"]


def test_repository_dispatch_main_broken_routes_to_rollback():
    outcome, handler, _loop, _inst = _route(
        "repository_dispatch",
        {"action": "aorc-main-broken", "client_payload": {"pr_number": 5}},
    )
    assert outcome == "rollback"
    assert handler.calls == [("main-broken", 5)]


def test_push_to_main_is_left_to_the_actions_workflow():
    outcome, handler, loop, _inst = _route("push", {"ref": "refs/heads/main"})
    assert outcome == "ci"
    assert handler.calls == [] and loop.calls == []


def test_installation_event_routes_to_on_install():
    outcome, _handler, _loop, installer = _route("installation", {"action": "created"})
    assert outcome == "install"
    assert installer.calls == ["install"]


def test_unknown_events_are_unrouted():
    outcome, handler, loop, installer = _route("watch", {"action": "started"})
    assert outcome is None
    assert handler.calls == [] and loop.calls == [] and installer.calls == []


# --------------------------------------------------------------------------- #
# The push-to-main rollback workflow (S17 wiring obligation)
# --------------------------------------------------------------------------- #


def test_rollback_workflow_is_valid_yaml_triggered_by_push_to_main():
    workflow = yaml.safe_load(ROLLBACK_WORKFLOW)
    assert workflow["on"]["push"]["branches"] == ["main"]


def test_rollback_workflow_reverts_and_notifies_the_orchestrator():
    assert "git revert" in ROLLBACK_WORKFLOW
    assert "aorc-main-broken" in ROLLBACK_WORKFLOW
    assert ".aorc.yml" in ROLLBACK_WORKFLOW  # test commands come from config,
    # never from baked-in defaults
