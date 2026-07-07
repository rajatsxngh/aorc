"""S19 -- real-adapter smoke: one real `complete()` per LLM adapter.

These hit live provider endpoints. They are gated on the SDK extra being
installed AND the relevant credentials/model env vars being set; anything
missing means a clean skip, never a failure, so the zero-dep unit suite and
credential-less environments are unaffected.

Model names come from the environment (never hardcoded -- invariant #2):

- ``ANTHROPIC_API_KEY`` + ``AORC_IT_CLAUDE_MODEL``          -> Claude adapter
- ``OPENAI_API_KEY``    + ``AORC_IT_OPENAI_MODEL``          -> OpenAI adapter (hosted)
- ``AORC_IT_LOCAL_LLM_BASE_URL`` + ``AORC_IT_LOCAL_LLM_MODEL``
  (optional ``AORC_IT_LOCAL_LLM_API_KEY``)                  -> OpenAI adapter (local runtime)
"""

from __future__ import annotations

import os

import pytest

from aorc.interfaces import Message

pytestmark = pytest.mark.integration

_PROMPT = [Message(role="user", content="Reply with exactly one word: pong")]


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


def test_claude_adapter_real_complete():
    pytest.importorskip("anthropic", reason="anthropic extra not installed")
    _require_env("ANTHROPIC_API_KEY")  # the SDK reads it from the environment
    model = _require_env("AORC_IT_CLAUDE_MODEL")
    from aorc.llm.claude_adapter import ClaudeLLMClient

    completion = ClaudeLLMClient(model).complete(_PROMPT, max_tokens=16)

    assert completion.text.strip()
    assert completion.model == model
    assert completion.raw is not None


def test_openai_adapter_real_complete_hosted():
    pytest.importorskip("openai", reason="openai extra not installed")
    _require_env("OPENAI_API_KEY")  # the SDK reads it from the environment
    model = _require_env("AORC_IT_OPENAI_MODEL")
    from aorc.llm.openai_adapter import OpenAICompatibleLLMClient

    completion = OpenAICompatibleLLMClient(model).complete(_PROMPT, max_tokens=16)

    assert completion.text.strip()
    assert completion.model == model


def test_openai_adapter_real_complete_local_base_url():
    """The same adapter against a local OpenAI-compatible runtime
    (Ollama / LM Studio / vLLM) via ``base_url`` -- the self-hosted path the
    PRD's local-model story depends on."""
    pytest.importorskip("openai", reason="openai extra not installed")
    base_url = _require_env("AORC_IT_LOCAL_LLM_BASE_URL")
    model = _require_env("AORC_IT_LOCAL_LLM_MODEL")
    from aorc.llm.openai_adapter import OpenAICompatibleLLMClient

    client = OpenAICompatibleLLMClient(
        model,
        # Local runtimes ignore the key but the SDK requires one to be present.
        # `or` (not a .get default): CI passes unset vars through as "".
        api_key=os.environ.get("AORC_IT_LOCAL_LLM_API_KEY") or "unused-local-key",
        base_url=base_url,
    )
    completion = client.complete(_PROMPT, max_tokens=16)

    assert completion.text.strip()
    assert client.base_url == base_url
