"""The tool-using reviewer loop: plan -> verify with tools -> submit_findings.

Budgets are enforced by ReviewBudget (tool calls) and MeteredClient (tokens, cost,
monthly cap). When a budget is nearly spent, the loop tells the model to submit and refuses
every other tool call, so the review ends with a structured answer, not an exception.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from string import Template
from typing import Any

from watchman_review import prompts
from watchman_review.agent.grounding import DIFF_REF, GroundingRecord, apply_grounding
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
from watchman_review.parsing import FindingsParseError, parse_findings, validate_items
from watchman_review.prompts import pitfalls
from watchman_review.schema import (
    Category,
    CheckResult,
    Finding,
    Severity,
    Source,
    ToolTraceStep,
)

SUBMIT = "submit_findings"
MAX_INITIAL_DIFF_CHARS = 60_000
TRACE_EXCERPT_CHARS = 1_500
NEAR_LIMIT_FRACTION = 0.8
EXTRA_TURNS = 6  # turns beyond max_tool_calls (nudges, forced submit)

_FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": [c.value for c in Category]},
        "severity": {"type": "string", "enum": [s.value for s in Severity]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "file": {"type": ["string", "null"]},
        "line_start": {"type": "integer", "minimum": 0},
        "line_end": {"type": "integer", "minimum": 0},
        "summary": {"type": "string"},
        "evidence": {"type": "string"},
        "suggested_fix": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "category", "severity", "confidence", "file", "line_start", "line_end", "summary",
        "evidence", "evidence_refs",
    ],
    "additionalProperties": False,
}  # fmt: skip

SUBMIT_SPEC = ToolSpec(
    name=SUBMIT,
    description="Submit your final review. Call exactly once. An empty list is valid.",
    parameters={
        "type": "object",
        "properties": {"findings": {"type": "array", "items": _FINDING_SCHEMA}},
        "required": ["findings"],
    },
)


@dataclass
class AgentOutcome:
    findings: list[Finding]
    trace: list[ToolTraceStep]
    grounding: list[GroundingRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    submitted: bool = False
    stop_reason: str = ""

    @property
    def incomplete(self) -> bool:
        """True if the model never delivered a final answer (error, budget, turn limit)."""
        return not self.submitted


def system_prompt(max_tool_calls: int) -> str:
    return "\n\n".join(
        [
            Template(prompts.load("agent_system")).substitute(max_tool_calls=max_tool_calls),
            pitfalls.render(include_verification=True),
            prompts.load("severity_guide"),
        ]
    )


def _layer1_summary(checks: list[CheckResult]) -> str:
    lines: list[str] = []
    for c in checks:
        lines.append(f"[ref:{c.id}] {c.name}: {c.status.value} - {c.summary}")
        for f in c.findings:
            loc = f"{f.file}:{f.line_start}" if f.file else "(change)"
            lines.append(f"    - {f.severity.value} {f.category.value} {loc}: {f.summary}")
    return "\n".join(lines)


def initial_user_message(tctx: ToolContext, checks: list[CheckResult] | None) -> str:
    d = tctx.diff
    files = "\n".join(
        f"  {f.status.value:8} {f.path} (+{len(f.added)}/-{len(f.removed)})" for f in d.files
    )
    raw = d.raw
    truncated = len(raw) > MAX_INITIAL_DIFF_CHARS
    parts: list[str] = []
    if tctx.task:
        parts.append("Stated task for this change:\n" + prompts.wrap_untrusted("task", tctx.task))
    else:
        parts.append("No task description was provided; judge scope from the change itself.")
    parts.append(f"Changed files ({len(d.files)}):\n{files}")
    if checks is not None:
        parts.append(
            "Layer 1 deterministic results (already reported; don't repeat them):\n"
            + prompts.wrap_untrusted("layer1", _layer1_summary(checks))
        )
    parts.append(
        f"[ref:{DIFF_REF}] Diff"
        + (" (truncated; use get_diff with a path for the rest)" if truncated else "")
        + ":\n"
        + prompts.wrap_untrusted("diff", raw[:MAX_INITIAL_DIFF_CHARS])
    )
    parts.append("Review the change now. Verify before you report, then call submit_findings.")
    return "\n\n".join(parts)


def _near_limit(budget: ReviewBudget) -> bool:
    cfg = budget.cfg
    tokens_near = budget.total_tokens >= NEAR_LIMIT_FRACTION * cfg.max_tokens_per_review
    cost_near = bool(cfg.max_cost_per_review_usd) and (
        budget.usage.cost_usd >= NEAR_LIMIT_FRACTION * cfg.max_cost_per_review_usd
    )
    return tokens_near or cost_near


def run_agent(
    client: LLMClient,
    tctx: ToolContext,
    *,
    checks: list[CheckResult] | None,
    budget: ReviewBudget,
    max_tokens: int,
    temperature: float,
    seed: int | None,
) -> AgentOutcome:
    all_tools = [t.spec for t in TOOLS.values()] + [SUBMIT_SPEC]
    messages = [
        Message.system(system_prompt(budget.cfg.max_tool_calls)),
        Message.user(initial_user_message(tctx, checks)),
    ]
    trace: list[ToolTraceStep] = []
    outputs: dict[str, str] = {}
    outcome = AgentOutcome(findings=[], trace=trace)
    forced = False
    nudged = False
    raw_findings: list[Any] | None = None

    for _turn in range(budget.cfg.max_tool_calls + EXTRA_TURNS):
        if not forced and (budget.tool_calls_remaining() == 0 or _near_limit(budget)):
            forced = True
            messages.append(
                Message.user(
                    "Budget nearly exhausted. Call submit_findings now with what you have verified."
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
            outcome.warnings.append(f"agent stopped: {e}")
            outcome.stop_reason = "budget"
            break
        except LLMAuthError as e:
            outcome.warnings.append(f"agent stopped, LLM access problem: {e}")
            outcome.stop_reason = "llm_auth"
            break
        except LLMError as e:
            outcome.warnings.append(f"agent LLM error: {e}")
            outcome.stop_reason = "llm_error"
            break
        messages.append(resp.as_message())

        if not resp.tool_calls:
            # Some models answer in text despite instructions; accept JSON, else nudge once.
            try:
                parsed, errors = parse_findings(resp.content, Source.AGENT)
            except FindingsParseError:
                if nudged:
                    outcome.warnings.append("agent ended without submitting findings")
                    outcome.stop_reason = "no_submit"
                    break
                nudged = True
                messages.append(Message.user("Call the submit_findings tool to finish."))
                continue
            outcome.findings = parsed
            outcome.warnings += [f"agent dropped invalid {e}" for e in errors]
            outcome.submitted = True
            outcome.stop_reason = "text_submit"
            break

        for call in resp.tool_calls:
            if call.name == SUBMIT:
                raw = call.arguments.get("findings")
                raw_findings = raw if isinstance(raw, list) else []
                messages.append(Message.tool_result(call.id, "received"))
                continue
            if raw_findings is not None or forced or budget.tool_calls_remaining() == 0:
                messages.append(
                    Message.tool_result(call.id, "error: tool budget exhausted; submit now")
                )
                continue
            budget.record_tool_call()
            ref = f"t{len(trace) + 1}"
            start = time.monotonic()
            out, ok = execute(tctx, call.name, call.arguments)
            outputs[ref] = out
            trace.append(
                ToolTraceStep(
                    ref=ref,
                    tool=call.name,
                    arguments=call.arguments,
                    ok=ok,
                    output_excerpt=out[:TRACE_EXCERPT_CHARS],
                    duration_s=round(time.monotonic() - start, 3),
                )
            )
            content = f"[ref:{ref}]\n" + prompts.wrap_untrusted("tool_output", out)
            messages.append(Message.tool_result(call.id, content))

        if raw_findings is not None:
            parsed, errors = validate_items(raw_findings, Source.AGENT)
            outcome.findings = parsed
            outcome.warnings += [f"agent dropped invalid {e}" for e in errors]
            outcome.submitted = True
            outcome.stop_reason = "submitted"
            break
    else:
        outcome.warnings.append("agent hit the turn limit without submitting")
        outcome.stop_reason = "turn_limit"

    check_ids = {c.id for c in checks or []}
    steps = {s.ref: s for s in trace}
    outcome.findings, outcome.grounding = apply_grounding(
        outcome.findings, steps, check_ids, outputs
    )
    return outcome
