"""Diff hygiene: test tampering and lint/type suppression, both common agent shortcuts.

When an agent can't make a test pass, it often skips, deletes, or weakens the test, or
silences the linter. These patterns are cheap to detect precisely from the diff alone.
"""

from __future__ import annotations

import re

from watchman_review.checks.base import Check, CheckContext
from watchman_review.checks.scope import is_test_path
from watchman_review.gitutil import FileStatus
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

_SKIP = re.compile(
    r"@pytest\.mark\.(skip|xfail)\b|\bpytest\.(skip|xfail)\(|@unittest\.(skip|expectedFailure)"
)
_ASSERT = re.compile(r"^\s*(assert\b|self\.assert\w*\(|pytest\.raises\()")
_SUPPRESS = re.compile(r"#\s*(noqa\b|type:\s*ignore\b|pragma:\s*no\s*cover|pyright:\s*ignore)")


def _finding(
    cat: Category, sev: Severity, conf: float, file: str, line: int, summary: str, evidence: str,
    rule: str,
) -> Finding:  # fmt: skip
    return Finding(
        category=cat, severity=sev, confidence=conf, file=file, line_start=line, line_end=line,
        summary=summary, evidence=evidence, source=Source.DETERMINISTIC, rule=rule,
    )  # fmt: skip


class HygieneCheck(Check):
    name = "hygiene"

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        for f in ctx.diff.files:
            if not f.is_python or f.is_binary:
                continue
            test_file = is_test_path(f.path)

            if test_file and f.status is FileStatus.DELETED:
                n = sum(1 for t in f.removed if re.match(r"\s*def test_", t))
                findings.append(_finding(
                    Category.WEAK_TEST, Severity.MAJOR, 0.85, f.path, 0,
                    f"Test file deleted ({n} test function(s) removed)",
                    f"{f.path} was deleted in this diff; {n} `def test_` definitions removed.",
                    "hygiene:test-deleted",
                ))  # fmt: skip
                continue

            for line, text in sorted(f.added.items()):
                if test_file and _SKIP.search(text):
                    findings.append(_finding(
                        Category.WEAK_TEST, Severity.MAJOR, 0.85, f.path, line,
                        "Test skipped or marked xfail in this change",
                        f"Added line {f.path}:{line}: `{text.strip()[:160]}`.",
                        "hygiene:test-skipped",
                    ))  # fmt: skip
                m = _SUPPRESS.search(text)
                if m:
                    findings.append(_finding(
                        Category.CONVENTION_VIOLATION, Severity.MINOR, 0.8, f.path, line,
                        f"New lint/type suppression `{m.group(1).strip()}`",
                        f"Added line {f.path}:{line}: `{text.strip()[:160]}`. Suppressions "
                        "should be justified or the underlying issue fixed.",
                        "hygiene:suppression",
                    ))  # fmt: skip

            if test_file and f.status is FileStatus.MODIFIED:
                removed_asserts = sum(1 for t in f.removed if _ASSERT.match(t))
                added_asserts = sum(1 for t in f.added.values() if _ASSERT.match(t))
                if removed_asserts >= 2 and added_asserts < removed_asserts / 2:
                    first = min(f.added) if f.added else 0
                    findings.append(_finding(
                        Category.WEAK_TEST, Severity.MAJOR, 0.7, f.path, first,
                        "Assertions removed from tests",
                        f"{f.path}: {removed_asserts} assertion line(s) removed, "
                        f"{added_asserts} added.",
                        "hygiene:assertions-removed",
                    ))  # fmt: skip
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        return self.result(status, f"{len(findings)} hygiene issue(s)", findings)
