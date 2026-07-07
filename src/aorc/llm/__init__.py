"""LLM seam: the `build_llm_client` factory selects an adapter from a config
model slot. Provider/model choice is driven entirely by `.aorc.yml`; no model
name is ever hardcoded here."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from ..config import ModelSlot
from ..interfaces import FailFastProviderError, LLMClient

# Providers that speak the native Anthropic contract. Everything else is
# reached through the OpenAI-compatible contract (hosted OpenAI, gateways, and
# local runtimes via a base_url).
_CLAUDE_PROVIDERS = {"claude", "anthropic"}

# Hostnames that resolve to the runner itself. On a GitHub-hosted (cloud)
# runner these point at the cloud VM, never at the user's machine running the
# model -- connection-refused there will never heal (PRD Local-LLM constraint).
_LOCAL_HOSTS = {"localhost", "0.0.0.0", "::1", "host.docker.internal"}

# The value GitHub Actions sets in RUNNER_ENVIRONMENT on its own runners.
_CLOUD_RUNNER = "github-hosted"


def is_local_base_url(base_url: str | None) -> bool:
    """Whether a configured `base_url` points at the local host."""
    if not base_url:
        return False
    host = (urlparse(base_url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.startswith("127.")


def assert_local_llm_reachable(
    slot: ModelSlot, runner_environment: str | None = None
) -> None:
    """S18's Local-LLM constraint: a local `base_url` configured on a cloud
    runner is a misconfiguration, not a transient provider error -- raise
    `FailFastProviderError` (never retried by S14's backoff) with a clear
    message. `runner_environment` defaults to `$RUNNER_ENVIRONMENT`, the
    variable GitHub Actions sets; anything other than "github-hosted"
    (self-hosted runners, local development) is treated as sharing the
    model's host."""
    if runner_environment is None:
        runner_environment = os.environ.get("RUNNER_ENVIRONMENT", "")
    if is_local_base_url(slot.base_url) and runner_environment == _CLOUD_RUNNER:
        raise FailFastProviderError(
            f"llm base_url {slot.base_url!r} points at the local host, but this "
            "orchestrator is running on a GitHub-hosted (cloud) runner where "
            "that address resolves to the runner itself -- the model is not "
            "reachable and retrying cannot fix it. Run AORC on a self-hosted "
            "runner sharing the model's host, or configure a hosted provider."
        )


def build_llm_client(slot: ModelSlot, *, runner_environment: str | None = None) -> LLMClient:
    """Construct the right `LLMClient` adapter for a configured model slot,
    failing fast on the local-model-on-cloud-runner misconfiguration first."""
    assert_local_llm_reachable(slot, runner_environment)
    provider = slot.provider.lower()
    if provider in _CLAUDE_PROVIDERS:
        from .claude_adapter import ClaudeLLMClient

        return ClaudeLLMClient(model=slot.model, api_key=slot.api_key, base_url=slot.base_url)

    from .openai_adapter import OpenAICompatibleLLMClient

    return OpenAICompatibleLLMClient(
        model=slot.model, api_key=slot.api_key, base_url=slot.base_url
    )


__all__ = ["assert_local_llm_reachable", "build_llm_client", "is_local_base_url"]
