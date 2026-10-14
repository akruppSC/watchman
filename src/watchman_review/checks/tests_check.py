"""Run the target repo's test suite in the sandbox.

Failures are verified against the merge-base. Any test that also fails there (pre-existing
breakage, or environment problems such as no network in the sandbox) is not blamed on the
change. This keeps the check's precision high.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from watchman_review.checks.base import DETERMINISTIC_CONFIDENCE, Check, CheckContext
from watchman_review.gitutil import GitError, snapshot
from watchman_review.sandbox import ExecResult, Sandbox
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:::(\S+))?(?: - (.*))?$", re.MULTILINE)

# pytest exit codes
_OK, _TESTS_FAILED, _NO_TESTS = 0, 1, 5
MAX_BASE_RECHECK = 30


@dataclass
class Failure:
    path: str
    test: str | None
    reason: str

    @property
    def node_id(self) -> str:
        return f"{self.path}::{self.test}" if self.test else self.path


def parse_failures(output: str) -> list[Failure]:
    seen: dict[str, Failure] = {}
    for m in _FAILED_RE.finditer(output):
        f = Failure(m.group(1), m.group(2), (m.group(3) or "").strip())
        seen.setdefault(f.node_id, f)
    return list(seen.values())


def has_tests(tree: Path) -> bool:
    for pattern in ("test_*.py", "*_test.py"):
        if next(tree.rglob(pattern), None) is not None:
            return True
    return False


def pytest_argv(
    ctx: CheckContext, sandbox: Sandbox, tree: Path, selector: list[str] | None = None
) -> list[str]:
    python = sandbox.python(tree, ctx.repo_root)
    custom = ctx.config.review.test_command
    if custom:
        argv: list[str] = []
        for part in custom:
            argv += python if part == "{python}" else [part]
        return argv + (selector or [])
    # -W ignore::DeprecationWarning: many suites set filterwarnings=error, and a newer pytest
    # or Python than the project pinned turns harmless deprecations into collection errors.
    # Watchman cares about behavior, not deprecations.
    return [
        *python, "-m", "pytest", "-q", "--no-header", "-rfE", "-p", "no:cacheprovider",
        "--color=no", "-W", "ignore::DeprecationWarning", *(selector or []),
    ]  # fmt: skip


def run_pytest(ctx: CheckContext, tree: Path, selector: list[str] | None = None) -> ExecResult:
    assert ctx.sandbox is not None
    argv = pytest_argv(ctx, ctx.sandbox, tree, selector)
    return ctx.sandbox.run(argv, tree, timeout=ctx.config.review.test_timeout_s)


class TestsCheck(Check):
    name = "pytest"

    def run(self, ctx: CheckContext) -> CheckResult:
        if ctx.sandbox is None:
            return self.result(CheckStatus.SKIPPED, "no sandbox available")
        if not ctx.config.review.test_command and not has_tests(ctx.tree):
            return self.result(CheckStatus.SKIPPED, "no tests found")

        res = run_pytest(ctx, ctx.tree)
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

        failures = parse_failures(out)
        also_at_base = self._fails_at_base(ctx, failures)
        new = [f for f in failures if f.node_id not in also_at_base]
        findings = [self._finding(ctx, f) for f in new]
        summary = _last_line(out)
        if also_at_base:
            summary += f"; {len(also_at_base)} also fail at merge-base (not attributed)"
        if not failures:
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
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        return self.result(status, summary, findings, out)

    def _fails_at_base(self, ctx: CheckContext, failures: list[Failure]) -> set[str]:
        """Node IDs that exist and also fail at the merge-base."""
        if not failures or not ctx.diff.merge_base or len(failures) > MAX_BASE_RECHECK:
            return set()
        try:
            with snapshot(ctx.repo_root, ctx.diff.merge_base) as base_tree:
                present = [f for f in failures if (base_tree / f.path).is_file()]
                if not present:
                    return set()
                res = run_pytest(ctx, base_tree, [f.node_id for f in present])
                if res.timed_out:
                    return set()
                base_failed = {f.node_id for f in parse_failures(res.combined)}
        except GitError:
            return set()
        return {f.node_id for f in present if f.node_id in base_failed}

    def _finding(self, ctx: CheckContext, f: Failure) -> Finding:
        changed = ctx.diff.file(f.path) is not None
        assert ctx.sandbox is not None
        return Finding(
            category=Category.CORRECTNESS,
            severity=Severity.BLOCKER if changed else Severity.MAJOR,
            confidence=DETERMINISTIC_CONFIDENCE,
            file=f.path,
            summary=f"Test fails: {f.node_id}"[:300],
            evidence=f"pytest in {ctx.sandbox.name} sandbox: FAILED {f.node_id}"
            + (f" - {f.reason[:300]}" if f.reason else "")
            + ". Not failing at merge-base (or new test).",
            source=Source.DETERMINISTIC,
            rule="pytest:failed",
        )


def _last_line(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1].strip("= ")[:200] if lines else ""
