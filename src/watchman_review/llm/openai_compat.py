"""OpenAI-compatible chat-completions client (OpenAI, OpenRouter, Ollama)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from watchman_review.llm.http import post_json
from watchman_review.llm.ratelimit import RateLimiter
from watchman_review.llm.types import (
    CompletionRequest,
    LLMError,
    LLMResponse,
    Message,
    Role,
    ToolCall,
)


class OpenAICompatClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        extra_headers: dict[str, str] | None = None,
        timeout_s: float = 120.0,
        is_openrouter: bool = False,
        http: httpx.Client | None = None,
        retry_backoff_s: float = 1.0,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._is_openrouter = is_openrouter
        self._http = http or httpx.Client(timeout=timeout_s)
        self._backoff = retry_backoff_s
        self._limiter = limiter

    @property
    def model(self) -> str:
        return self._model

    def complete(self, request: CompletionRequest) -> LLMResponse:
        messages = [_to_wire(m) for m in request.messages]
        if self._is_openrouter and self._model.startswith("anthropic/"):
            add_cache_breakpoints(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if self._is_openrouter:
            payload["usage"] = {"include": True}
        data = post_json(
            self._http,
            self._url,
            payload,
            self._headers,
            backoff_s=self._backoff,
            limiter=self._limiter,
        )
        return _from_wire(data, self._model)


def add_cache_breakpoints(messages: list[dict[str, Any]]) -> None:
    """Mark the system prompt and the newest message as cacheable (Anthropic via OpenRouter).

    Anthropic only caches at explicit breakpoints. Agent turns resend the whole growing
    history, so caching the prefix bills it at roughly 10% on every following turn. Older
    messages are never modified, which keeps the cached prefix byte-identical.
    """
    for idx in {0, len(messages) - 1}:
        if not 0 <= idx < len(messages):
            continue
        content = messages[idx].get("content")
        if isinstance(content, str) and content:
            messages[idx]["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]


def _to_wire(m: Message) -> dict[str, Any]:
    if m.role is Role.TOOL:
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict[str, Any] = {"role": m.role.value, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in m.tool_calls
        ]
    return out


def _from_wire(data: dict[str, Any], model: str) -> LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"no choices in response: {str(data)[:300]}")
    choice = choices[0]
    msg = choice.get("message") or {}
    calls: list[ToolCall] = []
    for raw in msg.get("tool_calls") or []:
        fn = raw.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except json.JSONDecodeError:
            # Surface the malformed call to the agent loop rather than crashing it.
            args = {"__invalid_json__": args_raw}
        calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{len(calls)}",
                name=fn.get("name", ""),
                arguments=args if isinstance(args, dict) else {"value": args},
            )
        )
    usage = data.get("usage") or {}
    cost = usage.get("cost")
    details = usage.get("prompt_tokens_details") or {}
    return LLMResponse(
        content=msg.get("content") or "",
        tool_calls=calls,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cached_input_tokens=int(details.get("cached_tokens") or 0),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        model=str(data.get("model") or model),
        stop_reason=str(choice.get("finish_reason") or ""),
    )
