"""Shared types for deterministic checks."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from watchman_review.config import Config
from watchman_review.gitutil import Diff, FileDiff, FileStatus
from watchman_review.sandbox import Sandbox
from watchman_review.schema import CheckResult, CheckStatus, Finding

DETERMINISTIC_CONFIDENCE = 0.95


@dataclass
class CheckContext:
    repo_root: Path  # the user's repo (for locating a venv, never modified)
    tree: Path  # snapshot of head that checks read and run in
    diff: Diff
    config: Config
    sandbox: Sandbox | None = None
    task: str | None = None
    http: httpx.Client | None = None
    offline: bool = False
    cache_dir: Path | None = None
    notes: list[str] = field(default_factory=list)

    def changed_python_files(self) -> list[FileDiff]:
        return [
            f
            for f in self.diff.files
            if f.is_python
            and f.status is not FileStatus.DELETED
            and not f.is_binary
            and (self.tree / f.path).is_file()
        ]


class Check(ABC):
    name: str

    @property
    def id(self) -> str:
        return f"check:{self.name}"

    @abstractmethod
    def run(self, ctx: CheckContext) -> CheckResult: ...

    def result(
        self,
        status: CheckStatus,
        summary: str,
        findings: list[Finding] | None = None,
        output: str = "",
    ) -> CheckResult:
        findings = findings or []
        for f in findings:
            if self.id not in f.evidence_refs:
                f.evidence_refs.append(self.id)
        return CheckResult(
            id=self.id,
            name=self.name,
            status=status,
            summary=summary,
            findings=findings,
            output_excerpt=output[-4000:],
        )


def run_check(check: Check, ctx: CheckContext) -> CheckResult:
    start = time.monotonic()
    try:
        res = check.run(ctx)
    except Exception as e:  # noqa: BLE001 - a crashing check must not abort the whole review
        res = CheckResult(
            id=check.id,
            name=check.name,
            status=CheckStatus.ERROR,
            summary=f"check crashed: {type(e).__name__}: {e}",
        )
    res.duration_s = round(time.monotonic() - start, 3)
    return res


def on_changed_line(ctx: CheckContext, path: str, line: int) -> bool:
    return ctx.diff.touches_line(path, line)
