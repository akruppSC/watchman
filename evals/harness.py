"""Eval harness: run a reviewer mode over a dataset, save results, build the results table.

  uv run python -m evals.harness run --dataset toy --mode deterministic
  uv run python -m evals.harness report            # regenerates evals/results/RESULTS.md

The held-out test split only runs with --final, and every such run is appended to
evals/results/test_access.log so any use of the test split stays auditable.
"""

from __future__ import annotations

import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from evals.dataset import Dataset, DatasetError, Split, materialize
from evals.metrics import ItemScore, Summary, error_score, score_item, summarize
from watchman_review.budget import Ledger
from watchman_review.config import Config
from watchman_review.llm.cache import CacheMode
from watchman_review.llm.types import LLMClient
from watchman_review.review import ReviewError, ReviewOptions, run_review
from watchman_review.schema import ReviewMode, ReviewResult

EVALS_DIR = Path(__file__).parent
RESULTS_DIR = EVALS_DIR / "results"
RUNS_DIR = RESULTS_DIR / "runs"
CACHE_DIR = EVALS_DIR / ".cache"
TEST_ACCESS_LOG = RESULTS_DIR / "test_access.log"

app = typer.Typer(add_completion=False, no_args_is_help=True)


class ItemRecord(BaseModel):
    item_id: str
    score: ItemScore
    result: ReviewResult | None


class RunRecord(BaseModel):
    run_id: str
    started_at: str
    dataset: str
    dataset_sha256: str | None
    dataset_frozen: bool
    split: Split
    mode: ReviewMode
    model: str | None
    watchman_commit: str
    review_config: dict[str, object]
    items: list[ItemRecord]


def _watchman_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=EVALS_DIR, capture_output=True,
            text=True, check=True,
        )  # fmt: skip
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", ":/src"], cwd=EVALS_DIR, capture_output=True,
            text=True, check=True,
        )  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def run_dataset(
    ds: Dataset,
    *,
    split: Split,
    mode: ReviewMode,
    cfg: Config,
    offline: bool = False,
    llm_client: LLMClient | None = None,
    llm_cache: CacheMode = CacheMode.READ_WRITE,
    limit: int | None = None,
    cache_dir: Path = CACHE_DIR,
    model: str | None = None,
    ledger: Ledger | None = None,
) -> RunRecord:
    ds.verify()
    items = ds.items(split)[:limit]
    records: list[ItemRecord] = []
    with tempfile.TemporaryDirectory(prefix="watchman-eval-") as tmp:
        for item in items:
            repo = Path(tmp) / item.id
            base, head = materialize(ds, item, repo)
            opts = ReviewOptions(
                base=base, head=head, mode=mode, model=model, task=item.task,
                offline=offline, llm_cache=llm_cache, llm_client=llm_client, ledger=ledger,
                cache_dir=cache_dir,
            )  # fmt: skip
            try:
                result = run_review(repo, opts, cfg)
            except ReviewError as e:
                records.append(ItemRecord(item_id=item.id, score=error_score(item, str(e)),
                                          result=None))  # fmt: skip
                continue
            records.append(
                ItemRecord(item_id=item.id, score=score_item(item, result), result=result)
            )
    now = datetime.now(UTC)
    used_model = (model or cfg.llm.model) if mode is not ReviewMode.DETERMINISTIC else None
    model_slug = f"-{used_model.replace('/', '_')}" if used_model else ""
    return RunRecord(
        run_id=f"{now:%Y%m%dT%H%M%S}-{ds.manifest.name}-{split}-{mode}{model_slug}",
        started_at=now.isoformat(),
        dataset=ds.manifest.name,
        dataset_sha256=ds.manifest.content_sha256,
        dataset_frozen=ds.manifest.frozen,
        split=split,
        mode=mode,
        model=used_model,
        watchman_commit=_watchman_commit(),
        review_config=cfg.review.model_dump(mode="json"),
        items=records,
    )


def save_run(run: RunRecord, runs_dir: Path = RUNS_DIR) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run.run_id}.json"
    path.write_text(run.model_dump_json(indent=2) + "\n")
    return path


def _fmt(v: float | None, pct: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.0f}%" if pct else f"{v:.2f}"


def summary_row(run: RunRecord, ds: Dataset) -> tuple[str, ...]:
    items = {i.id: i for i in ds.items()}
    s: Summary = summarize([items[r.item_id] for r in run.items], [r.score for r in run.items])
    return (
        run.dataset, run.split.value, run.mode.value, run.model or "-", str(s.n_items),
        _fmt(s.recall), _fmt(s.recall_strict), _fmt(s.precision),
        _fmt(s.fp_per_clean_diff, pct=False), f"${s.mean_cost:.4f}",
        f"{s.mean_latency:.1f}s", str(s.n_errors), run.watchman_commit,
    )  # fmt: skip


