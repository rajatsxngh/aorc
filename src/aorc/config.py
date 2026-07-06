"""`.aorc.yml` loading.

Architecture invariant #2: model names and secrets come *only* from config,
never from code. This module is the single place that reads the repo config
and turns it into typed objects the orchestrator can consume.

Fails closed (raises `ConfigError`) on anything malformed rather than falling
back to guessed defaults — running the pipeline on a hallucinated toolchain is
exactly the failure `.aorc.yml` exists to prevent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ``$VAR`` and ``${VAR}`` — expanded from the environment so secrets stay out
# of the file. A referenced-but-unset var fails closed.
_ENV_RE = re.compile(r"\$\{?([A-Z0-9_]+)\}?")


class ConfigError(ValueError):
    """Raised on any malformed or incomplete `.aorc.yml`."""


@dataclass
class ModelSlot:
    """One configurable model position (primary or escalation)."""

    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None  # set for local / OpenAI-compatible endpoints


@dataclass
class AorcConfig:
    primary: ModelSlot
    escalation: ModelSlot | None = None
    setup: str | None = None
    test: str | None = None
    lint: str | None = None
    smoke: list[dict] = field(default_factory=list)
    coverage_command: str | None = None
    coverage_floor: float = 80.0
    merge_auto: bool = False
    primary_attempts: int = 3
    escalation_attempts: int = 1
    dispatch_concurrency: int = 5
    clarification_nudge_days: float = 7.0
    clarification_block_days: float = 7.0
    raw: dict = field(default_factory=dict)


def _expand(value: Any) -> Any:
    """Expand ``$VAR`` / ``${VAR}`` references from the environment."""
    if not isinstance(value, str):
        return value

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise ConfigError(f"environment variable ${name} referenced in .aorc.yml is not set")
        return os.environ[name]

    if _ENV_RE.search(value):
        return _ENV_RE.sub(repl, value)
    return value


def _parse_window(value: Any, default: float) -> float:
    """A clarification timeout window in days. Accepts a number, or the
    string 'infinity'/'inf' (YAML's `.inf` tag already parses as a float) to
    restore wait-forever behavior."""
    if value is None:
        return default
    if isinstance(value, str) and value.strip().lower() in ("infinity", "inf"):
        return float("inf")
    return float(value)


def _parse_slot(data: Any, where: str) -> ModelSlot:
    if not isinstance(data, dict):
        raise ConfigError(f"llm.{where} must be a mapping")
    provider = data.get("provider")
    model = data.get("model")
    if not provider or not model:
        raise ConfigError(f"llm.{where} must define both 'provider' and 'model'")
    return ModelSlot(
        provider=str(provider),
        model=str(model),
        api_key=_expand(data.get("api_key")),
        base_url=_expand(data.get("base_url")),
    )


def load_config(path: str | Path) -> AorcConfig:
    """Parse a `.aorc.yml` file into an `AorcConfig`. Fails closed."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # malformed YAML
        raise ConfigError(f"could not parse {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a YAML mapping at the top level")

    return parse_config(raw)


def parse_config(raw: dict) -> AorcConfig:
    """Validate an already-parsed config mapping. Fails closed."""
    llm = raw.get("llm")
    if not isinstance(llm, dict) or "primary" not in llm:
        raise ConfigError("`.aorc.yml` must define llm.primary (provider + model)")

    primary = _parse_slot(llm["primary"], "primary")
    escalation = _parse_slot(llm["escalation"], "escalation") if llm.get("escalation") else None

    failure = raw.get("failure") or {}
    if not isinstance(failure, dict):
        raise ConfigError("`failure` must be a mapping")

    merge = raw.get("merge") or {}
    if not isinstance(merge, dict):
        raise ConfigError("`merge` must be a mapping")

    smoke = raw.get("smoke") or []
    if not isinstance(smoke, list):
        raise ConfigError("`smoke` must be a list of {input, expect} examples")

    coverage = raw.get("coverage") or {}
    if not isinstance(coverage, dict):
        raise ConfigError("`coverage` must be a mapping")

    dispatch = raw.get("dispatch") or {}
    if not isinstance(dispatch, dict):
        raise ConfigError("`dispatch` must be a mapping")

    clarification = raw.get("clarification") or {}
    if not isinstance(clarification, dict):
        raise ConfigError("`clarification` must be a mapping")

    return AorcConfig(
        primary=primary,
        escalation=escalation,
        setup=raw.get("setup"),
        test=raw.get("test"),
        lint=raw.get("lint"),
        smoke=smoke,
        coverage_command=coverage.get("command"),
        coverage_floor=float(coverage.get("floor", 80.0)),
        merge_auto=bool(merge.get("auto", False)),
        primary_attempts=int(failure.get("primary_attempts", 3)),
        escalation_attempts=int(failure.get("escalation_attempts", 1)),
        dispatch_concurrency=int(dispatch.get("concurrency", 5)),
        clarification_nudge_days=_parse_window(clarification.get("nudge_days"), 7.0),
        clarification_block_days=_parse_window(clarification.get("block_days"), 7.0),
        raw=raw,
    )
