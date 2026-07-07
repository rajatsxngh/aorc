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
    return Collaborators(llm=llm, loop=loop, installer=InstallHandler(loop))


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


def test_pat_passthrough_minter_returns_fixed_token_regardless_of_args():
    minter = pat_passthrough_minter("ghs_fixedtoken" + "a" * 20)
    t1 = minter("any-private-key", "acme/widget", {"contents": "write"})
    t2 = minter("", "other/repo", {"issues": "read"})
    assert t1 == t2 == "ghs_fixedtoken" + "a" * 20


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
