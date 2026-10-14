from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from watchman_review.budget import BudgetExceeded, Ledger, MeteredClient, ReviewBudget
from watchman_review.config import BudgetConfig, LLMConfig
from watchman_review.llm import Message, ToolCall, ToolSpec
from watchman_review.llm.anthropic import AnthropicClient
from watchman_review.llm.cache import CacheMode, CachingClient
from watchman_review.llm.catalog import list_models
from watchman_review.llm.fake import FakeClient
from watchman_review.llm.openai_compat import OpenAICompatClient
from watchman_review.llm.types import (
    CompletionRequest,
    LLMAuthError,
    LLMError,
    LLMResponse,
    Role,
)

TOOL = ToolSpec(
    name="read_file",
    description="Read a file",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


def mock_http(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_compat_roundtrip_with_tool_calls() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path": "a.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
            },
        )

    client = OpenAICompatClient(
        model="m", base_url="https://x/v1", api_key="k", is_openrouter=True, http=mock_http(handler)
    )
    history = [
        Message.system("sys"),
        Message.user("hi"),
        Message(role=Role.ASSISTANT, tool_calls=[ToolCall(id="c0", name="read_file")]),
        Message.tool_result("c0", "contents"),
    ]
    resp = client.complete(CompletionRequest(messages=history, tools=[TOOL], seed=3))

    assert resp.tool_calls == [ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})]
    assert (resp.input_tokens, resp.output_tokens, resp.cost_usd) == (10, 5, 0.001)
    assert seen["auth"] == "Bearer k"
    body = seen["body"]
    assert body["seed"] == 3
    assert body["usage"] == {"include": True}
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "contents"}
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_openai_compat_malformed_tool_args_are_preserved() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        call = {"id": "c", "function": {"name": "t", "arguments": "{not json"}}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})

    client = OpenAICompatClient(
        model="m", base_url="https://x", api_key=None, http=mock_http(handler)
    )
    resp = client.complete(CompletionRequest(messages=[Message.user("x")]))
    assert resp.tool_calls[0].arguments == {"__invalid_json__": "{not json"}


def test_http_non_retryable_error_raises_immediately() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "bad key"})

    client = OpenAICompatClient(
        model="m", base_url="https://x", api_key="k", http=mock_http(handler)
    )
    with pytest.raises(LLMAuthError, match="invalid API key"):
        client.complete(CompletionRequest(messages=[Message.user("x")]))
    assert calls == 1


def test_http_retries_then_succeeds() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        model="m", base_url="https://x", api_key="k", http=mock_http(handler), retry_backoff_s=0
    )
    assert client.complete(CompletionRequest(messages=[Message.user("x")])).content == "ok"
    assert calls == 3


def test_anthropic_wire_format() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["key"] = req.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "model": "claude-x",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "b"}},
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        )

    client = AnthropicClient(
        model="claude-x", base_url="https://a", api_key="k", http=mock_http(handler)
    )
    history = [
        Message.system("rules"),
        Message.user("review"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="a", name="read_file"), ToolCall(id="b", name="read_file")],
        ),
        Message.tool_result("a", "A"),
        Message.tool_result("b", "B"),
    ]
    resp = client.complete(CompletionRequest(messages=history, tools=[TOOL]))
    body = seen["body"]
    assert seen["key"] == "k"
    assert body["system"] == [
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}
    ]
    # The newest message carries a cache breakpoint so the next turn reads the prefix.
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["input_schema"]["type"] == "object"
    # Two tool results must be merged into a single user turn.
    assert len(body["messages"]) == 3
    assert [b["tool_use_id"] for b in body["messages"][2]["content"]] == ["a", "b"]
    assert resp.content == "checking"
    assert resp.tool_calls[0].arguments == {"path": "b"}


def test_cache_records_then_replays(tmp_path: Path) -> None:
    fake = FakeClient([LLMResponse(content="first", input_tokens=1)])
    req = CompletionRequest(messages=[Message.user("q")])
    rw = CachingClient(fake, tmp_path, CacheMode.READ_WRITE)
    assert rw.complete(req).cached is False
    replay = CachingClient(FakeClient([]), tmp_path, CacheMode.REPLAY)
    again = replay.complete(req)
    assert again.content == "first"
    assert again.cached is True
    with pytest.raises(LLMError, match="cache miss"):
        replay.complete(CompletionRequest(messages=[Message.user("other")]))


