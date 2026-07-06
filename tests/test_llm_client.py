"""S1 — `LLMClient` seam: one contract, multiple adapters, plus a mock.

All orchestrator logic talks to `LLMClient`; adapters (Claude/OpenAI/local)
are constructed from `.aorc.yml` model slots via the factory. Adapters
lazy-import their SDK so the core + tests run with zero third-party deps.
"""

from aorc.config import ModelSlot
from aorc.interfaces import Completion, LLMClient, Message
from aorc.llm import build_llm_client
from aorc.llm.claude_adapter import ClaudeLLMClient
from aorc.llm.mock import MockLLMClient
from aorc.llm.openai_adapter import OpenAICompatibleLLMClient


def test_mock_satisfies_contract_and_records_calls():
    mock = MockLLMClient(responses=["hello", "world"])
    assert isinstance(mock, LLMClient)

    out1 = mock.complete([Message(role="user", content="hi")])
    out2 = mock.complete([Message(role="user", content="again")])

    assert isinstance(out1, Completion)
    assert out1.text == "hello"
    assert out2.text == "world"
    assert len(mock.calls) == 2
    assert mock.calls[0][0][0].content == "hi"


def test_mock_default_response_when_exhausted():
    mock = MockLLMClient(default="fallback")
    assert mock.complete([Message(role="user", content="x")]).text == "fallback"


def test_all_adapters_share_one_contract():
    for adapter in (ClaudeLLMClient, OpenAICompatibleLLMClient, MockLLMClient):
        assert issubclass(adapter, LLMClient)


def test_factory_selects_claude_adapter():
    slot = ModelSlot(provider="claude", model="cfg-model-x", api_key="k")
    client = build_llm_client(slot)
    assert isinstance(client, ClaudeLLMClient)
    # Model comes straight from config — never hardcoded.
    assert client.model == "cfg-model-x"


def test_factory_selects_openai_adapter():
    slot = ModelSlot(provider="openai", model="cfg-model-y", api_key="k")
    client = build_llm_client(slot)
    assert isinstance(client, OpenAICompatibleLLMClient)
    assert client.model == "cfg-model-y"


def test_factory_routes_local_provider_through_openai_compatible_with_base_url():
    slot = ModelSlot(provider="ollama", model="local-z", base_url="http://host.docker.internal:11434")
    client = build_llm_client(slot)
    # Any OpenAI-compatible endpoint (incl. local) → the OpenAI-compatible adapter.
    assert isinstance(client, OpenAICompatibleLLMClient)
    assert client.base_url == "http://host.docker.internal:11434"


def test_adapters_construct_without_sdk_installed():
    # Lazy SDK import: constructing an adapter must not require the SDK to be
    # present (only an actual .complete() call would).
    ClaudeLLMClient(model="m")
    OpenAICompatibleLLMClient(model="m", base_url="http://localhost:1234")
