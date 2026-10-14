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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from evals.dataset import Dataset, Item, ItemKind, Split
from evals.harness import RUNS_DIR, RunRecord
from evals.metrics import ItemScore, Summary, score_item, summarize
from watchman_review.schema import CheckStatus, Finding, ReviewResult, Source

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


def merge_restatements_v2(slack_same: int, slack_cross: int) -> Policy:
    """Like merge_restatements, but different categories must overlap (slack_cross) to count
    as the same defect; the same category may be a few lines apart (slack_same)."""

    def strength(f: Finding) -> tuple[int, int, float]:
        return (int(f.source is Source.DETERMINISTIC), f.severity.rank, f.confidence)

    def apply(fs: list[Finding]) -> list[Finding]:
        kept: list[Finding] = []
        for f in sorted(fs, key=strength, reverse=True):
            twin = any(
                near(f, k, slack_same if f.category == k.category else slack_cross) for k in kept
            )
            if f.source is not Source.DETERMINISTIC and twin:
                continue
            kept.append(f)
        return kept

    return apply


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
    "overlap-only + 0.6": compose(threshold(0.6), merge_restatements_v2(0, 0)),
    "same-cat 2, cross 0 + 0.6": compose(threshold(0.6), merge_restatements_v2(2, 0)),
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


def latest_dev_runs(
    mode: str | None, model: str | None, split: Split = Split.DEV
) -> dict[tuple[str, str], list[RunRecord]]:
    latest: dict[tuple[str, str, str, str, str], RunRecord] = {}
    for p in sorted(Path(RUNS_DIR).glob("*.json")):
        r = RunRecord.model_validate_json(p.read_text())
        if r.split is not split:
            continue
        if not r.dataset.endswith("-v1") or r.model is None:
            continue
        if (mode and r.mode.value != mode) or (model and r.model != model):
            continue
        latest[(r.dataset, r.mode.value, r.model, r.subset or "", r.overseer or "")] = r
    groups: dict[tuple[str, str], list[RunRecord]] = {}
    for (_, m, mdl, subset, ov), r in sorted(latest.items()):
        groups.setdefault(
            (
                f"{m} ({mdl})"
                + (f" +overseer({ov})" if ov else "")
                + (f" [{subset}]" if subset else ""),
                "",
            ),
            [],
        ).append(r)
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
    split: Annotated[Split, typer.Option()] = Split.DEV,
    pre_registered: Annotated[
        bool,
        typer.Option(
            help="Required for --split test: confirms the policy was committed before the "
            "test run and nothing is being tuned. The access is logged."
        ),
    ] = False,
) -> None:
    """Re-apply the *current* post-processing to saved runs and save them as new runs.

    Test-split runs may only be re-scored with the policy that was committed before the
    test run (--pre-registered), never with anything tuned afterwards.

    Equivalent to re-running with the LLM cache (same model responses, deterministic
    post-processing) without repeating hours of sandboxed test runs. The source run id is
    recorded in review_config["rescored_from"].
    """
    from evals.harness import ItemRecord, _watchman_commit, save_run
    from watchman_review.config import ReviewConfig
    from watchman_review.postprocess import drop_restatements, merge_colocated
    from watchman_review.postprocess import threshold as apply_threshold
    from watchman_review.schema import DroppedFinding, compute_verdict, dedupe

    if split is Split.TEST:
        if not pre_registered:
            typer.echo("re-scoring the test split requires --pre-registered", err=True)
            raise typer.Exit(2)
        from evals.harness import TEST_ACCESS_LOG

        with TEST_ACCESS_LOG.open("a") as fh:
            fh.write(
                f"{datetime.now(UTC).isoformat()} rescore pre-registered "
                f"min_confidence={ReviewConfig().min_confidence} (policy committed in a0524a6)\n"
            )
    cfg = ReviewConfig()
    for (label, _), runs in latest_dev_runs(mode, model, split).items():
        for run in runs:
            if run.review_config.get("rescored_from"):
                continue
            ds = {i.id: i for i in Dataset.load(run.dataset).items()}
            new_items: list[ItemRecord] = []
            for rec in run.items:
                if rec.result is None or rec.score.error:
                    new_items.append(rec)  # failed/incomplete items stay errors
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
        if r.split is Split.DEV and r.dataset.endswith("-v1") and not r.overseer:
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


_LEGACY_INCOMPLETE = (
    "agent LLM error", "agent stopped", "agent ended without submitting",
    "agent hit the turn limit", "unparseable",
)  # fmt: skip


