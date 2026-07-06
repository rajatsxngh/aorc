"""`LLMClient` adapter for Anthropic Claude via the Anthropic SDK.

The SDK is imported lazily so the orchestrator core and its tests run with zero
third-party deps; only an actual `.complete()` call touches `anthropic`.
"""

from __future__ import annotations

from ..interfaces import Completion, LLMClient, Message, ProviderError


class ClaudeLLMClient(LLMClient):
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._client = None  # constructed lazily on first use

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str | None:
        return self._base_url

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # lazy: only when a real call is made

            kwargs: dict = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        import anthropic  # lazy: only when a real call is made

        client = self._ensure_client()
        # Anthropic takes the system prompt as a separate arg, not a turn.
        system = "\n".join(m.content for m in messages if m.role == "system")
        convo = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        create_kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": convo,
        }
        if system:
            create_kwargs["system"] = system
        # S14 seam error contract: transient transport/429/5xx failures become
        # `ProviderError` so orchestrator backoff can retry the same model.
        # Anything else (400 bad request, 401 auth) is not transient and
        # propagates untouched. NOT integration-tested here (mocks only) --
        # exercising the real SDK error paths is S19's job.
        try:
            resp = client.messages.create(**create_kwargs)
        except anthropic.APIConnectionError as exc:  # incl. timeouts
            raise ProviderError(f"connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise ProviderError(f"provider {exc.status_code}: {exc}") from exc
            raise
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        return Completion(
            text=text,
            model=self._model,
            finish_reason=getattr(resp, "stop_reason", None),
            raw=resp,
        )
