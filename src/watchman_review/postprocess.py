"""Shared post-processing: grounding, thresholds, dedupe, verdict.

Grounding enforces "every finding must cite verifiable evidence" in code rather than
trusting the prompt. A model finding is dropped if it points at a file that is not part of
the change or the repo, or at lines that do not exist in that file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from watchman_review.gitutil import Diff
from watchman_review.schema import Finding, Source, compute_verdict, dedupe


@dataclass
class FilterReport:
    kept: list[Finding] = field(default_factory=list)
    dropped: list[tuple[Finding, str]] = field(default_factory=list)


def _line_count(tree: Path, rel: str) -> int | None:
    p = (tree / rel).resolve()
    try:
        p.relative_to(tree.resolve())
    except ValueError:
        return None  # path escapes the repo
    if not p.is_file():
        return None
    with p.open("rb") as fh:
        return sum(1 for _ in fh)


def ground(findings: list[Finding], diff: Diff, tree: Path) -> FilterReport:
    report = FilterReport()
    diff_paths = {f.path for f in diff.files}
    for f in findings:
        if f.source is Source.DETERMINISTIC:
            report.kept.append(f)
            continue
        if f.file is None:
            if f.line_start:
                report.dropped.append((f, "line numbers given without a file"))
            else:
                report.kept.append(f)
            continue
        f.file = f.file.removeprefix("./").removeprefix("b/")
        n_lines = _line_count(tree, f.file)
        if n_lines is None:
            if f.file in diff_paths:  # deleted file: allow change-level reference
                f.line_start = f.line_end = 0
                report.kept.append(f)
            else:
                report.dropped.append((f, f"file {f.file!r} not in repo or diff"))
            continue
        if f.line_start > n_lines:
            report.dropped.append((f, f"line {f.line_start} beyond end of {f.file} ({n_lines})"))
            continue
        f.line_end = min(f.line_end, n_lines)
        report.kept.append(f)
    return report


def threshold(findings: list[Finding], min_confidence: float) -> FilterReport:
    report = FilterReport()
    for f in findings:
        if f.source is Source.DETERMINISTIC or f.confidence >= min_confidence:
            report.kept.append(f)
        else:
            report.dropped.append((f, f"confidence {f.confidence:.2f} < {min_confidence:.2f}"))
    return report


def merge_colocated(findings: list[Finding]) -> list[Finding]:
    """Merge findings with the same file, start line, and category into one.

    Several rules often describe one defect (ruff E722 and S110 both fire on a bare
    `except: pass`). Reporting it twice is noise to the reader, and it would inflate
    false positives in evals. The strongest finding is kept and the others are noted in
    its evidence.
    """
    groups: dict[tuple[str, int, str], list[Finding]] = {}
    passthrough: list[Finding] = []
    for f in findings:
        if f.file is None or f.line_start == 0:
            passthrough.append(f)
        else:
            groups.setdefault((f.file, f.line_start, f.category.value), []).append(f)
    merged: list[Finding] = []
    for group in groups.values():
        group.sort(key=lambda f: (-f.severity.rank, -f.confidence, f.rule or "", f.summary))
        primary, rest = group[0], group[1:]
        if rest:
            also = "; ".join(f.rule or f.summary for f in rest)
            primary = primary.model_copy(
                update={
                    "evidence": f"{primary.evidence} Also reported by: {also}.",
                    "evidence_refs": sorted({r for f in group for r in f.evidence_refs}),
                    "line_end": max(f.line_end for f in group),
                }
            )
        merged.append(primary)
    return merged + passthrough


# Findings restate each other only when their line ranges actually overlap. A two-line slack
# changed no dev result, but twice collapsed distinct defects sitting close together (a SQL
# injection under a prompt-injection comment).
RESTATEMENT_SLACK = 0


def lines_near(a: Finding, b: Finding, slack: int = RESTATEMENT_SLACK) -> bool:
    """True if two findings are in the same file and their line ranges are within `slack`."""
    if a.file is None or a.file != b.file or not a.line_start or not b.line_start:
        return False
    return a.line_start <= b.line_end + slack and b.line_start <= a.line_end + slack


def drop_restatements(findings: list[Finding]) -> FilterReport:
    """Drop a model finding that restates a stronger one on the same lines.

    Stronger = deterministic, else higher (severity, confidence). LLM reviewers often
    report one defect twice under different categories (e.g. swallowed_error and
    correctness for the same try/except). On the dev split this removed ~15% of findings
    with no recall loss. Deterministic findings are never dropped.
    """

    def strength(f: Finding) -> tuple[bool, bool, int, float, str]:
        verified = f.verified_by is not None and not f.verified_by.endswith("downgraded")
        return (f.source is Source.DETERMINISTIC, verified, f.severity.rank, f.confidence, f.id)

    def same_defect(a: Finding, b: Finding) -> bool:
        return lines_near(a, b, RESTATEMENT_SLACK)

    report = FilterReport()
    for f in sorted(findings, key=strength, reverse=True):
        twin = next((k for k in report.kept if same_defect(f, k)), None)
        if twin is not None and f.source is not Source.DETERMINISTIC:
            report.dropped.append((f, f"restates {twin.id} ({twin.category.value})"))
        else:
            report.kept.append(f)
    return report


@dataclass
class Finalized:
    findings: list[Finding]
    dropped: list[tuple[Finding, str]]


def finalize(
    findings: list[Finding], diff: Diff, tree: Path, *, min_confidence: float
) -> Finalized:
    g = ground(findings, diff, tree)
    t = threshold(g.kept, min_confidence)
    r = drop_restatements(merge_colocated(t.kept))
    return Finalized(findings=dedupe(r.kept), dropped=g.dropped + t.dropped + r.dropped)


__all__ = [
    "FilterReport",
    "Finalized",
    "compute_verdict",
    "drop_restatements",
    "finalize",
    "ground",
    "merge_colocated",
    "threshold",
]