@app.command()
def audit() -> None:
    """Flag items in saved runs whose model stage failed, from before `incomplete` existed.

    Rewrites the run files in place: affected items get `incomplete=True` and an error
    score, so they leave the rate calculations and `--retry-errors` can find them.
    """
    from evals.harness import ItemRecord, save_run
    from evals.metrics import error_score

    for p in sorted(Path(RUNS_DIR).glob("*.json")):
        run = RunRecord.model_validate_json(p.read_text())
        if not run.dataset.endswith("-v1") or run.review_config.get("audited"):
            continue
        ds = {i.id: i for i in Dataset.load(run.dataset).items()}
        flagged = 0
        items: list[ItemRecord] = []
        for rec in run.items:
            res = rec.result
            hit = [w for w in (res.warnings if res else []) if w.startswith(_LEGACY_INCOMPLETE)]
            if res is not None and not res.incomplete and not rec.score.error and hit:
                res = res.model_copy(
                    update={
                        "incomplete": True,
                        "incomplete_reasons": ["model stage did not finish"],
                    }
                )
                score = error_score(ds[rec.item_id], f"incomplete review: {hit[0]}"[:300],
                                    res.usage.cost_usd)  # fmt: skip
                rec = ItemRecord(item_id=rec.item_id, score=score, result=res)
                flagged += 1
            items.append(rec)
        run = run.model_copy(
            update={"items": items, "review_config": {**run.review_config, "audited": True}}
        )
        save_run(run)
        if flagged:
            typer.echo(f"{p.name}: flagged {flagged} incomplete item(s)")


@dataclass
class ArmStats:
    name: str
    summary: Summary
    clean_major: float | None
    triggered: float | None
    gained: int
    lost: int


def _arm_variants(rec_result: ReviewResult) -> tuple[ReviewResult, ReviewResult, bool] | None:
    """(primary-only, with-overseer, would_trigger) built from one 'always' run, or None."""
    primary = next((st for st in rec_result.stages if st.name == "primary"), None)
    if primary is None or primary.findings_before_overseer is None:
        return None
    only = rec_result.model_copy(
        update={
            "findings": primary.findings_before_overseer,
            "usage": primary.usage,
            "stages": [primary],
        }
    )
    return only, rec_result, bool(primary.trigger_reasons)


def overseer_arms(runs: list[RunRecord]) -> list[ArmStats]:
    """Paired arms from runs made with --overseer always: every arm sees the same primary
    review, so differences are due to the overseer alone."""
    arms: dict[str, tuple[list[Item], list[ItemScore], int, int, int, int, int]] = {}
    for run in runs:
        ds = {i.id: i for i in Dataset.load(run.dataset).items()}
        for rec in run.items:
            if rec.result is None or rec.score.error:
                continue
            variants = _arm_variants(rec.result)
            if variants is None:
                continue
            only, full, risky = variants
            item = ds[rec.item_id]
            picks = {"primary only": only, "overseer when risky": full if risky else only,
                     "overseer always": full}  # fmt: skip
            base_matched = {m.defect_index for m in score_item(item, only).matches}
            for name, res in picks.items():
                items, scores, trig, gain, lose, major, clean_n = arms.setdefault(
                    name, ([], [], 0, 0, 0, 0, 0)
                )
                sc = score_item(item, res)
                items.append(item)
                scores.append(sc)
                matched = {m.defect_index for m in sc.matches}
                if item.kind is ItemKind.CLEAN:
                    clean_n += 1
                    major += sum(1 for f in res.findings if f.severity.rank >= 2)
                arms[name] = (
                    items, scores, trig + (res is full), gain + len(matched - base_matched),
                    lose + len(base_matched - matched), major, clean_n,
                )  # fmt: skip
    out: list[ArmStats] = []
    for name in ("primary only", "overseer when risky", "overseer always"):
        if name not in arms:
            continue
        items, scores, trig, gain, lose, major, clean_n = arms[name]
        out.append(
            ArmStats(
                name,
                summarize(items, scores),
                major / clean_n if clean_n else None,
                trig / len(items) if items else None,
                gain,
                lose,
            )
        )
    return out


