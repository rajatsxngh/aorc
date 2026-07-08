"""S21 -- Composition root / entry point: `python -m aorc <subcommand>`."""

from __future__ import annotations

import pytest

from aorc.__main__ import (
    BASE_IMAGE_ENV,
    GITHUB_TOKEN_ENV,
    REPO_ENV,
    Collaborators,
    StartupError,
    compose,
    pat_passthrough_minter,
    run,
)
from aorc.config import ModelSlot, load_config, parse_config
from aorc.credentials import CredentialBroker
from aorc.github.mock import MockGitHubClient
from aorc.harness import MockContainerRuntime
from aorc.install import ConfigGatedWakeLoop, InstallHandler
from aorc.interfaces import Issue
from aorc.llm.mock import MockLLMClient
from aorc.merge import MergeTimeHandler, MockGitOps
from aorc.tester import SubprocessTestRunner

VALID_CONFIG = """
llm:
  primary: { provider: claude, model: test-model }
setup: pip install -e .
test: pytest -q
"""


class FakeWorktrees:
    """Same style as test_install.py's -- no real git needed for wiring
    tests; the split-brain sync fix is S22's job, not S21's."""

    def ensure(self, issue_number: int) -> str:
        return f"/worktrees/issue-{issue_number}"


class CountingMinter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, private_key: str, repo: str, permissions: dict) -> str:
        self.calls.append((repo, dict(permissions)))
        return "ghs_" + f"{len(self.calls):04d}" + "a" * 32


def make_collaborators(issues=None, *, llm=None) -> Collaborators:
    gh = MockGitHubClient(issues=issues)
    runtime = MockContainerRuntime()
    broker = CredentialBroker(
        "", CountingMinter(), llm_api_key="sk-" + "p" * 20, clock=lambda: 1000.0
    )
    loop = ConfigGatedWakeLoop.compose(
        gh,
        runtime,
        FakeWorktrees(),
        broker,
        repo="acme/widget",
        llm=llm or MockLLMClient(default="actionable"),
    )
    merge_handler = MergeTimeHandler(loop, MockGitOps())
    return Collaborators(
        llm=llm, loop=loop, installer=InstallHandler(loop), merge_handler=merge_handler
    )


# --------------------------------------------------------------------------- #
# Fail-closed startup: absent/malformed config, missing env vars
# --------------------------------------------------------------------------- #


def test_missing_config_file_fails_closed_with_nonzero_exit(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.yml"
    code = run(["--config", str(missing), "install"])
    assert code == 1
    assert "does-not-exist.yml" in capsys.readouterr().err


def test_malformed_config_fails_closed_with_nonzero_exit(tmp_path, capsys):
    bad = tmp_path / ".aorc.yml"
    bad.write_text("not: [valid, yaml, mapping\n")  # unterminated flow sequence
    code = run(["--config", str(bad), "install"])
    assert code == 1
    assert "could not parse" in capsys.readouterr().err


def test_config_missing_required_llm_block_fails_closed(tmp_path, capsys):
    cfg = tmp_path / ".aorc.yml"
    cfg.write_text("setup: pip install -e .\ntest: pytest -q\n")
    code = run(["--config", str(cfg), "install"])
    assert code == 1
    assert "llm.primary" in capsys.readouterr().err


def test_compose_fails_closed_on_missing_github_token(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    with pytest.raises(StartupError, match=GITHUB_TOKEN_ENV):
        compose(config, "acme/widget")


def test_compose_fails_closed_on_missing_base_image(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "ghp_" + "a" * 36)
    monkeypatch.delenv(BASE_IMAGE_ENV, raising=False)
    with pytest.raises(StartupError, match=BASE_IMAGE_ENV):
        compose(config, "acme/widget", github=MockGitHubClient())


def test_run_requires_repo_env_or_flag(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    monkeypatch.delenv(REPO_ENV, raising=False)
    code = run(["--config", str(cfg_path), "install"])
    assert code == 1
    assert REPO_ENV in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# compose(): every real adapter is overridable -- no env vars needed at all
# --------------------------------------------------------------------------- #


def test_compose_builds_purely_from_overrides_with_zero_env_vars(monkeypatch):
    for var in (GITHUB_TOKEN_ENV, BASE_IMAGE_ENV):
        monkeypatch.delenv(var, raising=False)
    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(default="actionable"),
    )
    assert isinstance(collaborators.loop, ConfigGatedWakeLoop)


def test_compose_wraps_the_default_llm_in_backoff_but_not_an_override():
    from aorc.escalation import BackoffLLMClient

    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    built = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
    )
    assert isinstance(built.llm, BackoffLLMClient)

    mock_llm = MockLLMClient()
    overridden = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=mock_llm,
    )
    assert overridden.llm is mock_llm


