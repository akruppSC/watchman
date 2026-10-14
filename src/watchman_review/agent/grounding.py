"""Evidence-reference grounding for agent findings.

Each agent finding must cite references that were actually produced in this review:
  t<N>        an executed, successful tool call
  check:<x>   a Layer 1 check result that was shown to the agent
  diff        the initial diff (weak evidence)

A finding is graded by its valid references:
  verified    at least one cited tool call or check that concerns the finding's file
              -> confidence unchanged
  diff-only   only `diff`, or tool refs unrelated to the file
              -> confidence capped below the block threshold
  unverified  no valid references
              -> confidence capped below the reporting threshold, so it is dropped
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from watchman_review.schema import Finding, ToolTraceStep

DIFF_REF = "diff"
DIFF_ONLY_CONFIDENCE_CAP = 0.6
UNVERIFIED_CONFIDENCE_CAP = 0.3


class Grade(StrEnum):
    VERIFIED = "verified"
    DIFF_ONLY = "diff_only"
    UNVERIFIED = "unverified"


@dataclass
class GroundingRecord:
    finding_id: str
    grade: Grade
    invalid_refs: list[str]


def _mentions(step: ToolTraceStep, output: str, path: str) -> bool:
    for v in step.arguments.values():
        if isinstance(v, str) and path in v:
            return True
        if isinstance(v, list) and any(path in str(x) for x in v):
            return True
    return path in output


def grade_finding(
    f: Finding,
    steps: dict[str, ToolTraceStep],
    check_ids: set[str],
    outputs: dict[str, str] | None = None,
) -> tuple[Grade, list[str], list[str]]:
    """Returns (grade, valid_refs, invalid_refs)."""
    valid: list[str] = []
    invalid: list[str] = []
    for ref in dict.fromkeys(f.evidence_refs):  # dedupe, keep order
        step = steps.get(ref)
        if ref == DIFF_REF or ref in check_ids or (step is not None and step.ok):
            valid.append(ref)
        else:
            invalid.append(ref)
    strong = [r for r in valid if r != DIFF_REF]
    if not valid:
        return Grade.UNVERIFIED, valid, invalid
    if not strong:
        return Grade.DIFF_ONLY, valid, invalid
    if f.file is None:
        return Grade.VERIFIED, valid, invalid
    outputs = outputs or {}
    relevant = any(
        r in check_ids or _mentions(steps[r], outputs.get(r, steps[r].output_excerpt), f.file)
        for r in strong
    )
    return (Grade.VERIFIED if relevant else Grade.DIFF_ONLY), valid, invalid


def apply_grounding(
    findings: list[Finding],
    steps: dict[str, ToolTraceStep],
    check_ids: set[str],
    outputs: dict[str, str] | None = None,
) -> tuple[list[Finding], list[GroundingRecord]]:
    out: list[Finding] = []
    records: list[GroundingRecord] = []
    for f in findings:
        grade, valid, invalid = grade_finding(f, steps, check_ids, outputs)
        cap = {
            Grade.VERIFIED: 1.0,
            Grade.DIFF_ONLY: DIFF_ONLY_CONFIDENCE_CAP,
            Grade.UNVERIFIED: UNVERIFIED_CONFIDENCE_CAP,
        }[grade]
        update: dict[str, object] = {"evidence_refs": valid}
        if f.confidence > cap:
            update["confidence"] = cap
        g = f.model_copy(update=update)
        out.append(g)
        records.append(GroundingRecord(finding_id=g.id, grade=grade, invalid_refs=invalid))
    return out, records
