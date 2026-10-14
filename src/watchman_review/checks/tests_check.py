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
        findings = self._findings(ctx, new)
        summary = _last_line(out)
        if also_at_base:
            summary += f"; {len(also_at_base)} also fail at merge-base (not attributed)"
        if not failures:
            # Exit 1 with no parseable test failures is almost always the environment (a
            # plugin or interpreter mismatch), which can't be attributed to the change.
            return self.result(
                CheckStatus.ERROR, f"pytest failed without test failures: {summary}", output=out
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

    def _findings(self, ctx: CheckContext, failures: list[Failure]) -> list[Finding]:
        """One finding per failing test file, not one per test.

        A single bug often breaks dozens of tests (one injected off-by-one in the eval
        set failed 21). Reporting each separately buries the real defect in repeats.
        """
        by_file: dict[str, list[Failure]] = {}
        for f in failures:
            by_file.setdefault(f.path, []).append(f)
        return [self._file_finding(ctx, path, group) for path, group in by_file.items()]

    def _file_finding(self, ctx: CheckContext, path: str, group: list[Failure]) -> Finding:
        assert ctx.sandbox is not None
        first = group[0]
        names = [g.node_id for g in group]
        if len(group) == 1:
            summary = f"Test fails: {first.node_id}"
        else:
            shown = ", ".join(n.split("::", 1)[-1] for n in names[:3])
            more = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
            summary = f"{len(group)} tests fail in {path}: {shown}{more}"
        return Finding(
            category=Category.CORRECTNESS,
            severity=Severity.BLOCKER if ctx.diff.file(path) is not None else Severity.MAJOR,
            confidence=DETERMINISTIC_CONFIDENCE,
            file=path,
            summary=summary[:300],
            evidence=f"pytest in {ctx.sandbox.name} sandbox: FAILED {', '.join(names[:5])}"
            + (f" (+{len(names) - 5} more)" if len(names) > 5 else "")
            + (f" - {first.reason[:300]}" if first.reason else "")
            + ". Not failing at merge-base (or new test).",
            source=Source.DETERMINISTIC,
            rule="pytest:failed",
        )


def _last_line(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1].strip("= ")[:200] if lines else ""