def test_metered_client_enforces_token_budget_and_records_ledger(tmp_path: Path) -> None:
    budget = ReviewBudget(BudgetConfig(max_tokens_per_review=100))
    ledger = Ledger(tmp_path / "l.jsonl")
    fake = FakeClient([LLMResponse(input_tokens=80, output_tokens=30)] * 2)
    llm = LLMConfig(model="m", input_cost_per_mtok=1.0, output_cost_per_mtok=2.0)
    client = MeteredClient(fake, llm_cfg=llm, budget=budget, ledger=ledger)
    client.complete(CompletionRequest(messages=[Message.user("x")]))
    assert budget.usage.cost_usd == pytest.approx((80 * 1 + 30 * 2) / 1e6)
    assert ledger.month_total() == pytest.approx(budget.usage.cost_usd)
    with pytest.raises(BudgetExceeded, match="token"):
        client.complete(CompletionRequest(messages=[Message.user("x")]))


def test_metered_client_enforces_monthly_cap(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl")
    llm = LLMConfig(model="m")
    budget = ReviewBudget(BudgetConfig(monthly_cap_usd=0.01))
    first = MeteredClient(
        FakeClient([LLMResponse(cost_usd=0.02)]), llm_cfg=llm, budget=budget, ledger=ledger
    )
    first.complete(CompletionRequest(messages=[Message.user("x")]))
    second = MeteredClient(
        FakeClient([LLMResponse()]),
        llm_cfg=llm,
        budget=ReviewBudget(BudgetConfig(monthly_cap_usd=0.01)),
        ledger=ledger,
    )
    with pytest.raises(BudgetExceeded, match="monthly"):
        second.complete(CompletionRequest(messages=[Message.user("x")]))


def test_cached_responses_do_not_hit_ledger(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl")
    client = MeteredClient(
        FakeClient([LLMResponse(cost_usd=1.0, cached=True)]),
        llm_cfg=LLMConfig(model="m"),
        budget=ReviewBudget(BudgetConfig()),
        ledger=ledger,
    )
    client.complete(CompletionRequest(messages=[Message.user("x")]))
    assert ledger.month_total() == 0.0


def test_tool_call_budget() -> None:
    b = ReviewBudget(BudgetConfig(max_tool_calls=1))
    b.record_tool_call()
    with pytest.raises(BudgetExceeded):
        b.record_tool_call()


def test_openrouter_catalog_parses_pricing() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"id": "a/b", "pricing": {"prompt": "0.000003", "completion": "0.000015"}}]
            },
        )

    models = list_models(LLMConfig(), None, http=mock_http(handler))
    assert models[0].id == "a/b"
    assert models[0].input_cost_per_mtok == pytest.approx(3.0)
    assert models[0].output_cost_per_mtok == pytest.approx(15.0)


def test_catalog_pricing_has_no_float_noise() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        pricing = {"prompt": "0.0000001", "completion": "0.0000005"}
        return httpx.Response(200, json={"data": [{"id": "m", "pricing": pricing}]})

    [m] = list_models(LLMConfig(), None, http=mock_http(handler))
    assert (m.input_cost_per_mtok, m.output_cost_per_mtok) == (0.1, 0.5)


def test_openrouter_anthropic_models_get_cache_breakpoints() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 1,
                          "prompt_tokens_details": {"cached_tokens": 80}},
            },
        )  # fmt: skip

    msgs = [Message.system("sys"), Message.user("u1"), Message.user("u2")]
    for model in ("anthropic/claude-x", "openai/gpt-5"):
        client = OpenAICompatClient(
            model=model, base_url="https://x", api_key="k", is_openrouter=True,
            http=mock_http(handler),
        )  # fmt: skip
        resp = client.complete(CompletionRequest(messages=msgs))
        assert resp.cached_input_tokens == 80
    anthropic_body, openai_body = seen
    first, middle, last = anthropic_body["messages"]
    assert first["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert middle["content"] == "u1"  # older messages untouched -> stable cached prefix
    assert last["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(isinstance(m["content"], str) for m in openai_body["messages"])


def test_anthropic_input_tokens_include_cache() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "x"}],
                  "usage": {"input_tokens": 10, "output_tokens": 2,
                            "cache_read_input_tokens": 900, "cache_creation_input_tokens": 90}},
        )  # fmt: skip

    client = AnthropicClient(model="c", base_url="https://a", api_key="k", http=mock_http(handler))
    resp = client.complete(CompletionRequest(messages=[Message.user("q")]))
    assert (resp.input_tokens, resp.cached_input_tokens) == (1000, 900)


