"""Objective evaluation of a worker's final repository state against a task.

spec      hidden acceptance tests (correct behavior, including the error paths)
visible   the repository's own tests, including the ones the worker wrote
probes    pitfall checks (swallowed errors, scope creep, hallucinated dependencies, ...)
mutants   wrong implementations swapped in, then the worker's tests run: a test suite that
          still passes against a wrong implementation is a weak one
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from evals.live.probes import run_probe

TASKS_DIR = Path(__file__).parent / "tasks"
_COUNT = re.compile(r"(\d+) (passed|failed|error|errors)")


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    pitfalls: list[str]
    probes: list[str]
    mutant_target: str | None
    post_hoc: bool = False  # added after seeing results; flagged in the report
    suite: str = "pitfalls"  # "pitfalls" (defects) or "quality" (maintainability)
    dir: Path = field(default=Path("."))

    @property
    def repo(self) -> Path:
        return self.dir / "repo"

    @property
    def hidden(self) -> Path:
        return self.dir / "hidden"

    @property
    def reference(self) -> Path:
        return self.dir / "reference"


def load_tasks(only: set[str] | None = None) -> list[Task]:
    out = []
    for p in sorted(TASKS_DIR.glob("*/task.json")):
        data = json.loads(p.read_text())
        if only and data["id"] not in only:
            continue
        keys = ("id", "title", "prompt", "pitfalls", "probes", "mutant_target")
        out.append(
            Task(
                dir=p.parent,
                post_hoc=bool(data.get("post_hoc")),
                suite=data.get("suite", "pitfalls"),
                **{k: data[k] for k in keys},
            )
        )
    return out


def pytest_counts(python: str, cwd: Path, *args: str, timeout: int = 120) -> dict[str, int]:
    env = {**os.environ, "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("VIRTUAL_ENV", None)
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *args],
            cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return {"passed": 0, "failed": 0, "error": 1, "rc": -9}
    counts = {"passed": 0, "failed": 0, "error": 0, "rc": proc.returncode}
    for n, kind in _COUNT.findall(proc.stdout.splitlines()[-1] if proc.stdout else ""):
        counts["error" if kind.startswith("error") else kind] += int(n)
    if proc.returncode not in (0, 1) and not (counts["passed"] or counts["failed"]):
        counts["error"] = max(counts["error"], 1)  # collection error, no tests, usage error
    return counts


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(
        src, dst, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", ".pytest_cache",
                                                ".watchman", ".opencode"),
    )  # fmt: skip


def evaluate(task: Task, workdir: Path, base_sha: str, python: str) -> dict[str, object]:
    spec_file = task.hidden / "test_spec.py"
    spec: dict[str, int]
    if spec_file.is_file():  # hidden acceptance tests, run on a copy so nothing leaks
        with tempfile.TemporaryDirectory(prefix="wm-eval-") as tmp:
            spec_dir = Path(tmp) / "repo"
            _copy_tree(workdir, spec_dir)
            (spec_dir / "_hidden").mkdir()
            shutil.copy(spec_file, spec_dir / "_hidden" / "test_spec.py")
            spec = pytest_counts(python, spec_dir, "_hidden")
    else:  # open-ended task: the only check is that the worker's own tests exist and pass
        spec = pytest_counts(python, workdir, "tests")
    visible = pytest_counts(python, workdir, "tests")
    probes = {p: run_probe(p, workdir, base_sha) for p in task.probes}

    mutants: dict[str, bool] = {}
    target = task.mutant_target
    if target and (workdir / target).is_file():
        for mutant in sorted((task.hidden / "mutants").glob("*.py")):
            with tempfile.TemporaryDirectory(prefix="wm-mut-") as tmp:
                mdir = Path(tmp) / "repo"
                _copy_tree(workdir, mdir)
                shutil.copy(mutant, mdir / target)
                mutants[mutant.stem] = pytest_counts(python, mdir, "tests")["rc"] != 0  # killed
    spec_total = spec["passed"] + spec["failed"] + spec["error"]
    correct = spec["rc"] == 0 and spec["passed"] > 0 and visible["rc"] == 0
    return {
        "spec": spec,
        "spec_pass_fraction": round(spec["passed"] / spec_total, 3) if spec_total else 0.0,
        "visible": visible,
        "probe_hits": {k: v for k, v in probes.items() if v},
        "mutants_killed": mutants,
        "correct": correct,
        "clean": not any(probes.values()),
        "strong_tests": bool(mutants) and all(mutants.values()) if task.mutant_target else None,
    }