HEADER = (
    "dataset", "split", "mode", "model", "items", "recall", "recall (strict)", "precision",
    "findings/clean diff", "cost/review", "latency", "errors", "watchman",
)  # fmt: skip


def build_report(runs_dir: Path = RUNS_DIR, datasets_dir: Path | None = None) -> str:
    runs = [RunRecord.model_validate_json(p.read_text()) for p in sorted(runs_dir.glob("*.json"))]
    latest: dict[tuple[str, str, str, str], RunRecord] = {}
    for r in runs:  # sorted by run_id (timestamp first), so later runs win
        latest[(r.dataset, r.split, r.mode, r.model or "")] = r
    lines = [
        "# Eval results",
        "",
        "Generated by `uv run python -m evals.harness report`. Do not edit by hand.",
        "Latest run per (dataset, split, mode, model). Raw runs: `evals/results/runs/`.",
        "",
        "| " + " | ".join(HEADER) + " |",
        "|" + "---|" * len(HEADER),
    ]
    for key in sorted(latest):
        r = latest[key]
        ds = Dataset.load(r.dataset, datasets_dir) if datasets_dir else Dataset.load(r.dataset)
        lines.append("| " + " | ".join(summary_row(r, ds)) + " |")
    lines += [
        "",
        "recall: defects matched by file and overlapping lines (±3). strict: category must "
        "also match. precision: matched / all findings on defect items (pessimistic). "
        "findings/clean diff: false-positive rate on known-good changes.",
    ]
    return "\n".join(lines) + "\n"


@app.command()
def run(
    dataset: Annotated[str, typer.Option()] = "toy",
    mode: Annotated[ReviewMode, typer.Option()] = ReviewMode.DETERMINISTIC,
    split: Annotated[Split, typer.Option()] = Split.DEV,
    final: Annotated[bool, typer.Option(help="Required to touch the held-out test split.")] = False,
    offline: Annotated[bool, typer.Option()] = False,
    limit: Annotated[int | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option(help="Override the configured model.")] = None,
) -> None:
    """Run one reviewer mode over a dataset split and save the run.

    Real LLM spend is recorded in the user ledger and counts toward the monthly cap.
    """
    if split is Split.TEST and not final:
        typer.echo("refusing to run on the held-out test split without --final", err=True)
        raise typer.Exit(2)
    from watchman_review.config import load_config

    cfg, _ = load_config(None)
    try:
        ds = Dataset.load(dataset)
        if split is Split.TEST:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            with TEST_ACCESS_LOG.open("a") as fh:
                fh.write(f"{datetime.now(UTC).isoformat()} dataset={dataset} mode={mode} "
                         f"watchman={_watchman_commit()}\n")  # fmt: skip
        record = run_dataset(
            ds,
            split=split,
            mode=mode,
            cfg=cfg,
            offline=offline,
            limit=limit,
            model=model,
            ledger=Ledger(),
        )
    except DatasetError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    path = save_run(record)
    typer.echo(" | ".join(summary_row(record, ds)))
    typer.echo(f"saved {path}")


@app.command()
def report() -> None:
    """Regenerate evals/results/RESULTS.md from saved runs."""
    out = RESULTS_DIR / "RESULTS.md"
    out.write_text(build_report())
    typer.echo(f"wrote {out}")


@app.command()
def freeze(
    dataset: Annotated[str, typer.Option()],
    owner_approved: Annotated[
        bool, typer.Option("--owner-approved", help="Confirm the owner approved freezing.")
    ] = False,
) -> None:
    """Freeze a dataset: record splits and content hash. Requires owner approval."""
    if not owner_approved:
        typer.echo("freezing requires --owner-approved", err=True)
        raise typer.Exit(2)
    m = Dataset.load(dataset).freeze()
    typer.echo(f"froze {m.name}: sha256={m.content_sha256}")


@app.command("list")
def list_items(dataset: Annotated[str, typer.Option()] = "toy") -> None:
    """List dataset items with their split."""
    ds = Dataset.load(dataset)
    for item in ds.items():
        typer.echo(f"{item.id:20} {item.kind.value:9} {ds.split_of(item.id).value:5} "
                   f"defects={len(item.defects)}")  # fmt: skip


if __name__ == "__main__":
    app()
