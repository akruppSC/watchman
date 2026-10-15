"""The experiment's tasks must themselves be correct, or its numbers mean nothing.

For every task: a reference solution passes the hidden spec and the probes, the reference
tests kill every wrong implementation, and every wrong implementation really fails the spec.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from evals.live.evaluate import Task, evaluate, load_tasks, pytest_counts

from tests.conftest import git

TASKS = load_tasks()


def build(task: Task, tmp_path: Path, overlay: bool = True) -> tuple[Path, str]:
    work = tmp_path / "work"
    shutil.copytree(task.repo, work)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True)
    git(work, "config", "user.email", "t@e.com")
    git(work, "config", "user.name", "t")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    base = git(work, "rev-parse", "HEAD").strip()
    if overlay:
        shutil.copytree(task.reference, work, dirs_exist_ok=True)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "reference solution")
    return work, base


def test_there_are_eight_tasks_with_unique_ids() -> None:
    assert len(TASKS) == 8 and len({t.id for t in TASKS}) == 8


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_reference_solution_is_correct_clean_and_strongly_tested(
    task: Task, tmp_path: Path
) -> None:
    work, base = build(task, tmp_path)
    result = evaluate(task, work, base, sys.executable)
    assert result["spec"]["failed"] == 0 and result["spec"]["error"] == 0, result  # type: ignore[index]
    assert result["correct"], result
    assert result["clean"], result["probe_hits"]
    assert result["strong_tests"] is True, result["mutants_killed"]
    assert len(result["mutants_killed"]) >= 3  # type: ignore[arg-type]


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_every_wrong_implementation_fails_the_hidden_spec(task: Task, tmp_path: Path) -> None:
    mutants = sorted((task.hidden / "mutants").glob("*.py"))
    assert mutants
    for mutant in mutants:
        work = tmp_path / mutant.stem
        work.mkdir()
        w, _ = build(task, work)
        assert task.mutant_target
        shutil.copy(mutant, w / task.mutant_target)
        spec = tmp_path / f"spec_{mutant.stem}"
        shutil.copytree(w, spec)
        (spec / "_hidden").mkdir()
        shutil.copy(task.hidden / "test_spec.py", spec / "_hidden" / "test_spec.py")
        counts = pytest_counts(sys.executable, spec, "_hidden")
        assert counts["rc"] != 0, f"{mutant.name} is not actually wrong: the spec accepts it"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_the_untouched_repo_does_not_already_solve_the_task(task: Task, tmp_path: Path) -> None:
    work, base = build(task, tmp_path, overlay=False)
    result = evaluate(task, work, base, sys.executable)
    assert not result["correct"] or result["spec"]["rc"] != 0  # type: ignore[index]
    assert result["spec"]["rc"] != 0  # type: ignore[index]


def test_prompts_end_with_the_shared_instruction_and_name_no_pitfall() -> None:
    banned = ("swallow", "bare except", "weak test", "duplicate", "hallucinat", "scope creep")
    for t in TASKS:
        assert "python -m pytest -q" in t.prompt and "commit your work" in t.prompt
        assert not any(b in t.prompt.lower() for b in banned), t.id
