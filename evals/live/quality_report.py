"""Objective code-quality metrics on the saved live-experiment runs, alone vs supervised.

  uv run python -m evals.live.quality_report [--results GLOB] [--out evals/results/QUALITY.md]

Rebuilds each worker's final tree (task repo + saved final diff), measures what the worker
added (evals/quality.py), and compares arms. No model calls, so it is free and repeatable.
Runs whose saved diff was truncated and no longer applies are counted and skipped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any

import typer

from evals.live.evaluate import load_tasks
from evals.live.report import RESULTS, is_done, merge, sign_test
from evals.quality import measure

app = typer.Typer(add_completion=False)

METRICS = (
    ("max_complexity", "max complexity"),
    ("breaches", "functions over any limit (primary)"),
    ("over_complexity", "functions > 10 complexity"),
    ("over_length", "functions > 60 lines"),
    ("max_length", "longest function (lines)"),
    ("max_params", "most parameters"),
    ("max_nesting", "deepest nesting"),
    ("public_undocumented", "public API without docstring"),
    ("public_defs", "public functions/classes written"),
    ("functions", "functions written or edited"),
    ("duplicated_windows", "copy-pasted 5-line windows"),
    ("lint", "maintainability lint findings"),
    ("prod_lines_added", "production lines added"),
    ("tests_added", "tests added"),
)


def rebuild(task_repo: Path, diff: str, dest: Path) -> bool:
    shutil.copytree(task_repo, dest, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git"))
    if not diff.strip():
        return True
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        input=diff if diff.endswith("\n") else diff + "\n",
        cwd=dest, text=True, capture_output=True, check=False,
    )  # fmt: skip
    return proc.returncode == 0


def measure_runs(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    tasks = {t.id: t for t in load_tasks()}
    out: list[dict[str, Any]] = []
    skipped = 0
    for r in runs:
        if not is_done(r) or r["task"] not in tasks:
            continue
        recorded = r.get("quality")
        if recorded and "error" not in recorded:  # measured on the full diff at run time
            out.append(
                {
                    "task": r["task"],
                    "arm": r["arm"],
                    "trial": r["trial"],
                    "q": recorded,
                    "correct": bool(r["evaluation"]["correct"]),
                    "cost": r["worker_cost"] + r.get("watchman", {}).get("cost_usd", 0.0),
                }
            )
            continue
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "tree"
            if not rebuild(tasks[r["task"]].repo, r.get("final_diff", ""), dest):
                skipped += 1
                continue
            q = measure(dest, r.get("final_diff", ""))
        out.append(
            {
                "task": r["task"],
                "arm": r["arm"],
                "trial": r["trial"],
                "q": q.as_dict(),
                "correct": bool(r["evaluation"]["correct"]),
                "cost": r["worker_cost"] + r.get("watchman", {}).get("cost_usd", 0.0),
            }
        )
    return out, skipped


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def render(measured: list[dict[str, Any]], skipped: int, title: str = "") -> str:
    arms = ("alone", "watchman")
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in measured:
        by_arm[m["arm"]].append(m)
    lines = [
        f"## {title or 'Code quality of what the worker produced (objective metrics)'}",
        "",
        f"{len(measured)} runs measured ({skipped} skipped: saved diff no longer applies). "
        "Only functions the worker added or edited are measured (`evals/quality.py`). "
        "Cells are the mean per run; the last column counts paired runs "
        "(same task and trial) where supervised was lower / higher.",
        "",
        "| metric | alone | supervised | lower / higher (supervised) |",
        "|---|---|---|---|",
    ]
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for m in measured:
        pairs[(m["task"], m["trial"])][m["arm"]] = m["q"]
    for key, label in METRICS:
        vals = {a: [m["q"][key] for m in by_arm[a]] for a in arms}
        ps = [v for v in pairs.values() if set(arms) <= v.keys()]
        lo = sum(v["watchman"][key] < v["alone"][key] for v in ps)
        hi = sum(v["watchman"][key] > v["alone"][key] for v in ps)
        lines.append(
            f"| {label} | {mean(vals['alone']):.2f} | {mean(vals['watchman']):.2f} | "
            f"{lo} / {hi} (sign test p = {sign_test(lo, hi):.2f}) |"
        )
    ps = [v for v in pairs.values() if set(arms) <= v.keys()]
    if by_arm["alone"] and by_arm["watchman"]:
        for a in arms:
            ok = sum(m["correct"] for m in by_arm[a])
            lines.append(f"\n- {a}: {ok}/{len(by_arm[a])} runs correct (spec or own tests pass).")
        lines.append(f"- {len(ps)} paired runs (same task and trial).")
    return "\n".join(lines) + "\n"


def suites() -> dict[str, str]:
    return {t.id: t.suite for t in load_tasks()}


SUITE_TITLES = {
    "pitfalls": "Defect-pitfall tasks (experiment 1)",
    "quality": "Experiment 2A: specified tasks that invite long functions",
    "vague": "Experiment 2B: vague one-line tasks",
}


@app.command()
def main(
    out: Annotated[Path, typer.Option(help="Where to write the table")] = RESULTS / "QUALITY.md",
) -> None:
    files = sorted((RESULTS / "live").glob("*.json"))
    by_worker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in files:
        d = json.loads(f.read_text())
        by_worker[d["worker"]].append(d)
    body = ""
    for ds in by_worker.values():
        merged = merge(ds)
        measured, skipped = measure_runs(merged["runs"])
        of = suites()
        body += f"### Worker `{merged['worker']}`\n\n"
        for suite, title in SUITE_TITLES.items():
            part = [m for m in measured if of.get(m["task"]) == suite]
            if part:
                body += render(part, skipped if suite == "pitfalls" else 0, title) + "\n"
    out.write_text(
        "# Code quality measurements\n\nGenerated by `evals.live.quality_report`.\n\n" + body
    )
    typer.echo(f"wrote {out}")


if __name__ == "__main__":
    app()
