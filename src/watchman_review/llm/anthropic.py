"""Native Anthropic Messages API client."""

from __future__ import annotations

from typing import Any

import httpx

from watchman_review.llm.http import post_json
from watchman_review.llm.types import (
    CompletionRequest,
    LLMResponse,
    Message,
    Role,
    ToolCall,
)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_s: float = 120.0,
        http: httpx.Client | None = None,
        retry_backoff_s: float = 1.0,
    ) -> None:
        self._model = model
        self._url = f"{base_url.rstrip('/')}/v1/messages"
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        self._http = http or httpx.Client(timeout=timeout_s)
        self._backoff = retry_backoff_s

    @property
    def model(self) -> str:
        return self._model

    def complete(self, request: CompletionRequest) -> LLMResponse:
        system = "\n\n".join(m.content for m in request.messages if m.role is Role.SYSTEM)
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": _to_wire(request.messages),
        }
        if system:
            payload["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        _cache_last_message(payload["messages"])
        if request.tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
        data = post_json(self._http, self._url, payload, self._headers, backoff_s=self._backoff)
        return _from_wire(data, self._model)


def _cache_last_message(wire: list[dict[str, Any]]) -> None:
    """Cache breakpoint on the newest message, so the growing history is billed at the
    cache-read rate on the next turn."""
    if not wire:
        return
    last = wire[-1]
    content = last["content"]
    if isinstance(content, str):
        if content:
            last["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
    elif isinstance(content, list) and content:
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}


def _to_wire(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert to Anthropic format. Consecutive tool results merge into one user turn."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role is Role.SYSTEM:
            continue
        if m.role is Role.TOOL:
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if m.role is Role.ASSISTANT and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": m.role.value, "content": m.content})
    return out


def _from_wire(data: dict[str, Any], model: str) -> LLMResponse:
    text: list[str] = []
    calls: list[ToolCall] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            raw_input = block.get("input")
            calls.append(
                ToolCall(
                    id=block.get("id", f"call_{len(calls)}"),
                    name=block.get("name", ""),
                    arguments=raw_input if isinstance(raw_input, dict) else {},
                )
            )
    usage = data.get("usage") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    return LLMResponse(
        content="".join(text),
        tool_calls=calls,
        # Anthropic reports uncached input separately; normalize to total input tokens.
        input_tokens=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
        output_tokens=int(usage.get("output_tokens") or 0),
        cached_input_tokens=cache_read,
        model=str(data.get("model") or model),
        stop_reason=str(data.get("stop_reason") or ""),
    )
