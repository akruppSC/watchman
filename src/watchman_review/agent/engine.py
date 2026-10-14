"""Generic tool-using loop shared by the primary agent and the overseer.

The loop is: model turn -> run requested tools (budgeted, refs numbered) -> repeat until
the model calls its submit tool. Budgets are enforced by ReviewBudget (tool calls) and
MeteredClient (tokens, cost, monthly cap). When a budget is nearly spent, the loop tells
the model to submit and refuses every other tool call, so a stage ends with a structured
answer, not an exception.

What differs between stages is only data: the submit tool, the ref prefix (`t` for the
primary reviewer, `o` for the overseer, `r` for the second pass), and how the submitted
arguments are interpreted. That lives in the callers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from watchman_review import prompts
from watchman_review.agent.tools import TOOLS, ToolContext, execute
from watchman_review.budget import BudgetExceeded, ReviewBudget
from watchman_review.llm.types import (
    CompletionRequest,
    LLMAuthError,
    LLMClient,
    LLMError,
    Message,
    ToolSpec,
)
from watchman_review.parsing import FindingsParseError, extract_json_object
from watchman_review.schema import ToolTraceStep

TRACE_EXCERPT_CHARS = 1_500
NEAR_LIMIT_FRACTION = 0.8
EXTRA_TURNS = 6  # turns beyond max_tool_calls (nudges, forced submit)


@dataclass
class LoopSpec:
    label: str  # "agent", "overseer", ... used in warnings
    submit: ToolSpec
    ref_prefix: str = "t"
    required_key: str = "findings"  # key a text-JSON fallback answer must contain
    first_ref: int = 1  # ref numbering starts here (lets stages share one namespace)
    extra_tools: list[ToolSpec] = field(default_factory=list)


@dataclass
class LoopResult:
    submitted_args: dict[str, Any] | None
    trace: list[ToolTraceStep]
    outputs: dict[str, str]
    warnings: list[str]
    stop_reason: str

    @property
    def submitted(self) -> bool:
        return self.submitted_args is not None


def near_limit(budget: ReviewBudget) -> bool:
    cfg = budget.cfg
    tokens_near = budget.total_tokens >= NEAR_LIMIT_FRACTION * cfg.max_tokens_per_review
    cost_near = bool(cfg.max_cost_per_review_usd) and (
        budget.usage.cost_usd >= NEAR_LIMIT_FRACTION * cfg.max_cost_per_review_usd
    )
    return tokens_near or cost_near


def run_loop(
    client: LLMClient,
    tctx: ToolContext,
    messages: list[Message],
    spec: LoopSpec,
    *,
    budget: ReviewBudget,
    max_tokens: int,
    temperature: float,
    seed: int | None,
    on_tool: Callable[[ToolTraceStep], None] | None = None,
) -> LoopResult:
    all_tools = [t.spec for t in TOOLS.values()] + spec.extra_tools + [spec.submit]
    trace: list[ToolTraceStep] = []
    outputs: dict[str, str] = {}
    warnings: list[str] = []
    stop_reason = ""
    submitted: dict[str, Any] | None = None
    forced = False
    nudged = False
    submit_name = spec.submit.name

    for _turn in range(budget.cfg.max_tool_calls + EXTRA_TURNS):
        if not forced and (budget.tool_calls_remaining() == 0 or near_limit(budget)):
            forced = True
            messages.append(
                Message.user(
                    f"Budget nearly exhausted. Call {submit_name} now with what you have verified."
                )
            )
        # Always offer every tool: some providers reject histories that reference tools
        # missing from the request. In forced mode non-submit calls are refused below.
        req = CompletionRequest(
            messages=messages,
            tools=all_tools,
            max_tokens=max_tokens,
            temperature=temperature,
            seed=seed,
        )
        try:
            resp = client.complete(req)
        except BudgetExceeded as e:
            warnings.append(f"{spec.label} stopped: {e}")
            stop_reason = "budget"
            break
        except LLMAuthError as e:
            warnings.append(f"{spec.label} stopped, LLM access problem: {e}")
            stop_reason = "llm_auth"
            break
        except LLMError as e:
            warnings.append(f"{spec.label} LLM error: {e}")
            stop_reason = "llm_error"
            break
        messages.append(resp.as_message())

        if not resp.tool_calls:
            # Some models answer in text despite instructions; accept JSON, else nudge once.
            try:
                obj = extract_json_object(resp.content)
                if spec.required_key not in obj:
                    raise FindingsParseError(f"missing {spec.required_key!r}")
            except FindingsParseError:
                if nudged:
                    warnings.append(f"{spec.label} ended without submitting")
                    stop_reason = "no_submit"
                    break
                nudged = True
                messages.append(Message.user(f"Call the {submit_name} tool to finish."))
                continue
            submitted, stop_reason = obj, "text_submit"
            break

        for call in resp.tool_calls:
            if call.name == submit_name:
                submitted = call.arguments
                messages.append(Message.tool_result(call.id, "received"))
                continue
            if submitted is not None or forced or budget.tool_calls_remaining() == 0:
                messages.append(
                    Message.tool_result(call.id, "error: tool budget exhausted; submit now")
                )
                continue
            budget.record_tool_call()
            ref = f"{spec.ref_prefix}{spec.first_ref + len(trace)}"
            start = time.monotonic()
            out, ok = execute(tctx, call.name, call.arguments)
            outputs[ref] = out
            step = ToolTraceStep(
                ref=ref,
                tool=call.name,
                arguments=call.arguments,
                ok=ok,
                output_excerpt=out[:TRACE_EXCERPT_CHARS],
                duration_s=round(time.monotonic() - start, 3),
            )
            trace.append(step)
            if on_tool:
                on_tool(step)
            content = f"[ref:{ref}]\n" + prompts.wrap_untrusted("tool_output", out)
            messages.append(Message.tool_result(call.id, content))

        if submitted is not None:
            stop_reason = "submitted"
            break
    else:
        warnings.append(f"{spec.label} hit the turn limit without submitting")
        stop_reason = "turn_limit"

    return LoopResult(submitted, trace, outputs, warnings, stop_reason)
