"""Diff hygiene: test tampering and lint/type suppression, both common agent shortcuts.

When an agent can't make a test pass, it often skips, deletes, or weakens the test, or
silences the linter. These patterns are cheap to detect precisely from the diff alone.
"""

from __future__ import annotations

import io
import re
import tokenize

from watchman_review.checks.base import Check, CheckContext
from watchman_review.checks.scope import is_test_path
from watchman_review.gitutil import FileStatus
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

# Only unconditional skip decorators. Conditional skips (skipif, pytest.skip() inside an
# environment check) are legitimate and were the main false-positive source in dogfooding.
_SKIP = re.compile(
    r"^\s*@(?:pytest\.mark\.skip\b(?!if)|pytest\.mark\.xfail\s*(?:$|\(\s*(?:reason\s*=.*)?\)\s*$)"
    r"|unittest\.skip\(|unittest\.expectedFailure\b)"
)
_ASSERT = re.compile(r"^\s*(assert\b|self\.assert\w*\(|pytest\.raises\()")
_SUPPRESS = re.compile(
    r"#\s*(?P<kind>noqa|type:\s*ignore|pyright:\s*ignore)"
    r"(?P<codes>(?::\s*[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)|\[[\w\-, ]+\])?"
    r"(?P<rest>.*)$"
)


def comments_by_line(source: str) -> dict[int, str] | None:
    """Map line -> comment text using the tokenizer, so strings are never mistaken for
    comments. Returns None if the file doesn't tokenize."""
    out: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string
    except (tokenize.TokenError, SyntaxError):
        return None
    return out


def classify_suppression(text: str) -> tuple[str, Severity] | None:
    """Return (kind, severity) for an unjustified suppression, or None if fine."""
    m = _SUPPRESS.search(text)
    if not m:
        return None
    kind = re.sub(r"\s+", " ", m.group("kind"))
    justified = len(re.findall(r"[A-Za-z]{3,}", m.group("rest"))) >= 2
    if not m.group("codes"):
        return kind, Severity.MINOR  # blanket suppression
    return None if justified else (kind, Severity.NIT)


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

            head_file = ctx.tree / f.path
            comments = (
                comments_by_line(head_file.read_text(errors="replace"))
                if head_file.is_file()
                else None
            )
            for line, text in sorted(f.added.items()):
                if test_file and _SKIP.search(text):
                    findings.append(_finding(
                        Category.WEAK_TEST, Severity.MAJOR, 0.85, f.path, line,
                        "Test skipped or marked xfail in this change",
                        f"Added line {f.path}:{line}: `{text.strip()[:160]}`.",
                        "hygiene:test-skipped",
                    ))  # fmt: skip
                comment = text if comments is None else comments.get(line)
                sup = classify_suppression(comment) if comment else None
                if sup:
                    kind, severity = sup
                    blanket = severity is Severity.MINOR
                    findings.append(_finding(
                        Category.CONVENTION_VIOLATION, severity, 0.8, f.path, line,
                        f"New {'blanket ' if blanket else ''}`{kind}` suppression without "
                        "justification",
                        f"Added line {f.path}:{line}: `{text.strip()[:160]}`. "
                        + ("No rule code given. " if blanket else "")
                        + "Justify it in the comment or fix the underlying issue.",
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
