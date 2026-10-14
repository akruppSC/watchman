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


@dataclass
class Finalized:
    findings: list[Finding]
    dropped: list[tuple[Finding, str]]


def finalize(
    findings: list[Finding], diff: Diff, tree: Path, *, min_confidence: float
) -> Finalized:
    g = ground(findings, diff, tree)
    t = threshold(g.kept, min_confidence)
    return Finalized(findings=dedupe(t.kept), dropped=g.dropped + t.dropped)


__all__ = ["FilterReport", "Finalized", "compute_verdict", "finalize", "ground", "threshold"]
