"""Eval harness: run a reviewer mode over a dataset, save results, build the results table.

  uv run python -m evals.harness run --dataset toy --mode deterministic
  uv run python -m evals.harness report            # regenerates evals/results/RESULTS.md

The held-out test split only runs with --final, and every such run is appended to
evals/results/test_access.log so any use of the test split stays auditable.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel

from evals.dataset import Dataset, DatasetError, Item, Split, materialize, strata_key
from evals.metrics import (
    ItemScore,
    Summary,
    calibration,
    error_score,
    score_item,
    summarize,
)
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
    subset: str | None = None
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


def sample_items(items: list[Item], n: int, seed: str = "sample") -> list[Item]:
    """Deterministic sample of n items that round-robins across strata (template/repo)."""
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(strata_key(it), []).append(it)
    for key, group in groups.items():
        group.sort(key=lambda it: hashlib.sha256(f"{seed}:{it.id}".encode()).hexdigest())
        groups[key] = group
    out: list[Item] = []
    while len(out) < min(n, len(items)):
        for key in sorted(groups):
            if groups[key] and len(out) < n:
                out.append(groups[key].pop(0))
    return sorted(out, key=lambda it: it.id)


def _review_item(
    ds: Dataset, item: Item, root: Path, mode: ReviewMode, cfg: Config, opts_kw: dict[str, Any]
) -> ItemRecord:
    repo = root / item.id
    try:
        base, head = materialize(ds, item, repo)
        opts = ReviewOptions(base=base, head=head, mode=mode, task=item.task, **opts_kw)
        result = run_review(repo, opts, cfg)
    except (ReviewError, subprocess.CalledProcessError) as e:
        return ItemRecord(item_id=item.id, score=error_score(item, str(e)), result=None)
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    return ItemRecord(item_id=item.id, score=score_item(item, result), result=result)


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
    sample: int | None = None,
    cache_dir: Path = CACHE_DIR,
    model: str | None = None,
    ledger: Ledger | None = None,
    jobs: int = 1,
    on_item: Callable[[ItemRecord], None] | None = None,
) -> RunRecord:
    ds.verify()
    items = ds.items(split)
    if sample is not None:
        items = sample_items(items, sample)
    items = items[:limit]
    opts_kw: dict[str, Any] = {
        "model": model, "offline": offline, "llm_cache": llm_cache, "llm_client": llm_client,
        "ledger": ledger, "cache_dir": cache_dir,
    }  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="watchman-eval-") as tmp:
        root = Path(tmp)
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = {
                pool.submit(_review_item, ds, it, root, mode, cfg, opts_kw): it for it in items
            }
            by_id: dict[str, ItemRecord] = {}
            for fut in as_completed(futures):
                rec = fut.result()
                by_id[rec.item_id] = rec
                if on_item:
                    on_item(rec)
    records = [by_id[it.id] for it in items]
    now = datetime.now(UTC)
    used_model = (model or cfg.llm.model) if mode is not ReviewMode.DETERMINISTIC else None
    model_slug = f"-{used_model.replace('/', '_')}" if used_model else ""
    subset = f"sample{sample}" if sample is not None else None
    return RunRecord(
        run_id=f"{now:%Y%m%dT%H%M%S}-{ds.manifest.name}-{split}-{mode}{model_slug}"
        + (f"-{subset}" if subset else ""),
        started_at=now.isoformat(),
        dataset=ds.manifest.name,
        dataset_sha256=ds.manifest.content_sha256,
        dataset_frozen=ds.manifest.frozen,
        split=split,
        mode=mode,
        model=used_model,
        watchman_commit=_watchman_commit(),
        review_config=cfg.review.model_dump(mode="json"),
        subset=subset,
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


def _label(r: RunRecord) -> str:
    label = r.mode.value + (f" ({r.model})" if r.model else "")
    return label + (f" [{r.subset}]" if r.subset else "")


def build_report(runs_dir: Path = RUNS_DIR, datasets_dir: Path | None = None) -> str:
    runs = [RunRecord.model_validate_json(p.read_text()) for p in sorted(runs_dir.glob("*.json"))]
    latest: dict[tuple[str, str, str, str, str], RunRecord] = {}
    for r in runs:  # sorted by run_id (timestamp first), so later runs win
        latest[(r.dataset, r.split, r.mode, r.model or "", r.subset or "")] = r

    def load(name: str) -> Dataset:
        return Dataset.load(name, datasets_dir) if datasets_dir else Dataset.load(name)

    lines = [
        "# Eval results",
        "",
        "Generated by `uv run python -m evals.harness report`. Do not edit by hand.",
        "Latest run per (dataset, split, mode, model, subset). Raw runs: `evals/results/runs/`.",
        "",
        "| " + " | ".join(HEADER) + " |",
        "|" + "---|" * len(HEADER),
    ]
    for key in sorted(latest):
        r = latest[key]
        lines.append("| " + " | ".join(summary_row(r, load(r.dataset))) + " |")
    lines += [
        "",
        "recall: defects matched by file and overlapping lines (±3). strict: category must "
        "also match. precision: matched / all findings on defect items (pessimistic). "
        "findings/clean diff: false-positive rate on known-good changes.",
    ]

    # Recall by category, per dataset with defects.
    by_dataset: dict[tuple[str, str], list[RunRecord]] = {}
    for (ds_name, split, *_), r in sorted(latest.items()):
        by_dataset.setdefault((ds_name, split), []).append(r)
    for (ds_name, split), rs in by_dataset.items():
        ds = load(ds_name)
        items = {i.id: i for i in ds.items()}
        cats = sorted({d.category.value for i in items.values() for d in i.defects})
        if not cats:
            continue
        lines += ["", f"## Recall by category: {ds_name} ({split})", ""]
        lines.append("| category | " + " | ".join(_label(r) for r in rs) + " |")
        lines.append("|---|" + "---|" * len(rs))
        sums = [
            summarize([items[x.item_id] for x in r.items], [x.score for x in r.items]) for r in rs
        ]
        for cat in cats:
            cells = []
            for s in sums:
                found, total = s.recall_by_category.get(cat, (0, 0))
                cells.append(f"{found}/{total}" if total else "-")
            lines.append(f"| {cat} | " + " | ".join(cells) + " |")

    # Calibration for LLM runs on defect datasets.
    cal_lines: list[str] = []
    for r in (r for r in latest.values() if r.model):
        pairs = [(x.score, x.result) for x in r.items if x.result is not None]
        cal = calibration(pairs)
        if not any(n for _, _, n in cal.buckets):
            continue
        buckets = " ".join(f"{lbl}: {tp}/{n}" for lbl, tp, n in cal.buckets if n)
        sev = (
            f"{cal.mean_severity_tp:.2f} vs {cal.mean_severity_fp:.2f}"
            if cal.mean_severity_tp is not None and cal.mean_severity_fp is not None
            else "-"
        )
        cal_lines.append(f"| {r.dataset} ({r.split}) | {_label(r)} | {buckets} | {sev} |")
    if cal_lines:
        lines += [
            "",
            "## Calibration (defect datasets)",
            "",
            "Precision per confidence bucket (matched/total). Severity rank: "
            "blocker=3, major=2, minor=1, nit=0; true positives vs false positives.",
            "",
            "| dataset | run | precision by confidence | mean severity TP vs FP |",
            "|---|---|---|---|",
            *sorted(cal_lines),
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
    sample: Annotated[
        int | None, typer.Option(help="Stratified deterministic sample of N items.")
    ] = None,
    model: Annotated[str | None, typer.Option(help="Override the configured model.")] = None,
    jobs: Annotated[int, typer.Option(help="Items reviewed in parallel.")] = 1,
    checks: Annotated[
        str | None, typer.Option(help="Comma-separated Layer 1 checks (default: config).")
    ] = None,
) -> None:
    """Run one reviewer mode over a dataset split and save the run.

    Real LLM spend is recorded in the user ledger and counts toward the monthly cap.
    """
    if split is Split.TEST and not final:
        typer.echo("refusing to run on the held-out test split without --final", err=True)
        raise typer.Exit(2)
    from watchman_review.config import load_config

    cfg, _ = load_config(None)
    if checks is not None:
        cfg.review.checks = [c.strip() for c in checks.split(",") if c.strip()]
    done = 0

    def progress(rec: ItemRecord) -> None:
        nonlocal done
        done += 1
        sc = rec.score
        status = f"error: {sc.error[:80]}" if sc.error else (
            f"{len(sc.matches)}/{sc.n_defects} found, {sc.n_findings} findings, ${sc.cost_usd:.3f}"
        )  # fmt: skip
        typer.echo(f"[{done}] {rec.item_id}: {status}", err=True)

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
            sample=sample,
            model=model,
            ledger=Ledger(),
            jobs=jobs,
            on_item=progress,
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