def test_compose_wraps_github_client_exactly_once():
    from aorc.credentials import ScrubbingGitHubClient

    gh = MockGitHubClient()
    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=gh,
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert isinstance(collaborators.github, ScrubbingGitHubClient)
    # exactly one layer: unwrap once and land on the real mock, not another wrapper
    assert collaborators.github._inner is gh


def test_compose_attaches_a_pipeline_driver_when_setup_and_test_are_configured():
    from aorc.driver import PipelineDriver

    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert isinstance(collaborators.driver, PipelineDriver)
    assert collaborators.loop.driver is collaborators.driver


def test_compose_wires_a_container_test_runner_by_default():
    """S27: the live composition path (docker, the config default) must not
    use `SubprocessTestRunner` for the build pipeline -- toolchain commands
    run inside the issue's own container via `ContainerTestRunner`."""
    from aorc.tester import ContainerTestRunner

    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert isinstance(collaborators.test_runner, ContainerTestRunner)


def test_compose_keeps_subprocess_test_runner_for_actions_runtime():
    """S25's `ActionsContainerRuntime` has no local container for `docker
    exec` to target -- S27 stays out of scope for it (the driver still runs
    orchestrator-side per S25's own scope note)."""
    config = parse_config(
        {
            "llm": {"primary": {"provider": "claude", "model": "m"}},
            "setup": "x",
            "test": "y",
            "container": {"runtime": "actions", "workflow_file": "aorc-build.yml"},
        }
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert type(collaborators.test_runner) is SubprocessTestRunner


def test_compose_no_container_flag_falls_back_to_subprocess_test_runner():
    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
        no_container=True,
    )
    assert type(collaborators.test_runner) is SubprocessTestRunner


def test_compose_threads_worktrees_into_the_merge_handler():
    """S27: `MergeTimeHandler`'s stale-PR recheck must run its toolchain
    against the real per-issue worktree, not a single fixed `cwd` shared by
    every issue -- otherwise `ContainerTestRunner` can't resolve a container
    from it."""
    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    worktrees = FakeWorktrees()
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=worktrees,
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert collaborators.merge_handler._cwd_for(42) == worktrees.ensure(42)


def test_compose_leaves_driver_none_without_setup_and_test_configured():
    # No `setup`/`test` -- same fields `config.build_blockers` gates the
    # rest of the build pipeline on. `compose()` also runs for `install`,
    # before any real `.aorc.yml` may exist yet, so this must not crash.
    config = parse_config({"llm": {"primary": {"provider": "claude", "model": "m"}}})
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert collaborators.driver is None
    assert collaborators.loop.driver is None


def test_compose_uses_a_driver_override_as_is():
    sentinel = object()
    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
        driver=sentinel,
    )
    assert collaborators.driver is sentinel
    assert collaborators.loop.driver is sentinel


def test_compose_builds_a_merge_handler_by_default():
    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert isinstance(collaborators.merge_handler, MergeTimeHandler)


def test_compose_uses_a_merge_handler_override_as_is():
    sentinel = object()
    config = parse_config(
        {"llm": {"primary": {"provider": "claude", "model": "m"}}, "setup": "x", "test": "y"}
    )
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
        merge_handler=sentinel,
    )
    assert collaborators.merge_handler is sentinel


def test_compose_builds_merge_handler_without_stale_pr_wiring_when_unconfigured():
    # No setup/test -- same gate the driver stays None under. The merge
    # handler must still build (issue-close-on-merge needs no build
    # pipeline), just without stale-PR re-review capability.
    config = parse_config({"llm": {"primary": {"provider": "claude", "model": "m"}}})
    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
        llm=MockLLMClient(),
    )
    assert isinstance(collaborators.merge_handler, MergeTimeHandler)
    assert collaborators.merge_handler._reviewer is None
    assert collaborators.merge_handler._test_command is None


