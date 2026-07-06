"""LLM seam: the `build_llm_client` factory selects an adapter from a config
model slot. Provider/model choice is driven entirely by `.aorc.yml`; no model
name is ever hardcoded here."""

from __future__ import annotations

from ..config import ModelSlot
from ..interfaces import LLMClient

# Providers that speak the native Anthropic contract. Everything else is
# reached through the OpenAI-compatible contract (hosted OpenAI, gateways, and
# local runtimes via a base_url).
_CLAUDE_PROVIDERS = {"claude", "anthropic"}


def build_llm_client(slot: ModelSlot) -> LLMClient:
    """Construct the right `LLMClient` adapter for a configured model slot."""
    provider = slot.provider.lower()
    if provider in _CLAUDE_PROVIDERS:
        from .claude_adapter import ClaudeLLMClient

        return ClaudeLLMClient(model=slot.model, api_key=slot.api_key, base_url=slot.base_url)

    from .openai_adapter import OpenAICompatibleLLMClient

    return OpenAICompatibleLLMClient(
        model=slot.model, api_key=slot.api_key, base_url=slot.base_url
    )


__all__ = ["build_llm_client"]
