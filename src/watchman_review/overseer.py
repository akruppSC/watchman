"""The overseer: a larger model that supervises the primary reviewer.

Pipeline position: Layer 1 -> primary agent -> [overseer] -> [second primary pass] -> filters.

1. Triggers decide whether the review looks risky enough to spend the larger model on.
2. The overseer gets compact evidence packets for the primary's findings (code excerpt plus
   the tool output the primary cited), spot-checks each one (confirm / reject / downgrade),
   digs into anything the primary escalated, may raise new findings (grounded like any
   other), and writes notes for the primary reviewer.
3. If it rejected or added many findings, the notes go back to the primary model for one
   more pass with a fresh tool budget. Findings the overseer rejected stay rejected.

Everything the overseer reads is untrusted data: the diff, code, tool output, and the
primary reviewer's own text (which was written after reading untrusted code).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from string import Template
from typing import Any

from watchman_review import prompts
from watchman_review.agent.engine import LoopSpec, run_loop
from watchman_review.agent.grounding import apply_grounding
from watchman_review.agent.loop import (
    FINDING_SCHEMA,
    AgentOutcome,
    changed_files_summary,
    layer1_summary,
    run_agent,
)
from watchman_review.agent.tools import ToolContext, ToolError, resolve_in_tree
from watchman_review.budget import ReviewBudget
from watchman_review.config import Config, OverseerConfig, OverseerTrigger
from watchman_review.gitutil import Diff
from watchman_review.llm.types import LLMClient, Message, ToolSpec
from watchman_review.parsing import validate_items
from watchman_review.postprocess import lines_near
from watchman_review.prompts import pitfalls
from watchman_review.schema import (
    CheckResult,
    CheckStatus,
    Escalation,
    Finding,
    Severity,
    Source,
    StageSummary,
    ToolTraceStep,
    Usage,
)

SUBMIT_OVERSIGHT = "submit_oversight"
MAX_DIFF_CHARS = 40_000
EXCERPT_CONTEXT = 12
MAX_EXCERPT_LINES = 60
MAX_CITED_OUTPUT_CHARS = 600
MAX_NOTES_CHARS = 1_500
_SEVERITY_ORDER = [Severity.BLOCKER, Severity.MAJOR, Severity.MINOR, Severity.NIT]

# Added lines matching this are security-sensitive enough to warrant a closer look.
SENSITIVE_CODE = re.compile(
    r"\b(subprocess|os\.system|os\.popen|eval|exec|pickle|marshal|shelve|"
    r"yaml\.(?:unsafe_)?load|shell\s*=\s*True|verify\s*=\s*False|check_hostname|"
    r"hashlib|hmac|secrets\.|jwt|oauth|password|passwd|credential|crypt\w*|"
    r"sqlite3|\.execute\(|\.executemany\(|tempfile|chmod|chown)\b",
    re.IGNORECASE,
)
SENSITIVE_PATH = re.compile(r"(auth|login|session|crypto|secur|permission|token)", re.IGNORECASE)

_OVERSIGHT_SPEC = ToolSpec(
    name=SUBMIT_OVERSIGHT,
    description="Submit your oversight of the primary review. Call exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "F1, F2, ..."},
                        "verdict": {"type": "string", "enum": ["confirm", "reject", "downgrade"]},
                        "reason": {"type": "string"},
                        "new_severity": {"type": "string", "enum": [s.value for s in Severity]},
                        "new_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["id", "verdict", "reason"],
                },
            },
            "new_findings": {"type": "array", "items": FINDING_SCHEMA},
            "notes_for_reviewer": {"type": "string"},
        },
        "required": ["verdicts"],
    },
)


# --- triggers ---------------------------------------------------------------


def risk_reasons(
    *,
    primary: AgentOutcome,
    reportable: list[Finding],
    checks: list[CheckResult] | None,
    diff: Diff,
    oc: OverseerConfig,
) -> list[str]:
    """Reasons the review looks risky. Empty means the primary review stands on its own."""
    reasons: list[str] = []
    if primary.incomplete:
        reasons.append("primary review did not finish")
    serious = [f for f in reportable if f.severity.rank >= Severity.MAJOR.rank]
    if serious:
        reasons.append(f"{len(serious)} major/blocker finding(s) from the primary reviewer")
    if primary.escalations:
        reasons.append(f"primary reviewer escalated {len(primary.escalations)} question(s)")
    # Only checks that found problems. A check that could not run (status ERROR, most often
    # the target repo's tests failing to install in the sandbox) says nothing about the
    # change; on the dev split that case fired the trigger on 61% of reviews.
    bad = [c.name for c in checks or [] if c.status is CheckStatus.FAILED]
    if bad:
        reasons.append(f"Layer 1 check(s) found problems: {', '.join(bad)}")
    size = diff.lines_added + diff.lines_removed
    if size > oc.large_diff_lines or len(diff.files) > oc.large_diff_files:
        reasons.append(f"large diff ({len(diff.files)} files, {size} lines)")
    hit = _sensitive_hit(diff)
    if hit:
        reasons.append(f"touches security-sensitive code ({hit})")
    return reasons


def _sensitive_hit(diff: Diff) -> str | None:
    for f in diff.files:
        if not f.is_python:
            continue
        if SENSITIVE_PATH.search(f.path):
            return f.path
        for text in f.added.values():
            m = SENSITIVE_CODE.search(text)
            if m:
                return f"{m.group(0)} in {f.path}"
    return None


def should_run(trigger: OverseerTrigger, reasons: list[str]) -> bool:
    if trigger is OverseerTrigger.NEVER:
        return False
    return trigger is OverseerTrigger.ALWAYS or bool(reasons)


# --- evidence packets -------------------------------------------------------


def _excerpt(tctx: ToolContext, f: Finding) -> str:
    if not f.file or not f.line_start:
        return ""
    try:
        path = resolve_in_tree(tctx.tree, f.file)
        lines = path.read_text(errors="replace").splitlines()
    except (ToolError, OSError):
        return ""
    lo = max(1, f.line_start - EXCERPT_CONTEXT)
    hi = min(
        len(lines), max(f.line_end, f.line_start) + EXCERPT_CONTEXT, lo + MAX_EXCERPT_LINES - 1
    )
    width = len(str(hi))
    return "\n".join(f"{n:>{width}}| {lines[n - 1]}" for n in range(lo, hi + 1))


def build_packets(
    findings: list[Finding],
    tctx: ToolContext,
    steps: dict[str, ToolTraceStep],
    outputs: dict[str, str],
    max_spot_checks: int,
) -> tuple[str, dict[str, Finding]]:
    """Numbered packets for the findings worth spot-checking, plus the id -> finding map."""
    ranked = sorted(findings, key=lambda f: (-f.severity.rank, -f.confidence, f.id))
    checked, skipped = ranked[:max_spot_checks], ranked[max_spot_checks:]
    index: dict[str, Finding] = {}
    blocks: list[str] = []
    for i, f in enumerate(checked, 1):
        fid = f"F{i}"
        index[fid] = f
        loc = f"{f.file}:{f.line_start}-{f.line_end}" if f.file else "(change-level)"
        lines = [
            f"{fid} [{f.severity.value}] {f.category.value} at {loc} "
            f"(confidence {f.confidence:.2f})",
            f"summary: {f.summary}",
            f"evidence: {f.evidence}",
            f"cited refs: {', '.join(f.evidence_refs) or 'none'}",
        ]
        code = _excerpt(tctx, f)
        if code:
            lines += ["code at head:", code]
        for ref in [r for r in f.evidence_refs if r in steps][:2]:
            st = steps[ref]
            body = outputs.get(ref, st.output_excerpt)[:MAX_CITED_OUTPUT_CHARS]
            lines.append(f"tool output {ref} ({st.tool} {st.arguments}):\n{body}")
        blocks.append("\n".join(lines))
    if skipped:
        blocks.append(
            "Not spot-checked (budget); do not issue verdicts for these:\n"
            + "\n".join(
                f"- [{f.severity.value}] {f.file}:{f.line_start} {f.summary}" for f in skipped
            )
        )
    return "\n\n".join(blocks), index


def _escalation_text(escs: list[Escalation]) -> str:
    return "\n".join(
        f"E{i}. {e.file or '(change)'}:{e.line_start}-{e.line_end}: {e.question}"
        for i, e in enumerate(escs, 1)
    )


def overseer_prompt(oc: OverseerConfig) -> str:
    return "\n\n".join(
        [
            Template(prompts.load("overseer_system")).substitute(max_tool_calls=oc.max_tool_calls),
            pitfalls.render(include_verification=True),
            prompts.load("severity_guide"),
        ]
    )


def overseer_message(
    tctx: ToolContext,
    checks: list[CheckResult] | None,
    packets: str,
    escalations: list[Escalation],
    reasons: list[str],
) -> str:
    parts: list[str] = []
    if tctx.task:
        parts.append("Stated task for this change:\n" + prompts.wrap_untrusted("task", tctx.task))
    parts.append(f"Changed files ({len(tctx.diff.files)}):\n{changed_files_summary(tctx)}")
    if checks is not None:
        parts.append(
            "Layer 1 deterministic results (facts; already reported):\n"
            + prompts.wrap_untrusted("layer1", layer1_summary(checks))
        )
    raw = tctx.diff.raw
    note = (
        " (truncated; use get_diff with a path for the rest)" if len(raw) > MAX_DIFF_CHARS else ""
    )
    parts.append(f"Diff{note}:\n" + prompts.wrap_untrusted("diff", raw[:MAX_DIFF_CHARS]))
    if reasons:
        parts.append("You were called because: " + "; ".join(reasons) + ".")
    parts.append(
        "Findings from the primary reviewer to spot-check:\n"
        + prompts.wrap_untrusted("primary_findings", packets or "(none reported)")
    )
    if escalations:
        parts.append(
            "Questions the primary reviewer escalated to you:\n"
            + prompts.wrap_untrusted("escalations", _escalation_text(escalations))
        )
    parts.append("Do your oversight now, then call submit_oversight.")
    return "\n\n".join(parts)


# --- applying verdicts ------------------------------------------------------


@dataclass
class Applied:
    kept: list[Finding] = field(default_factory=list)
    rejected: list[tuple[Finding, str]] = field(default_factory=list)
    confirmed: int = 0
    downgraded: int = 0
    warnings: list[str] = field(default_factory=list)


def _downgrade(f: Finding, v: dict[str, Any]) -> Finding:
    rank = _SEVERITY_ORDER.index(f.severity)
    wanted: Severity | None
    try:
        wanted = Severity(str(v.get("new_severity")))
    except ValueError:
        wanted = None
    if wanted is not None and _SEVERITY_ORDER.index(wanted) > rank:
        sev = wanted
    else:
        sev = _SEVERITY_ORDER[min(rank + 1, len(_SEVERITY_ORDER) - 1)]
    conf = f.confidence * 0.85
    nc = v.get("new_confidence")
    if isinstance(nc, int | float) and 0 <= nc <= 1:
        conf = min(conf, float(nc))
    return f.model_copy(
        update={"severity": sev, "confidence": round(conf, 3), "verified_by": "overseer:downgraded"}
    )


def apply_verdicts(raw: Any, packets: dict[str, Finding]) -> Applied:
    """Apply the overseer's verdicts. Unknown ids and unreviewed findings are left alone."""
    out = Applied()
    decided: dict[str, dict[str, Any]] = {}
    for v in raw if isinstance(raw, list) else []:
        if not isinstance(v, dict):
            continue
        fid = str(v.get("id", "")).strip().upper()
        if fid not in packets:
            out.warnings.append(f"overseer gave a verdict for unknown finding {fid!r}; ignored")
        elif fid not in decided:  # first verdict wins
            decided[fid] = v
    for fid, f in packets.items():
        v = decided.get(fid)
        verdict = str(v.get("verdict", "")).lower() if v else ""
        reason = str(v.get("reason", ""))[:300] if v else ""
        if v is None or verdict not in ("confirm", "reject", "downgrade"):
            out.kept.append(f)  # unreviewed: stays as the primary reported it
        elif verdict == "reject":
            out.rejected.append((f, reason or "no reason given"))
        elif verdict == "downgrade":
            out.kept.append(_downgrade(f, v))
            out.downgraded += 1
        else:
            out.kept.append(f.model_copy(update={"verified_by": "overseer:confirmed"}))
            out.confirmed += 1
    return out