def test_pat_passthrough_minter_returns_fixed_token_regardless_of_args():
    minter = pat_passthrough_minter("ghs_fixedtoken" + "a" * 20)
    t1 = minter("any-private-key", "acme/widget", {"contents": "write"})
    t2 = minter("", "other/repo", {"issues": "read"})
    assert t1 == t2 == "ghs_fixedtoken" + "a" * 20


# --------------------------------------------------------------------------- #
# S23: default broker is the real App-JWT minter; --dev-pat-minter opts out
# --------------------------------------------------------------------------- #


def test_compose_default_broker_fails_closed_on_missing_app_id(tmp_path, monkeypatch):
    from aorc.__main__ import APP_ID_ENV, APP_PRIVATE_KEY_PATH_ENV

    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.delenv(APP_ID_ENV, raising=False)
    monkeypatch.delenv(APP_PRIVATE_KEY_PATH_ENV, raising=False)

    with pytest.raises(StartupError, match=APP_ID_ENV):
        compose(
            config,
            "acme/widget",
            github=MockGitHubClient(),
            runtime=MockContainerRuntime(),
            worktrees=FakeWorktrees(),
        )


def test_compose_default_broker_fails_closed_on_missing_private_key_path(
    tmp_path, monkeypatch
):
    from aorc.__main__ import APP_ID_ENV, APP_PRIVATE_KEY_PATH_ENV

    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.setenv(APP_ID_ENV, "app-123")
    monkeypatch.delenv(APP_PRIVATE_KEY_PATH_ENV, raising=False)

    with pytest.raises(StartupError, match=APP_PRIVATE_KEY_PATH_ENV):
        compose(
            config,
            "acme/widget",
            github=MockGitHubClient(),
            runtime=MockContainerRuntime(),
            worktrees=FakeWorktrees(),
        )


def test_compose_default_broker_reads_key_material_from_the_configured_path(
    tmp_path, monkeypatch
):
    from aorc.__main__ import APP_ID_ENV, APP_PRIVATE_KEY_PATH_ENV

    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    config = load_config(cfg_path)
    key_path = tmp_path / "app-key.pem"
    key_path.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    monkeypatch.setenv(APP_ID_ENV, "app-123")
    monkeypatch.setenv(APP_PRIVATE_KEY_PATH_ENV, str(key_path))

    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
    )

    broker = collaborators.loop._broker
    # Key material read from the configured path, handed only to the broker
    # (invariant #2) -- constructing the real minter must itself be
    # dependency-free; only an actual `.mint()` call touches PyJWT/network.
    assert broker._private_key == key_path.read_text()
    assert broker._minter.__module__ == "aorc.github.app_token"


def test_compose_dev_pat_minter_flag_uses_the_passthrough_minter(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "ghs_" + "a" * 36)

    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        runtime=MockContainerRuntime(),
        worktrees=FakeWorktrees(),
        dev_pat_minter=True,
    )

    token = collaborators.loop._broker.mint(1, "acme/widget")
    assert token.token == "ghs_" + "a" * 36


# --------------------------------------------------------------------------- #
# S25: runtime selection (Docker vs Actions) comes from `.aorc.yml`
# --------------------------------------------------------------------------- #

ACTIONS_CONFIG = """
llm:
  primary: { provider: claude, model: test-model }
setup: pip install -e .
test: pytest -q
container:
  runtime: actions
  workflow_file: aorc-build.yml
"""


def test_compose_defaults_to_docker_runtime_when_container_block_absent(
    tmp_path, monkeypatch
):
    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "ghp_" + "a" * 36)
    monkeypatch.setenv(BASE_IMAGE_ENV, "aorc/base:latest")

    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        worktrees=FakeWorktrees(),
        broker=CredentialBroker("", CountingMinter()),
    )

    from aorc.harness import DockerContainerRuntime

    assert isinstance(collaborators.loop.harness._runtime, DockerContainerRuntime)


