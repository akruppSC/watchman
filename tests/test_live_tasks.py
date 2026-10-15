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

ALL_TASKS = load_tasks()
TASKS = [t for t in ALL_TASKS if t.suite != "vague"]  # vague tasks have no spec or reference


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


def test_tasks_have_unique_ids_and_the_added_ones_are_flagged() -> None:
    assert len(ALL_TASKS) == 16 and len({t.id for t in ALL_TASKS}) == 16
    assert {t.id for t in ALL_TASKS if t.post_hoc} == {"ticket_summary", "invoice_prices"}
    quality = {"shipping_cost", "command_interpreter", "invoice_rules"}
    assert {t.id for t in ALL_TASKS if t.suite == "quality"} == quality
    vague = {"vague_inventory", "vague_notes", "vague_gradebook"}
    assert {t.id for t in ALL_TASKS if t.suite == "vague"} == vague


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_reference_solution_is_correct_clean_and_strongly_tested(
    task: Task, tmp_path: Path
) -> None:
    work, base = build(task, tmp_path)
    result = evaluate(task, work, base, sys.executable)
    assert result["spec"]["failed"] == 0 and result["spec"]["error"] == 0, result  # type: ignore[index]
    assert result["correct"], result
    assert result["clean"], result["probe_hits"]
    if task.mutant_target:
        assert result["strong_tests"] is True, result["mutants_killed"]
        assert len(result["mutants_killed"]) >= 3  # type: ignore[arg-type]
    else:
        assert result["strong_tests"] is None


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_every_wrong_implementation_fails_the_hidden_spec(task: Task, tmp_path: Path) -> None:
    mutants = sorted((task.hidden / "mutants").glob("*.py"))
    if not task.mutant_target:
        return  # quality tasks are scored on maintainability, not on mutants
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
    for t in ALL_TASKS:
        assert "python -m pytest -q" in t.prompt and "commit your work" in t.prompt
        if t.suite == "pitfalls":  # quality prompts may use words like "duplicate sku" as spec
            assert not any(b in t.prompt.lower() for b in banned), t.id


@pytest.mark.parametrize("task", [t for t in TASKS if t.suite == "quality"], ids=lambda t: t.id)
def test_quality_tasks_have_a_reference_that_stays_inside_every_limit(
    task: Task, tmp_path: Path
) -> None:
    """The limits must be reachable: the reference splits the logic up, so a worker that
    ends over a limit chose to."""
    from evals.quality import measure

    work, base = build(task, tmp_path)
    diff = git(work, "diff", base, "HEAD")
    q = measure(work, diff)
    assert q.functions >= 4
    assert q.breaches == 0, q.detail


@pytest.mark.parametrize("task", [t for t in ALL_TASKS if t.suite == "vague"], ids=lambda t: t.id)
def test_vague_tasks_start_without_tests_and_say_nothing_about_quality(
    task: Task, tmp_path: Path
) -> None:
    """Open-ended on purpose: nothing hints at structure, style or standards, and the base
    repo has no tests, so passing needs the worker to write some."""
    assert not (task.hidden / "test_spec.py").exists()
    quality_words = ("quality", "clean", "standard", "ieee", "docstring", "maintain", "readable")
    assert not any(w in task.prompt.lower() for w in quality_words)
    work, base = build(task, tmp_path, overlay=False)
    result = evaluate(task, work, base, sys.executable)
    assert result["correct"] is False  # no worker tests yet: pytest collects nothing
