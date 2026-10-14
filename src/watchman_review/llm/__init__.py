"""Provider-agnostic LLM access."""

from __future__ import annotations

from watchman_review.config import LLMConfig, Provider, resolve_api_key
from watchman_review.llm.anthropic import AnthropicClient
from watchman_review.llm.openai_compat import OpenAICompatClient
from watchman_review.llm.types import (
    CompletionRequest,
    LLMClient,
    LLMError,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    ToolSpec,
)

__all__ = [
    "CompletionRequest",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "Message",
    "Role",
    "ToolCall",
    "ToolSpec",
    "build_client",
]


def build_client(cfg: LLMConfig) -> LLMClient:
    if not cfg.model:
        raise LLMError("no model configured; run `watchman setup` or pass --model")
    key = resolve_api_key(cfg)
    base = cfg.resolved_base_url()
    if cfg.provider is Provider.ANTHROPIC:
        if not key:
            raise LLMError("Anthropic provider requires an API key")
        return AnthropicClient(model=cfg.model, base_url=base, api_key=key, timeout_s=cfg.timeout_s)
    headers: dict[str, str] = {}
    if cfg.provider is Provider.OPENROUTER:
        headers = {
            "HTTP-Referer": "https://github.com/akruppSC/watchman",
            "X-Title": "Watchman",
        }
    return OpenAICompatClient(
        model=cfg.model,
        base_url=base,
        api_key=key,
        extra_headers=headers,
        timeout_s=cfg.timeout_s,
        is_openrouter=cfg.provider is Provider.OPENROUTER,
    )