def matches_rejected(f: Finding, rejected: list[Finding]) -> bool:
    return any(f.id == r.id or (f.category == r.category and lines_near(f, r)) for r in rejected)


def feedback_needed(oc: OverseerConfig, *, rejected: int, added: int, spot_checked: int) -> bool:
    if oc.feedback_max_rounds < 1:
        return False
    if rejected + added >= oc.feedback_min_issues:
        return True
    return spot_checked >= 2 and rejected / spot_checked >= oc.feedback_reject_fraction


def build_notes(applied: Applied, new: list[Finding], free_notes: str) -> str:
    lines: list[str] = []
    if free_notes.strip():
        lines.append(free_notes.strip()[:MAX_NOTES_CHARS])
    for f, why in applied.rejected:
        lines.append(
            f"- REJECTED your finding at {f.file}:{f.line_start} ({f.category.value}): {why}"
        )
    for f in new:
        lines.append(f"- OVERSEER FOUND, verify it yourself: {f.file}:{f.line_start} {f.summary}")
    return "\n".join(lines)[:4000]


# --- the stage --------------------------------------------------------------


@dataclass
class Supervision:
    """What supervision changed. `findings` replaces the primary's agent findings."""

    findings: list[Finding]
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    stages: list[StageSummary] = field(default_factory=list)
    trace: list[ToolTraceStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def supervise(
    *,
    cfg: Config,
    tctx: ToolContext,
    checks: list[CheckResult] | None,
    primary: AgentOutcome,
    reportable: list[Finding],
    reasons: list[str],
    overseer_client: LLMClient,
    overseer_budget: ReviewBudget,
    make_second_pass: Callable[[], tuple[LLMClient, ReviewBudget]],
) -> Supervision:
    oc = cfg.overseer
    result = Supervision(findings=list(primary.findings))

    packets_text, index = build_packets(
        reportable, tctx, {s.ref: s for s in primary.trace}, primary.outputs, oc.max_spot_checks
    )
    spec = LoopSpec(
        label="overseer", submit=_OVERSIGHT_SPEC, ref_prefix="o", required_key="verdicts"
    )
    messages = [
        Message.system(overseer_prompt(oc)),
        Message.user(overseer_message(tctx, checks, packets_text, primary.escalations, reasons)),
    ]
    loop = run_loop(
        overseer_client, tctx, messages, spec, budget=overseer_budget,
        max_tokens=cfg.llm.max_output_tokens, temperature=cfg.llm.temperature, seed=cfg.llm.seed,
    )  # fmt: skip
    result.trace += loop.trace
    result.warnings += loop.warnings
    stage = StageSummary(
        name="overseer", model=overseer_client.model, trigger_reasons=reasons,
        usage=overseer_budget.usage, escalations=primary.escalations,
    )  # fmt: skip
    result.stages.append(stage)
    result.usage.add(overseer_budget.usage)

    if loop.submitted_args is None:
        stage.error = f"overseer did not finish ({loop.stop_reason})"
        result.warnings.append(
            f"{stage.error}; the primary reviewer's findings were not independently verified"
        )
        return result

    args = loop.submitted_args
    applied = apply_verdicts(args.get("verdicts"), index)
    result.warnings += applied.warnings

    steps = {s.ref: s for s in [*primary.trace, *loop.trace]}
    outputs = {**primary.outputs, **loop.outputs}
    check_ids = {c.id for c in checks or []}
    raw_new = args.get("new_findings")
    new_found, errors = validate_items(
        raw_new if isinstance(raw_new, list) else [], Source.OVERSEER
    )
    result.warnings += [f"overseer dropped invalid {e}" for e in errors]
    new_found, _ = apply_grounding(new_found, steps, check_ids, outputs)
    grounded: list[Finding] = []
    for f in new_found:
        if f.confidence < cfg.review.min_confidence:
            # Cited refs that never ran (or only the diff): not evidence. It must not count
            # toward the feedback threshold either, or junk could trigger a second pass.
            result.dropped.append((f, "overseer finding lacks verifiable evidence"))
        else:
            grounded.append(f.model_copy(update={"verified_by": "overseer"}))
    new_found = grounded

    notes = str(args.get("notes_for_reviewer") or "")[:MAX_NOTES_CHARS]
    stage.confirmed, stage.rejected = applied.confirmed, len(applied.rejected)
    stage.downgraded, stage.added = applied.downgraded, len(new_found)
    stage.notes = notes or None
    result.dropped += [(f, f"overseer rejected: {why}") for f, why in applied.rejected]
    rejected_findings = [f for f, _ in applied.rejected]

    # The primary's findings: untouched ones (not shown to the overseer) plus the verdicts.
    shown = {f.id for f in index.values()}
    kept = [f for f in primary.findings if f.id not in shown]
    kept += applied.kept
    result.findings = kept + new_found

    if feedback_needed(
        oc, rejected=len(applied.rejected), added=len(new_found), spot_checked=len(index)
    ):
        client2, budget2 = make_second_pass()
        second = run_agent(
            client2,
            tctx,
            checks=checks,
            budget=budget2,
            max_tokens=cfg.llm.max_output_tokens,
            temperature=cfg.llm.temperature,
            seed=cfg.llm.seed,
            notes=build_notes(applied, new_found, notes),
            ref_prefix="r",
        )
        result.trace += second.trace
        result.warnings += [w.replace("agent", "second-pass agent") for w in second.warnings]
        result.usage.add(budget2.usage)
        have = {f.id for f in result.findings}
        fresh: list[Finding] = []
        for f in second.findings:
            if matches_rejected(f, rejected_findings):
                result.dropped.append((f, "second pass re-raised a finding the overseer rejected"))
            elif f.id not in have:
                fresh.append(f)
        result.findings += fresh
        result.stages.append(
            StageSummary(
                name="second_pass",
                model=client2.model,
                trigger_reasons=["overseer feedback"],
                usage=budget2.usage,
                added=len(fresh),
                error=None if second.submitted else "second pass did not finish",
            )
        )
    return result
