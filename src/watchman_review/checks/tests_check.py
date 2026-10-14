"""Run the target repo's test suite in the sandbox."""

from __future__ import annotations

import re
from pathlib import Path

from watchman_review.checks.base import DETERMINISTIC_CONFIDENCE, Check, CheckContext
from watchman_review.sandbox import ExecResult, Sandbox
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:::(\S+))?(?: - (.*))?$", re.MULTILINE)

# pytest exit codes
_OK, _TESTS_FAILED, _INTERRUPTED, _INTERNAL, _USAGE, _NO_TESTS = 0, 1, 2, 3, 4, 5


def has_tests(tree: Path) -> bool:
    for pattern in ("test_*.py", "*_test.py"):
        if next(tree.rglob(pattern), None) is not None:
            return True
    return False


def pytest_argv(
    ctx: CheckContext, sandbox: Sandbox, selector: list[str] | None = None
) -> list[str]:
    if ctx.config.review.test_command and not selector:
        return list(ctx.config.review.test_command)
    python = sandbox.python(ctx.tree, ctx.repo_root)
    return [
        *python, "-m", "pytest", "-q", "--no-header", "-rfE", "-p", "no:cacheprovider",
        "--color=no", *(selector or []),
    ]  # fmt: skip


def run_pytest(ctx: CheckContext, selector: list[str] | None = None) -> ExecResult:
    assert ctx.sandbox is not None
    argv = pytest_argv(ctx, ctx.sandbox, selector)
    return ctx.sandbox.run(argv, ctx.tree, timeout=ctx.config.review.test_timeout_s)


class TestsCheck(Check):
    name = "pytest"

    def run(self, ctx: CheckContext) -> CheckResult:
        if ctx.sandbox is None:
            return self.result(CheckStatus.SKIPPED, "no sandbox available")
        if not ctx.config.review.test_command and not has_tests(ctx.tree):
            return self.result(CheckStatus.SKIPPED, "no tests found")

        res = run_pytest(ctx)
        out = res.combined
        if res.timed_out:
            return self.result(
                CheckStatus.ERROR,
                f"test suite timed out after {ctx.config.review.test_timeout_s:.0f}s",
                output=out,
            )
        if res.returncode == _OK:
            return self.result(CheckStatus.PASSED, _last_line(out), output=out)
        if res.returncode == _NO_TESTS:
            return self.result(CheckStatus.SKIPPED, "no tests collected", output=out)
        if res.returncode != _TESTS_FAILED:
            return self.result(
                CheckStatus.ERROR,
                f"pytest could not run (exit {res.returncode}): {_last_line(out)}",
                output=out,
            )

        findings: list[Finding] = []
        for m in _FAILED_RE.finditer(out):
            path, test, reason = m.group(1), m.group(2), (m.group(3) or "").strip()
            changed = ctx.diff.file(path) is not None
            findings.append(
                Finding(
                    category=Category.CORRECTNESS,
                    severity=Severity.BLOCKER if changed else Severity.MAJOR,
                    confidence=DETERMINISTIC_CONFIDENCE,
                    file=path,
                    summary=f"Test fails: {path}::{test or '?'}"[:300],
                    evidence=f"pytest in {ctx.sandbox.name} sandbox: FAILED {path}::{test or ''}"
                    + (f" - {reason[:300]}" if reason else ""),
                    source=Source.DETERMINISTIC,
                    rule="pytest:failed",
                )
            )
        if not findings:
            findings.append(
                Finding(
                    category=Category.CORRECTNESS,
                    severity=Severity.MAJOR,
                    confidence=DETERMINISTIC_CONFIDENCE,
                    summary="Test suite fails",
                    evidence=f"pytest exited 1: {_last_line(out)}",
                    source=Source.DETERMINISTIC,
                    rule="pytest:failed",
                )
            )
        return self.result(CheckStatus.FAILED, _last_line(out), findings, out)


def _last_line(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1].strip("= ")[:200] if lines else ""