def _client_for(handler: Any) -> OpenAICompatClient:
    return OpenAICompatClient(
        model="m", base_url="https://x", api_key="k", http=mock_http(handler), retry_backoff_s=0
    )


def test_200_with_embedded_timeout_is_retried_not_treated_as_empty() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            body = {"id": "g", "error": {"message": "A Timeout Occurred", "code": 504}}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    resp = _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")]))
    assert (resp.content, calls) == ("ok", 3)


def test_200_with_persistent_embedded_error_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "overloaded", "code": 529}})

    with pytest.raises(LLMError, match="after 4 attempts"):
        _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")]))


@pytest.mark.parametrize(("code", "hint"), [(401, "invalid API key"), (402, "out of credits")])
def test_auth_and_credit_errors_fail_fast_even_inside_200(code: int, hint: str) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error": {"message": "nope", "code": code}})

    with pytest.raises(LLMAuthError, match=hint):
        _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")]))
    assert calls == 1


def test_non_json_200_is_retried() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="<html>gateway</html>")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    assert (
        _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")])).content
        == "ok"
    )


def test_rate_limiter_spaces_requests_within_the_window() -> None:
    from watchman_review.llm.ratelimit import RateLimiter

    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    lim = RateLimiter(3, clock=lambda: now[0], sleep=sleep)
    for t in (0.0, 1.0, 2.0):
        now[0] = t
        lim.acquire()
    assert slept == []  # three requests fit in the window
    now[0] = 3.0
    lim.acquire()  # the fourth must wait for the first to age out at t=60
    assert slept and slept[0] == pytest.approx(57.0)
    assert now[0] == pytest.approx(60.0)


def test_limiter_registry_shares_per_key_and_is_off_without_a_limit() -> None:
    from watchman_review.llm.ratelimit import limiter_for

    assert limiter_for("k1", None) is None
    assert limiter_for("k1", 10) is limiter_for("k1", 10)
    assert limiter_for("k1", 10) is not limiter_for("k2", 10)


def test_429_waits_for_the_providers_reset_time(sleeps: list[float]) -> None:
    import time as _time

    calls = 0
    reset_ms = int((_time.time() + 30) * 1000)

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            body = {
                "error": {
                    "code": 429,
                    "message": "slow down",
                    "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}},
                }
            }
            return httpx.Response(429, json=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    resp = _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")]))
    assert resp.content == "ok"
    assert 25 <= sleeps[0] <= 31  # waited for the reset, not a fixed 1-2 second backoff


def test_429_honors_retry_after_header_and_caps_the_wait(sleeps: list[float]) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        if calls == 2:
            return httpx.Response(429, headers={"retry-after": "9999"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")]))
    assert sleeps == [3.0, 65.0]


def test_429_inside_a_200_is_treated_as_rate_limited(sleeps: list[float]) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"error": {"code": 429, "message": "rate limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    assert (
        _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")])).content
        == "ok"
    )
    assert len(sleeps) == 1 and sleeps[0] >= 1.0


def test_persistent_429_gives_up_with_a_clear_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": 429, "message": "no"}})

    with pytest.raises(LLMError, match="rate limited 9 times"):
        _client_for(handler).complete(CompletionRequest(messages=[Message.user("x")]))


def test_client_waits_on_the_limiter_before_each_request() -> None:
    from watchman_review.llm.ratelimit import RateLimiter

    acquired = 0

    class Counting(RateLimiter):
        def acquire(self) -> None:
            nonlocal acquired
            acquired += 1

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        model="m", base_url="https://x", api_key="k", http=mock_http(handler), limiter=Counting(5)
    )
    client.complete(CompletionRequest(messages=[Message.user("x")]))
    client.complete(CompletionRequest(messages=[Message.user("y")]))
    assert acquired == 2