def test_compose_selects_actions_runtime_and_mints_its_own_actions_write_token(
    tmp_path, monkeypatch
):
    from aorc.__main__ import APP_ID_ENV, APP_PRIVATE_KEY_PATH_ENV

    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(ACTIONS_CONFIG)
    config = load_config(cfg_path)
    key_path = tmp_path / "app-key.pem"
    key_path.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    monkeypatch.setenv(APP_ID_ENV, "app-123")
    monkeypatch.setenv(APP_PRIVATE_KEY_PATH_ENV, str(key_path))
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "ghp_" + "a" * 36)

    captured_minter_calls = []

    def fake_build_app_token_minter(app_id):
        def minter(private_key, repo, permissions):
            captured_minter_calls.append((app_id, repo, dict(permissions)))
            return "ghs_actions_token"

        return minter

    import aorc.github.app_token as app_token_module

    monkeypatch.setattr(app_token_module, "build_app_token_minter", fake_build_app_token_minter)

    collaborators = compose(
        config, "acme/widget", github=MockGitHubClient(), worktrees=FakeWorktrees()
    )

    from aorc.github.actions_runtime import ActionsContainerRuntime

    runtime = collaborators.loop.harness._runtime
    assert isinstance(runtime, ActionsContainerRuntime)
    assert runtime._workflow_file == "aorc-build.yml"
    assert runtime._token == "ghs_actions_token"
    assert captured_minter_calls == [("app-123", "acme/widget", {"actions": "write"})]


