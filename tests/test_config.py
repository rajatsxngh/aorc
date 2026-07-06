"""S1 — `.aorc.yml` config loading drives provider/model selection.

Config is the *only* source of model names and provider choice (architecture
invariant #2: no hardcoded model names / secrets in code).
"""

import os

import pytest

from aorc.config import AorcConfig, ConfigError, ModelSlot, load_config

SAMPLE = """\
llm:
  primary:    {{ provider: claude, model: {primary_model}, api_key: $AORC_TEST_KEY }}
  escalation: {{ provider: claude, model: {esc_model},   api_key: $AORC_TEST_KEY }}
setup: pip install -e .
test:  pytest tests/
lint:  ruff check .
smoke:
  - {{ input: examples/case1.yml, expect: examples/case1.sql }}
merge:
  auto: false
failure:
  primary_attempts: 3
  escalation_attempts: 1
"""


def _write(tmp_path, text):
    p = tmp_path / ".aorc.yml"
    p.write_text(text)
    return p


def test_load_config_parses_model_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("AORC_TEST_KEY", "s3cr3t")
    cfg = load_config(_write(tmp_path, SAMPLE.format(primary_model="alpha-1", esc_model="beta-2")))
    assert isinstance(cfg, AorcConfig)
    assert isinstance(cfg.primary, ModelSlot)
    assert cfg.primary.provider == "claude"
    assert cfg.primary.model == "alpha-1"
    assert cfg.escalation.model == "beta-2"


def test_env_vars_expanded_for_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AORC_TEST_KEY", "s3cr3t")
    cfg = load_config(_write(tmp_path, SAMPLE.format(primary_model="alpha-1", esc_model="beta-2")))
    assert cfg.primary.api_key == "s3cr3t"
    # The literal token must never survive into the parsed config.
    assert "$AORC_TEST_KEY" != cfg.primary.api_key


def test_toolchain_and_failure_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AORC_TEST_KEY", "s3cr3t")
    cfg = load_config(_write(tmp_path, SAMPLE.format(primary_model="a", esc_model="b")))
    assert cfg.setup == "pip install -e ."
    assert cfg.test == "pytest tests/"
    assert cfg.lint == "ruff check ."
    assert cfg.primary_attempts == 3
    assert cfg.escalation_attempts == 1
    assert cfg.merge_auto is False
    assert cfg.smoke and cfg.smoke[0]["input"] == "examples/case1.yml"


def test_coverage_floor_defaults_and_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AORC_TEST_KEY", "s3cr3t")
    cfg = load_config(_write(tmp_path, SAMPLE.format(primary_model="a", esc_model="b")))
    assert cfg.coverage_command is None
    assert cfg.coverage_floor == 80.0

    with_coverage = SAMPLE.format(primary_model="a", esc_model="b") + (
        "coverage:\n  command: coverage run -m pytest && coverage report\n  floor: 90\n"
    )
    cfg = load_config(_write(tmp_path, with_coverage))
    assert cfg.coverage_command == "coverage run -m pytest && coverage report"
    assert cfg.coverage_floor == 90.0


def test_dispatch_concurrency_defaults_and_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AORC_TEST_KEY", "s3cr3t")
    cfg = load_config(_write(tmp_path, SAMPLE.format(primary_model="a", esc_model="b")))
    assert cfg.dispatch_concurrency == 5

    with_dispatch = SAMPLE.format(primary_model="a", esc_model="b") + (
        "dispatch:\n  concurrency: 8\n"
    )
    cfg = load_config(_write(tmp_path, with_dispatch))
    assert cfg.dispatch_concurrency == 8


def test_clarification_windows_default_and_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AORC_TEST_KEY", "s3cr3t")
    cfg = load_config(_write(tmp_path, SAMPLE.format(primary_model="a", esc_model="b")))
    assert cfg.clarification_nudge_days == 7.0
    assert cfg.clarification_block_days == 7.0

    with_clarification = SAMPLE.format(primary_model="a", esc_model="b") + (
        "clarification:\n  nudge_days: 3\n  block_days: 10\n"
    )
    cfg = load_config(_write(tmp_path, with_clarification))
    assert cfg.clarification_nudge_days == 3.0
    assert cfg.clarification_block_days == 10.0


def test_clarification_windows_infinity(tmp_path, monkeypatch):
    monkeypatch.setenv("AORC_TEST_KEY", "s3cr3t")
    with_infinity = SAMPLE.format(primary_model="a", esc_model="b") + (
        "clarification:\n  nudge_days: infinity\n  block_days: infinity\n"
    )
    cfg = load_config(_write(tmp_path, with_infinity))
    assert cfg.clarification_nudge_days == float("inf")
    assert cfg.clarification_block_days == float("inf")


def test_local_base_url_slot(tmp_path):
    text = """\
llm:
  primary: { provider: ollama, model: some-local, base_url: http://host.docker.internal:11434 }
setup: make
test: make test
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.primary.provider == "ollama"
    assert cfg.primary.base_url == "http://host.docker.internal:11434"
    assert cfg.escalation is None


def test_malformed_config_fails_closed(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "llm: [this is not a mapping]"))


def test_missing_llm_primary_fails_closed(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "setup: x\ntest: y\n"))


def test_no_model_names_hardcoded_in_source():
    """Architecture invariant #2 — model names live in config, not code."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "aorc"
    banned = ["claude-sonnet", "claude-opus", "claude-3", "gpt-4", "gpt-3", "llama3", "claude-fable"]
    offenders = []
    for py in src.rglob("*.py"):
        low = py.read_text().lower()
        for token in banned:
            if token in low:
                offenders.append(f"{py.name}: {token}")
    assert not offenders, f"hardcoded model names found: {offenders}"
