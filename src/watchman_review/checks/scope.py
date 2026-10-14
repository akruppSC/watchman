"""Diff-size and scope metrics."""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from watchman_review.checks.base import Check, CheckContext
from watchman_review.schema import CheckResult, CheckStatus

SENSITIVE_PREFIXES = (".github/workflows/", ".gitlab-ci", ".circleci/", "Dockerfile", ".pre-commit")


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        "tests" in p.parts
        or "test" in p.parts
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
    )


class ScopeCheck(Check):
    name = "scope"

    def run(self, ctx: CheckContext) -> CheckResult:
        d = ctx.diff
        cfg = ctx.config.review
        n_files = len(d.files)
        added, removed = d.lines_added, d.lines_removed
        top_dirs = Counter(
            PurePosixPath(f.path).parts[0] if "/" in f.path else "." for f in d.files
        )
        src = [f for f in d.files if f.is_python and not is_test_path(f.path)]
        tests = [f for f in d.files if is_test_path(f.path)]

        metrics = (
            f"{n_files} file(s), +{added}/-{removed} lines, "
            f"{len(src)} source / {len(tests)} test file(s), "
            f"areas: {', '.join(f'{k}({v})' for k, v in top_dirs.most_common(6))}"
        )
        # Metrics only. Size and CI-touching are context for the agent's scope judgment,
        # not defects: on the dev split's clean diffs, every scope finding was a false alarm.
        notes: list[str] = []
        if n_files > cfg.max_diff_files_warn or added + removed > cfg.max_diff_lines_warn:
            notes.append("large change")
        ci = [f.path for f in d.files if f.path.startswith(SENSITIVE_PREFIXES)]
        if ci:
            notes.append(f"touches CI/build config: {', '.join(ci[:5])}")
        summary = metrics + (f"; {'; '.join(notes)}" if notes else "")
        return self.result(CheckStatus.PASSED, summary)
