"""Ruff lint check: a fixed AI-pitfall rule set plus the repo's own lint config.

Only violations on lines added by the diff are reported, so pre-existing debt in touched
files never becomes noise.
"""

from __future__ import annotations

import json
from pathlib import Path

from watchman_review.checks.base import DETERMINISTIC_CONFIDENCE, Check, CheckContext
from watchman_review.sandbox import run_static_tool
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

# Rules that map directly onto the AI-code pitfall catalog.
PITFALL_RULES: dict[str, tuple[Category, Severity]] = {
    "F821": (Category.CORRECTNESS, Severity.MAJOR),  # undefined name
    "F811": (Category.CORRECTNESS, Severity.MINOR),  # redefinition of unused name
    "F401": (Category.OVER_ABSTRACTION, Severity.MINOR),  # unused import (dead code)
    "F841": (Category.OVER_ABSTRACTION, Severity.MINOR),  # unused local variable
    "ARG001": (Category.OVER_ABSTRACTION, Severity.NIT),  # unused function argument
    "E722": (Category.SWALLOWED_ERROR, Severity.MAJOR),  # bare except
    "BLE001": (Category.SWALLOWED_ERROR, Severity.MAJOR),  # blind except Exception
    "S110": (Category.SWALLOWED_ERROR, Severity.MAJOR),  # try/except/pass
    "S112": (Category.SWALLOWED_ERROR, Severity.MAJOR),  # try/except/continue
    "B904": (Category.SWALLOWED_ERROR, Severity.MINOR),  # raise without from in except
    "B006": (Category.CORRECTNESS, Severity.MAJOR),  # mutable default argument
    "B008": (Category.CORRECTNESS, Severity.MINOR),  # call in default argument
    "B011": (Category.WEAK_TEST, Severity.MINOR),  # assert False
    "B015": (Category.WEAK_TEST, Severity.MAJOR),  # pointless comparison (assert missing)
    "PT015": (Category.WEAK_TEST, Severity.MINOR),  # assertion always fails
    "S105": (Category.SECURITY, Severity.MAJOR),  # hardcoded password string
    "S106": (Category.SECURITY, Severity.MAJOR),  # hardcoded password argument
    "S307": (Category.SECURITY, Severity.MAJOR),  # eval
    "S301": (Category.SECURITY, Severity.MINOR),  # pickle
    "S506": (Category.SECURITY, Severity.MAJOR),  # yaml.load without SafeLoader
    "S602": (Category.SECURITY, Severity.MAJOR),  # subprocess shell=True
    "S605": (Category.SECURITY, Severity.MAJOR),  # os.system-style shell
    "S608": (Category.SECURITY, Severity.MAJOR),  # SQL string building
    "S501": (Category.SECURITY, Severity.MAJOR),  # requests verify=False
    "S113": (Category.SECURITY, Severity.MINOR),  # request without timeout
}

_DEFAULT_REPO_MAPPING = (Category.CONVENTION_VIOLATION, Severity.MINOR)


def ruff_bin() -> str:
    from ruff.__main__ import find_ruff_bin

    return str(find_ruff_bin())


def _repo_has_ruff_config(tree: Path) -> bool:
    if (tree / "ruff.toml").exists() or (tree / ".ruff.toml").exists():
        return True
    pyproject = tree / "pyproject.toml"
    return pyproject.exists() and "[tool.ruff" in pyproject.read_text(errors="replace")


class RuffCheck(Check):
    name = "ruff"

    def _invoke(
        self, ctx: CheckContext, files: list[str], extra: list[str]
    ) -> list[dict[str, object]]:
        argv = [ruff_bin(), "check", "--output-format=json", "--no-cache", "--exit-zero", *extra]
        res = run_static_tool([*argv, "--", *files], ctx.tree)
        if res.returncode != 0:
            raise RuntimeError(f"ruff failed: {res.combined[:500]}")
        data = json.loads(res.stdout or "[]")
        if not isinstance(data, list):
            raise RuntimeError("unexpected ruff output")
        return data

    def run(self, ctx: CheckContext) -> CheckResult:
        files = [f.path for f in ctx.changed_python_files()]
        if not files:
            return self.result(CheckStatus.SKIPPED, "no changed Python files")

        violations: list[tuple[dict[str, object], bool]] = []
        pitfall = self._invoke(
            ctx,
            files,
            ["--isolated", "--select", ",".join(PITFALL_RULES), "--target-version=py312"],
        )
        violations += [(v, True) for v in pitfall]
        if _repo_has_ruff_config(ctx.tree):
            violations += [(v, False) for v in self._invoke(ctx, files, [])]

        findings: list[Finding] = []
        seen: set[tuple[str, int, str]] = set()
        for v, is_pitfall in violations:
            path = _rel(ctx.tree, str(v.get("filename", "")))
            loc = v.get("location") or {}
            row = int(loc.get("row", 0)) if isinstance(loc, dict) else 0
            code = str(v.get("code") or "")
            if not code or (path, row, code) in seen or not ctx.diff.touches_line(path, row):
                continue
            seen.add((path, row, code))
            category, severity = (
                PITFALL_RULES.get(code, _DEFAULT_REPO_MAPPING)
                if is_pitfall or code in PITFALL_RULES
                else _DEFAULT_REPO_MAPPING
            )
            message = str(v.get("message") or code)
            findings.append(
                Finding(
                    category=category,
                    severity=severity,
                    confidence=DETERMINISTIC_CONFIDENCE,
                    file=path,
                    line_start=row,
                    line_end=row,
                    summary=f"{code}: {message}",
                    evidence=f"ruff reported {code} at {path}:{row} on a line added by this diff.",
                    source=Source.DETERMINISTIC,
                    rule=f"ruff:{code}",
                )
            )
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        return self.result(status, f"{len(findings)} violation(s) on changed lines", findings)


def _rel(tree: Path, filename: str) -> str:
    p = Path(filename)
    try:
        return p.resolve().relative_to(tree.resolve()).as_posix()
    except ValueError:
        return p.as_posix()