@app.command("overseer")
def overseer_report(
    split: Annotated[Split, typer.Option()] = Split.DEV,
    model: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Paired comparison of primary-only vs overseer arms from `--overseer always` runs."""
    latest: dict[tuple[str, str, str, str], RunRecord] = {}
    for p in sorted(Path(RUNS_DIR).glob("*.json")):
        r = RunRecord.model_validate_json(p.read_text())
        if r.split is split and r.overseer == "always" and (not model or r.model == model):
            latest[(r.dataset, r.mode.value, r.model or "", r.subset or "")] = r
    groups: dict[tuple[str, str], list[RunRecord]] = {}
    for (_, m, mdl, _), r in latest.items():
        groups.setdefault((m, mdl), []).append(r)
    for (m, mdl), runs in sorted(groups.items()):
        typer.echo(f"\n{m} / {mdl} ({split.value}): {', '.join(sorted(r.dataset for r in runs))}")
        typer.echo(
            f"  {'arm':22} {'recall':>7} {'strict':>7} {'prec':>5} {'FP/clean':>8} "
            f"{'FP major+':>9} {'ran':>5} {'+TP':>4} {'-TP':>4} {'$/rev':>7}"
        )
        for a in overseer_arms(runs):
            sm = a.summary
            typer.echo(
                f"  {a.name:22} {fmt(sm.recall):>7} {fmt(sm.recall_strict):>7} "
                f"{fmt(sm.precision):>5} {fmt(sm.fp_per_clean_diff, pct=False):>8} "
                f"{fmt(a.clean_major, pct=False):>9} {fmt(a.triggered):>5} {a.gained:>4} "
                f"{a.lost:>4} ${sm.mean_cost:>6.3f}"
            )


REASON_KEYS = {
    "primary review did not finish": "incomplete",
    "major/blocker": "major",
    "escalated": "escalated",
    "Layer 1 check": "layer1",
    "large diff": "large",
    "security-sensitive": "sensitive",
}


def reason_key(text: str) -> str:
    return next((k for frag, k in REASON_KEYS.items() if frag in text), "other")


def _always_runs(split: Split, model: str | None) -> list[RunRecord]:
    latest: dict[tuple[str, str, str, str], RunRecord] = {}
    for p in sorted(Path(RUNS_DIR).glob("*.json")):
        r = RunRecord.model_validate_json(p.read_text())
        if r.split is split and r.overseer == "always" and (not model or r.model == model):
            latest[(r.dataset, r.mode.value, r.model or "", r.subset or "")] = r
    return list(latest.values())


@dataclass
class Row2:
    items: list[Item] = field(default_factory=list)
    scores: list[ItemScore] = field(default_factory=list)
    ran: int = 0
    n: int = 0
    clean_major: int = 0
    clean_n: int = 0


def trigger_subsets(
    runs: list[RunRecord], subsets: dict[str, set[str] | None], layer1_failed_only: bool = True
) -> dict[str, Row2]:
    """Score 'overseer when risky' for different sets of allowed trigger reasons.

    None means always. An item uses the overseer result when any of its trigger reasons is in
    the allowed set. Everything comes from the same primary reviews.
    """
    out = {name: Row2() for name in subsets}
    for run in runs:
        ds = {i.id: i for i in Dataset.load(run.dataset).items()}
        for rec in run.items:
            if rec.result is None or rec.score.error:
                continue
            variants = _arm_variants(rec.result)
            if variants is None:
                continue
            only, full, _ = variants
            keys = {reason_key(t) for t in rec.result.stages[0].trigger_reasons}
            if layer1_failed_only:
                # Re-evaluate the Layer 1 trigger as the code now defines it: a check that
                # found problems (FAILED), not one that could not run (ERROR).
                keys.discard("layer1")
                if any(c.status is CheckStatus.FAILED for c in rec.result.checks):
                    keys.add("layer1")
            item = ds[rec.item_id]
            for name, allowed in subsets.items():
                use = full if (allowed is None or keys & allowed) else only
                row = out[name]
                row.n += 1
                row.ran += use is full
                row.items.append(item)
                row.scores.append(score_item(item, use))
                if item.kind is ItemKind.CLEAN:
                    row.clean_n += 1
                    row.clean_major += sum(1 for f in use.findings if f.severity.rank >= 2)
    return out


@app.command("overseer-triggers")
def overseer_triggers(
    split: Annotated[Split, typer.Option()] = Split.DEV,
    model: Annotated[str | None, typer.Option()] = None,
) -> None:
    """How often each risk trigger fires and what the overseer does for those items."""
    runs = _always_runs(split, model)
    all_keys = ["incomplete", "major", "escalated", "layer1", "large", "sensitive"]
    subsets: dict[str, set[str] | None] = {"primary only": set(), "all triggers": set(all_keys)}
    for k in all_keys:
        subsets[f"only {k}"] = {k}
        subsets[f"all but {k}"] = set(all_keys) - {k}
    subsets["always"] = None
    table = trigger_subsets(runs, subsets)
    typer.echo(f"  {'trigger set':22} {'ran':>5} {'recall':>7} {'strict':>7} {'prec':>5} "
               f"{'FP/clean':>8} {'$/rev':>7}")  # fmt: skip
    for name, row in table.items():
        sm = summarize(row.items, row.scores)
        typer.echo(
            f"  {name:22} {fmt(row.ran / row.n if row.n else None):>5} {fmt(sm.recall):>7} "
            f"{fmt(sm.recall_strict):>7} {fmt(sm.precision):>5} "
            f"{fmt(sm.fp_per_clean_diff, pct=False):>8} ${sm.mean_cost:>6.3f}"
        )


if __name__ == "__main__":
    app()
