"""The primary reviewer: plan -> verify with tools -> submit_findings.

The loop itself lives in `engine.py`; this module supplies the prompts, the submit tool,
and the interpretation of what the model submits (findings, escalations, grounding).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from string import Template
from typing import Any

from pydantic import ValidationError

from watchman_review import prompts
from watchman_review.agent.engine import LoopSpec, run_loop
from watchman_review.agent.grounding import DIFF_REF, GroundingRecord, apply_grounding
from watchman_review.agent.tools import ToolContext
from watchman_review.budget import ReviewBudget
from watchman_review.llm.types import LLMClient, Message, ToolSpec
from watchman_review.parsing import validate_items
from watchman_review.prompts import pitfalls
from watchman_review.schema import (
    Category,
    CheckResult,
    Escalation,
    Finding,
    Severity,
    Source,
    ToolTraceStep,
)

SUBMIT = "submit_findings"
MAX_INITIAL_DIFF_CHARS = 60_000
MAX_ESCALATIONS = 3

ESCALATION_NOTE = """

If, within your budget, you cannot resolve something that could matter (an unclear
caller, a risky change you couldn't verify), list it under `escalations` (at most 3,
each with the file, lines, and a concrete question). A senior reviewer may look at it.
Don't escalate things you can check yourself."""

# The agent doesn't emit `maintainability`; only the deterministic quality check does, so the
# model's tool schema (and with it every cached request) stays what it was.
AGENT_CATEGORIES = [c.value for c in Category if c is not Category.MAINTAINABILITY]

FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": AGENT_CATEGORIES},
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

_ESCALATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": {"type": ["string", "null"]},
        "line_start": {"type": "integer", "minimum": 0},
        "line_end": {"type": "integer", "minimum": 0},
        "question": {"type": "string"},
    },
    "required": ["question"],
    "additionalProperties": False,
}


def submit_spec(*, escalations: bool = False) -> ToolSpec:
    props: dict[str, Any] = {"findings": {"type": "array", "items": FINDING_SCHEMA}}
    if escalations:
        props["escalations"] = {
            "type": "array",
            "items": _ESCALATION_SCHEMA,
            "maxItems": MAX_ESCALATIONS,
        }
    return ToolSpec(
        name=SUBMIT,
        description="Submit your final review. Call exactly once. An empty list is valid.",
        parameters={"type": "object", "properties": props, "required": ["findings"]},
    )


SUBMIT_SPEC = submit_spec()


@dataclass
class AgentOutcome:
    findings: list[Finding]
    trace: list[ToolTraceStep]
    outputs: dict[str, str] = field(default_factory=dict)
    grounding: list[GroundingRecord] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    submitted: bool = False
    stop_reason: str = ""

    @property
    def incomplete(self) -> bool:
        """True if the model never delivered a final answer (error, budget, turn limit)."""
        return not self.submitted


def system_prompt(max_tool_calls: int, *, escalations: bool = False) -> str:
    return "\n\n".join(
        [
            Template(prompts.load("agent_system")).substitute(
                max_tool_calls=max_tool_calls,
                escalation_note=ESCALATION_NOTE if escalations else "",
            ),
            pitfalls.render(include_verification=True),
            prompts.load("severity_guide"),
        ]
    )


def layer1_summary(checks: list[CheckResult]) -> str:
    lines: list[str] = []
    for c in checks:
        lines.append(f"[ref:{c.id}] {c.name}: {c.status.value} - {c.summary}")
        for f in c.findings:
            loc = f"{f.file}:{f.line_start}" if f.file else "(change)"
            lines.append(f"    - {f.severity.value} {f.category.value} {loc}: {f.summary}")
    return "\n".join(lines)


def changed_files_summary(tctx: ToolContext) -> str:
    return "\n".join(
        f"  {f.status.value:8} {f.path} (+{len(f.added)}/-{len(f.removed)})"
        for f in tctx.diff.files
    )


def initial_user_message(
    tctx: ToolContext, checks: list[CheckResult] | None, *, notes: str | None = None
) -> str:
    d = tctx.diff
    raw = d.raw
    truncated = len(raw) > MAX_INITIAL_DIFF_CHARS
    parts: list[str] = []
    if tctx.task:
        parts.append("Stated task for this change:\n" + prompts.wrap_untrusted("task", tctx.task))
    else:
        parts.append("No task description was provided; judge scope from the change itself.")
    parts.append(f"Changed files ({len(d.files)}):\n{changed_files_summary(tctx)}")
    if checks is not None:
        parts.append(
            "Layer 1 deterministic results (already reported; don't repeat them):\n"
            + prompts.wrap_untrusted("layer1", layer1_summary(checks))
        )
    parts.append(
        f"[ref:{DIFF_REF}] Diff"
        + (" (truncated; use get_diff with a path for the rest)" if truncated else "")
        + ":\n"
        + prompts.wrap_untrusted("diff", raw[:MAX_INITIAL_DIFF_CHARS])
    )
    if notes:
        parts.append(
            "SECOND PASS. A senior reviewer (Watchman's overseer) checked your first review "
            "and left the notes below. They say where your first pass went wrong and what to "
            "look at again. Treat them as leads to verify with your tools, not as facts. "
            "Findings the overseer rejected must not come back unless you find new evidence.\n"
            + prompts.wrap_untrusted("overseer_notes", notes)
        )
    parts.append("Review the change now. Verify before you report, then call submit_findings.")
    return "\n\n".join(parts)


def parse_escalations(raw: Any) -> list[Escalation]:
    out: list[Escalation] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            q = str(item.get("question", ""))[:300]
            out.append(
                Escalation(
                    file=item.get("file") if isinstance(item.get("file"), str) else None,
                    line_start=max(0, int(item.get("line_start") or 0)),
                    line_end=max(0, int(item.get("line_end") or 0)),
                    question=q,
                )
            )
        except (ValidationError, TypeError, ValueError):
            continue
        if len(out) == MAX_ESCALATIONS:
            break
    return out


def run_agent(
    client: LLMClient,
    tctx: ToolContext,
    *,
    checks: list[CheckResult] | None,
    budget: ReviewBudget,
    max_tokens: int,
    temperature: float,
    seed: int | None,
    escalations: bool = False,
    notes: str | None = None,
    ref_prefix: str = "t",
) -> AgentOutcome:
    messages = [
        Message.system(system_prompt(budget.cfg.max_tool_calls, escalations=escalations)),
        Message.user(initial_user_message(tctx, checks, notes=notes)),
    ]
    spec = LoopSpec(
        label="agent", submit=submit_spec(escalations=escalations), ref_prefix=ref_prefix,
        required_key="findings",
    )  # fmt: skip
    res = run_loop(
        client, tctx, messages, spec, budget=budget, max_tokens=max_tokens,
        temperature=temperature, seed=seed,
    )  # fmt: skip
    outcome = AgentOutcome(
        findings=[], trace=res.trace, outputs=res.outputs, warnings=list(res.warnings),
        stop_reason=res.stop_reason,
    )  # fmt: skip
    if res.submitted_args is not None:
        raw = res.submitted_args.get("findings")
        parsed, errors = validate_items(raw if isinstance(raw, list) else [], Source.AGENT)
        outcome.findings = parsed
        outcome.warnings += [f"agent dropped invalid {e}" for e in errors]
        outcome.escalations = parse_escalations(res.submitted_args.get("escalations"))
        outcome.submitted = True

    check_ids = {c.id for c in checks or []}
    steps = {s.ref: s for s in res.trace}
    outcome.findings, outcome.grounding = apply_grounding(
        outcome.findings, steps, check_ids, res.outputs
    )
    return outcome