def test_compose_actions_runtime_fails_closed_on_missing_app_id(tmp_path, monkeypatch):
    from aorc.__main__ import APP_ID_ENV, APP_PRIVATE_KEY_PATH_ENV

    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(ACTIONS_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.delenv(APP_ID_ENV, raising=False)
    monkeypatch.delenv(APP_PRIVATE_KEY_PATH_ENV, raising=False)

    with pytest.raises(StartupError, match=APP_ID_ENV):
        compose(
            config,
            "acme/widget",
            github=MockGitHubClient(),
            worktrees=FakeWorktrees(),
            broker=CredentialBroker("", CountingMinter()),
        )


def test_compose_actions_runtime_dev_pat_minter_reuses_the_github_pat(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(ACTIONS_CONFIG)
    config = load_config(cfg_path)
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "ghp_" + "a" * 36)

    collaborators = compose(
        config,
        "acme/widget",
        github=MockGitHubClient(),
        worktrees=FakeWorktrees(),
        dev_pat_minter=True,
    )

    from aorc.github.actions_runtime import ActionsContainerRuntime

    runtime = collaborators.loop.harness._runtime
    assert isinstance(runtime, ActionsContainerRuntime)
    assert runtime._token == "ghp_" + "a" * 36


def test_run_threads_dev_pat_minter_flag_into_compose(tmp_path, monkeypatch):
    import aorc.__main__ as main_module

    cfg_path = tmp_path / ".aorc.yml"
    cfg_path.write_text(VALID_CONFIG)
    monkeypatch.setenv(REPO_ENV, "acme/widget")
    captured = {}

    def fake_compose(config, repo, **kwargs):
        captured.update(kwargs)
        return make_collaborators()

    monkeypatch.setattr(main_module, "compose", fake_compose)

    code = run(["--config", str(cfg_path), "--dev-pat-minter", "backfill"])

    assert code == 0
    assert captured["dev_pat_minter"] is True


# --------------------------------------------------------------------------- #
# Subcommands, end to end, against in-memory mocks (zero third-party deps)
# --------------------------------------------------------------------------- #


def test_install_subcommand_runs_the_full_install_flow(capsys):
    collaborators = make_collaborators()
    code = run(["install"], collaborators=collaborators)
    assert code == 0
    assert collaborators.github._inner.board_columns is not None
    assert "install: config PR #" in capsys.readouterr().out


def test_backfill_subcommand_triages_open_issues(capsys):
    issues = [Issue(number=1, title="Fix the bug", body="details", labels=[])]
    collaborators = make_collaborators(issues=issues)
    code = run(["backfill"], collaborators=collaborators)
    assert code == 0
    # no .aorc.yml on main -> the config gate parks it under awaiting-config
    assert "awaiting-config" in collaborators.github.get_labels(1)
    assert "backfill:" in capsys.readouterr().out


def test_wake_subcommand_runs_a_cron_tick(capsys):
    collaborators = make_collaborators()
    code = run(["wake"], collaborators=collaborators)
    assert code == 0
    assert "wake:" in capsys.readouterr().out


def test_run_issue_subcommand_dispatches_a_single_issue(capsys):
    collaborators = make_collaborators(
        issues=[Issue(number=7, title="Do a thing", body="x", labels=[])]
    )
    collaborators.github._inner.add_file(
        "main",
        ".aorc.yml",
        "llm:\n  primary: { provider: claude, model: m }\nsetup: x\ntest: y\n",
    )
    code = run(["run-issue", "7"], collaborators=collaborators)
    assert code == 0
    assert ("start", 7, "aorc/issue-7") in collaborators.loop.harness._runtime.calls
    assert "dispatched issue #7" in capsys.readouterr().out


def test_run_issue_without_config_parks_under_awaiting_config():
    collaborators = make_collaborators(
        issues=[Issue(number=9, title="Do a thing", body="x", labels=[])]
    )
    code = run(["run-issue", "9"], collaborators=collaborators)
    assert code == 0
    assert "awaiting-config" in collaborators.github.get_labels(9)
    assert collaborators.loop.harness._runtime.calls == []


def test_run_issue_gate_closed_reports_parked_not_dispatched(capsys):
    """`ConfigGatedWakeLoop.dispatch_issue` returns None when the config
    gate is closed (issue parked under awaiting-config, no container
    started) -- the CLI must not claim a dispatch happened."""
    collaborators = make_collaborators(
        issues=[Issue(number=9, title="Do a thing", body="x", labels=[])]
    )
    code = run(["run-issue", "9"], collaborators=collaborators)
    out = capsys.readouterr().out
    assert code == 0
    assert "dispatched" not in out
    assert "parked issue #9 (awaiting-config)" in out


# --------------------------------------------------------------------------- #
# S24: `serve` subcommand -- webhook secret fail-closed + wiring to webhook.serve
# --------------------------------------------------------------------------- #


def test_serve_subcommand_fails_closed_without_webhook_secret(monkeypatch, capsys):
    from aorc.__main__ import WEBHOOK_SECRET_ENV

    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
    collaborators = make_collaborators()

    code = run(["serve"], collaborators=collaborators)

    assert code == 1
    assert WEBHOOK_SECRET_ENV in capsys.readouterr().err


def test_serve_subcommand_wires_the_receiver_and_never_logs_the_secret(monkeypatch, capsys):
    import aorc.__main__ as main_module

    monkeypatch.setenv(main_module.WEBHOOK_SECRET_ENV, "s3kr1t-value")
    collaborators = make_collaborators()
    captured = {}

    class FakeServer:
        def serve_forever(self):
            captured["served"] = True

    def fake_serve(secret, route, *, host, port):
        captured["secret"] = secret
        captured["host"] = host
        captured["port"] = port
        captured["route"] = route
        return FakeServer()

    monkeypatch.setattr(main_module.webhook, "serve", fake_serve)

    code = run(["serve"], collaborators=collaborators)
    out, err = capsys.readouterr()

    assert code == 0
    assert captured["secret"] == "s3kr1t-value"
    assert captured["host"] == main_module.DEFAULT_SERVE_HOST
    assert captured["port"] == main_module.DEFAULT_SERVE_PORT
    assert captured["served"] is True
    assert "serve: listening" in out
    assert "s3kr1t-value" not in out
    assert "s3kr1t-value" not in err

    route = captured["route"]
    assert route.keywords["handler"] is collaborators.merge_handler
    assert route.keywords["loop"] is collaborators.loop
    assert route.keywords["installer"] is collaborators.installer


def test_serve_subcommand_accepts_host_and_port_flags(monkeypatch):
    import aorc.__main__ as main_module

    monkeypatch.setenv(main_module.WEBHOOK_SECRET_ENV, "s3kr1t")
    collaborators = make_collaborators()
    captured = {}

    class FakeServer:
        def serve_forever(self):
            pass

    def fake_serve(secret, route, *, host, port):
        captured["host"] = host
        captured["port"] = port
        return FakeServer()

    monkeypatch.setattr(main_module.webhook, "serve", fake_serve)

    code = run(["serve", "--host", "127.0.0.1", "--port", "9090"], collaborators=collaborators)

    assert code == 0
    assert captured == {"host": "127.0.0.1", "port": 9090}
