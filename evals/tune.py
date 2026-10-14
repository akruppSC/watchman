"""Offline precision tuning on saved dev-split runs. No LLM calls; free to iterate.

Every saved run holds each item's final findings plus everything post-processing
dropped (with the reason), so candidate post-processing policies can be replayed
exactly. Refuses to read test-split runs: thresholds must never be tuned on the
held-out data.

  uv run python -m evals.tune                 # all latest dev runs with an LLM
  uv run python -m evals.tune --mode agent
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from evals.dataset import Dataset, Item, Split
from evals.harness import RUNS_DIR, RunRecord
from evals.metrics import score_item, summarize
from watchman_review.schema import Finding, ReviewResult, Source

app = typer.Typer(add_completion=False)

Policy = Callable[[list[Finding]], list[Finding]]


def candidates(result: ReviewResult) -> list[Finding]:
    """Findings before the confidence threshold: kept + threshold-dropped ones."""
    out = list(result.findings)
    out += [d.finding for d in result.dropped if d.reason.startswith("confidence")]
    return out


def threshold(min_conf: float) -> Policy:
    def apply(fs: list[Finding]) -> list[Finding]:
        return [f for f in fs if f.source is Source.DETERMINISTIC or f.confidence >= min_conf]

    return apply


def near(a: Finding, b: Finding, slack: int = 2) -> bool:
    if a.file is None or a.file != b.file or not a.line_start or not b.line_start:
        return False
    return a.line_start <= b.line_end + slack and b.line_start <= a.line_end + slack


def merge_restatements(fs: list[Finding]) -> list[Finding]:
    """Drop an LLM finding that sits on the same lines as a stronger finding.

    Stronger means deterministic, or else higher (severity, confidence). This models the
    restatement pattern seen on the toy set (one defect reported under two categories).
    """

    def strength(f: Finding) -> tuple[int, int, float]:
        return (f.source is Source.DETERMINISTIC, f.severity.rank, f.confidence)

    ordered = sorted(fs, key=strength, reverse=True)
    kept: list[Finding] = []
    for f in ordered:
        if f.source is not Source.DETERMINISTIC and any(near(f, k) for k in kept):
            continue
        kept.append(f)
    return kept


def compose(*ps: Policy) -> Policy:
    def apply(fs: list[Finding]) -> list[Finding]:
        for p in ps:
            fs = p(fs)
        return fs

    return apply


POLICIES: dict[str, Policy] = {
    "current (min_conf 0.5)": threshold(0.5),
    "min_conf 0.6": threshold(0.6),
    "min_conf 0.7": threshold(0.7),
    "min_conf 0.8": threshold(0.8),
    "merge restatements": compose(threshold(0.5), merge_restatements),
    "merge + min_conf 0.6": compose(threshold(0.6), merge_restatements),
    "merge + min_conf 0.7": compose(threshold(0.7), merge_restatements),
    "merge + min_conf 0.8": compose(threshold(0.8), merge_restatements),
}


@dataclass
class Row:
    recall: float | None
    precision: float | None
    fp_per_clean: float | None


def evaluate(runs: list[RunRecord], policy: Policy) -> Row:
    items: list[Item] = []
    scores = []
    for run in runs:
        ds = {i.id: i for i in Dataset.load(run.dataset).items()}
        for rec in run.items:
            if rec.result is None:
                continue
            item = ds[rec.item_id]
            result = rec.result.model_copy(update={"findings": policy(candidates(rec.result))})
            items.append(item)
            scores.append(score_item(item, result))
    s = summarize(items, scores)
    return Row(s.recall, s.precision, s.fp_per_clean_diff)


def latest_dev_runs(mode: str | None, model: str | None) -> dict[tuple[str, str], list[RunRecord]]:
    latest: dict[tuple[str, str, str, str], RunRecord] = {}
    for p in sorted(Path(RUNS_DIR).glob("*.json")):
        r = RunRecord.model_validate_json(p.read_text())
        if r.split is not Split.DEV:
            continue  # never tune on held-out data
        if not r.dataset.endswith("-v1") or r.model is None:
            continue
        if (mode and r.mode.value != mode) or (model and r.model != model):
            continue
        latest[(r.dataset, r.mode.value, r.model, r.subset or "")] = r
    groups: dict[tuple[str, str], list[RunRecord]] = {}
    for (_, m, mdl, subset), r in sorted(latest.items()):
        groups.setdefault((f"{m} ({mdl})" + (f" [{subset}]" if subset else ""), ""), []).append(r)
    return groups


def fmt(v: float | None, pct: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.0f}%" if pct else f"{v:.2f}"


@app.command()
def main(
    mode: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option()] = None,
) -> None:
    for (label, _), runs in latest_dev_runs(mode, model).items():
        kinds = sorted({r.dataset for r in runs})
        typer.echo(f"\n{label}: {', '.join(kinds)}")
        typer.echo(f"  {'policy':28} {'recall':>7} {'precision':>10} {'FP/clean':>9}")
        for name, policy in POLICIES.items():
            row = evaluate(runs, policy)
            typer.echo(
                f"  {name:28} {fmt(row.recall):>7} {fmt(row.precision):>10} "
                f"{fmt(row.fp_per_clean, pct=False):>9}"
            )


if __name__ == "__main__":
    app()
