"""In-memory `LLMClient` for unit tests: scripted responses + call recording."""

from __future__ import annotations

from ..interfaces import Completion, LLMClient, Message


class MockLLMClient(LLMClient):
    """Returns queued `responses` in order, then falls back to `default`.

    Every call is recorded in `self.calls` as ``(messages, kwargs)`` so tests
    can assert exactly what the orchestrator sent across the seam.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        default: str = "",
        model: str = "mock-model",
    ) -> None:
        self._responses = list(responses or [])
        self._default = default
        self._model = model
        self.calls: list[tuple[list[Message], dict]] = []

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        self.calls.append((messages, {"max_tokens": max_tokens, "temperature": temperature}))
        text = self._responses.pop(0) if self._responses else self._default
        return Completion(text=text, model=self._model, finish_reason="stop")
