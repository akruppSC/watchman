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


@app.command("policies")
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


@app.command()
def rescore(
    mode: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Re-apply the *current* post-processing to saved dev runs and save them as new runs.

    Equivalent to re-running with the LLM cache (same model responses, deterministic
    post-processing) without repeating hours of sandboxed test runs. The source run id is
    recorded in review_config["rescored_from"].
    """
    from datetime import UTC, datetime

    from evals.harness import ItemRecord, _watchman_commit, save_run
    from watchman_review.config import ReviewConfig
    from watchman_review.postprocess import drop_restatements, merge_colocated
    from watchman_review.postprocess import threshold as apply_threshold
    from watchman_review.schema import DroppedFinding, compute_verdict, dedupe

    cfg = ReviewConfig()
    for (label, _), runs in latest_dev_runs(mode, model).items():
        for run in runs:
            if run.review_config.get("rescored_from"):
                continue
            ds = {i.id: i for i in Dataset.load(run.dataset).items()}
            new_items: list[ItemRecord] = []
            for rec in run.items:
                if rec.result is None:
                    new_items.append(rec)
                    continue
                res = rec.result
                kept_earlier = [d for d in res.dropped if not d.reason.startswith("confidence")]
                t = apply_threshold(candidates(res), cfg.min_confidence)
                r = drop_restatements(merge_colocated(t.kept))
                final = dedupe(r.kept)
                dropped = kept_earlier + [
                    DroppedFinding(finding=f, reason=why) for f, why in t.dropped + r.dropped
                ]
                new_res = res.model_copy(
                    update={
                        "findings": final,
                        "dropped": dropped,
                        "dropped_findings": len(dropped),
                        "verdict": compute_verdict(
                            final, block_min_confidence=cfg.block_min_confidence
                        ),
                    }
                )
                item = ds[rec.item_id]
                new_items.append(
                    ItemRecord(item_id=rec.item_id, score=score_item(item, new_res), result=new_res)
                )
            now = datetime.now(UTC)
            new_cfg = {**run.review_config, "min_confidence": cfg.min_confidence,
                       "rescored_from": run.run_id}  # fmt: skip
            out = run.model_copy(
                update={
                    "run_id": f"{now:%Y%m%dT%H%M%S}-" + run.run_id.split("-", 1)[1] + "-rescored",
                    "started_at": now.isoformat(),
                    "watchman_commit": _watchman_commit(),
                    "review_config": new_cfg,
                    "items": new_items,
                }
            )
            path = save_run(out)
            typer.echo(f"{label} {run.dataset}: rescored -> {path.name}")


@app.command()
def compare() -> None:
    """Compare every latest dev run on the item set shared with the smallest sample.

    Tiers ran on nested stratified samples (Opus 20 ⊂ GPT-5 40 ⊂ full dev), so each model
    is scored on exactly the same items here.
    """
    from evals.harness import ItemRecord

    latest: dict[tuple[str, str, str, str], RunRecord] = {}
    for p in sorted(Path(RUNS_DIR).glob("*.json")):
        r = RunRecord.model_validate_json(p.read_text())
        if r.split is Split.DEV and r.dataset.endswith("-v1"):
            latest[(r.dataset, r.mode.value, r.model or "-", r.subset or "")] = r
    labels = sorted({(m, mdl) for (_, m, mdl, _) in latest})
    for subset_size in ("sample-opus", "sample-gpt5"):
        # Item ids covered by the reference model's runs.
        ref_model = "anthropic/claude-opus-4.6" if subset_size == "sample-opus" else "openai/gpt-5"
        ids = {
            rec.item_id for (_, m, mdl, _), r in latest.items()
            if mdl == ref_model and m == "agent" for rec in r.items
        }  # fmt: skip
        if not ids:
            continue
        typer.echo(f"\n== items in the {ref_model} agent sample ({len(ids)} items)")
        typer.echo(
            f"  {'mode / model':44} {'A rec':>6} {'A strict':>8} {'B strict':>8} "
            f"{'prec':>5} {'FP/clean':>8} {'FP major+':>9} {'$/rev':>7}"
        )
        for mode, mdl in labels:
            recs: dict[str, list[ItemRecord]] = {}
            for (ds_name, m, model_, _), r in latest.items():
                if (m, model_) == (mode, mdl):
                    recs.setdefault(ds_name, []).extend(x for x in r.items if x.item_id in ids)
            n = sum(len(v) for v in recs.values())
            if n < len(ids):
                continue
            items_all: list[Item] = []
            scores = []
            per: dict[str, tuple[float | None, float | None]] = {}
            cost = 0.0
            clean_major = 0
            clean_n = 0
            for ds_name, rs in recs.items():
                ds = {i.id: i for i in Dataset.load(ds_name).items()}
                its = [ds[x.item_id] for x in rs]
                scs = [x.score for x in rs]
                items_all += its
                scores += scs
                s = summarize(its, scs)
                per[ds_name] = (s.recall, s.recall_strict)
                cost += sum(x.score.cost_usd for x in rs)
                if ds_name.startswith("clean"):
                    clean_n += len(rs)
                    clean_major += sum(
                        1 for x in rs if x.result
                        for f in x.result.findings if f.severity.rank >= 2
                    )  # fmt: skip
            tot = summarize(items_all, scores)
            a = per.get("seeded-v1", (None, None))
            b = per.get("reverted-v1", (None, None))
            typer.echo(
                f"  {mode + ' / ' + mdl:44} {fmt(a[0]):>6} {fmt(a[1]):>8} {fmt(b[1]):>8} "
                f"{fmt(tot.precision):>5} {fmt(tot.fp_per_clean_diff, pct=False):>8} "
                f"{fmt(clean_major / clean_n if clean_n else None, pct=False):>9} "
                f"${cost / n:>6.3f}"
            )


if __name__ == "__main__":
    app()
