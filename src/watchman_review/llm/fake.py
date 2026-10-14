"""Scripted LLM client for tests and offline demos."""

from __future__ import annotations

from collections.abc import Callable

from watchman_review.llm.types import CompletionRequest, LLMError, LLMResponse

Responder = Callable[[CompletionRequest], LLMResponse]


class FakeClient:
    """Returns scripted responses in order, or delegates to a responder function."""

    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        *,
        responder: Responder | None = None,
        model: str = "fake-model",
    ) -> None:
        self._responses = list(responses or [])
        self._responder = responder
        self._model = model
        self.requests: list[CompletionRequest] = []

    @property
    def model(self) -> str:
        return self._model

    def complete(self, request: CompletionRequest) -> LLMResponse:
        self.requests.append(request)
        if self._responder is not None:
            return self._responder(request)
        if not self._responses:
            raise LLMError("FakeClient: no scripted responses left")
        return self._responses.pop(0)
