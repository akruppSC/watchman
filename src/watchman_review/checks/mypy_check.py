"""Type check focused on hallucinated APIs and wrong call signatures.

Watchman deliberately ignores the repo's mypy config: mypy plugins listed there would
execute repo code outside the sandbox. Only a small allowlist of error codes is reported,
and only on changed lines, which keeps precision high on untyped codebases.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

from watchman_review.checks.base import Check, CheckContext
from watchman_review.sandbox import find_repo_python, run_static_tool
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

# mypy >=1.11 says 'Module has no attribute "x"'; older versions name the module.
_MODULE_ATTR = re.compile(r'^Module(?: "[^"]+")? has no attribute')
_UNEXPECTED_KW = re.compile(r"^Unexpected keyword argument")

REPORTED_CODES = {"attr-defined", "name-defined", "call-arg", "call-overload", "index", "operator"}


def classify(code: str, message: str) -> tuple[Category, Severity] | None:
    if code not in REPORTED_CODES:
        return None
    if code == "attr-defined" and _MODULE_ATTR.match(message):
        return Category.HALLUCINATED_DEPENDENCY, Severity.MAJOR
    if code == "call-arg" and _UNEXPECTED_KW.match(message):
        return Category.HALLUCINATED_DEPENDENCY, Severity.MAJOR
    return Category.CORRECTNESS, Severity.MAJOR


class MypyCheck(Check):
    name = "mypy"

    def run(self, ctx: CheckContext) -> CheckResult:
        all_files = [f.path for f in ctx.changed_python_files()]
        # A module with a sibling .pyi stub is typed by the stub; passing the .py makes
        # mypy abort with "Duplicate module". Its implementation is left to the agent.
        files = [
            p for p in all_files
            if p.endswith(".pyi") or not (ctx.tree / p).with_suffix(".pyi").exists()
        ]  # fmt: skip
        if not files:
            return self.result(CheckStatus.SKIPPED, "no changed Python files mypy can check")

        repo_python = find_repo_python(ctx.repo_root)
        with tempfile.TemporaryDirectory(prefix="watchman-mypy-") as tmp:
            cfg = Path(tmp) / "mypy.ini"
            cfg.write_text("[mypy]\n")
            argv = [
                sys.executable, "-m", "mypy",
                "--config-file", str(cfg),
                "--cache-dir", str(Path(tmp) / "cache"),
                "--check-untyped-defs",
                "--follow-imports=silent",
                "--ignore-missing-imports",
                "--no-error-summary",
                "--show-error-codes",
                "--explicit-package-bases",
                "-O", "json",
            ]  # fmt: skip
            if repo_python is not None:
                argv += ["--python-executable", str(repo_python)]
            mypypath = ":".join(p for p in ("src", ".") if (ctx.tree / p).is_dir())
            res = run_static_tool(
                [*argv, "--", *files], ctx.tree, timeout=300, env={"MYPYPATH": mypypath}
            )

        if res.returncode not in (0, 1):
            return self.result(CheckStatus.ERROR, "mypy failed to run", output=res.combined)

        findings: list[Finding] = []
        for line in res.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("severity") != "error":
                continue
            path = Path(str(item.get("file", ""))).as_posix()
            row = int(item.get("line") or 0)
            code = str(item.get("code") or "")
            message = str(item.get("message") or "")
            mapping = classify(code, message)
            if mapping is None or not ctx.diff.touches_line(path, row):
                continue
            category, severity = mapping
            findings.append(
                Finding(
                    category=category,
                    severity=severity,
                    confidence=0.85 if repo_python else 0.75,
                    file=path,
                    line_start=row,
                    line_end=row,
                    summary=f"mypy [{code}]: {message}"[:300],
                    evidence=(
                        f"mypy reported [{code}] at {path}:{row} on a changed line"
                        + (
                            ""
                            if repo_python
                            else " (no repo venv found; third-party types unknown)"
                        )
                        + "."
                    ),
                    source=Source.DETERMINISTIC,
                    rule=f"mypy:{code}",
                )
            )
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        note = "" if repo_python else " (third-party imports unresolved: no repo venv)"
        return self.result(
            status, f"{len(findings)} type error(s) on changed lines{note}", findings, res.combined
        )
