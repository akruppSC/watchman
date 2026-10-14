"""Provider-neutral message and tool-call types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def tool_result(cls, call_id: str, content: str) -> Message:
        return cls(role=Role.TOOL, content=content, tool_call_id=call_id)


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(description="JSON Schema for the arguments object.")


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = Field(
        default=None, description="Provider-reported cost, if any (e.g. OpenRouter)."
    )
    model: str = ""
    stop_reason: str = ""
    cached: bool = False

    def as_message(self) -> Message:
        return Message(role=Role.ASSISTANT, content=self.content, tool_calls=self.tool_calls)


class CompletionRequest(BaseModel):
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.0
    seed: int | None = None


class LLMError(Exception):
    """Raised when the provider call fails after retries or returns unusable output."""


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def complete(self, request: CompletionRequest) -> LLMResponse: ...
