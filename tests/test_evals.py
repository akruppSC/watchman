from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from evals.dataset import (
    DATASETS_DIR,
    Dataset,
    DatasetError,
    Defect,
    InlineSource,
    Item,
    ItemKind,
    Split,
    assign_split,
    materialize,
)
from evals.harness import RunRecord, build_report, run_dataset, save_run
from evals.harness import app as harness_app
from evals.metrics import overlaps, score_item, summarize
from typer.testing import CliRunner

from tests.conftest import git
from watchman_review.config import Config
from watchman_review.llm.cache import CacheMode
from watchman_review.llm.fake import FakeClient
from watchman_review.llm.types import LLMResponse
from watchman_review.schema import (
    Category,
    Finding,
    ReviewMode,
    ReviewResult,
    Severity,
    Source,
    Verdict,
)


def finding(
    file: str, line: int, cat: Category = Category.CORRECTNESS, conf: float = 0.9
) -> Finding:
    return Finding(
        category=cat, severity=Severity.MAJOR, confidence=conf, file=file, line_start=line,
        line_end=line, summary=f"s{line}{cat}", evidence="e", source=Source.AGENT,
    )  # fmt: skip


def result(findings: list[Finding]) -> ReviewResult:
    return ReviewResult(
        mode=ReviewMode.AGENT, base="b", head="h", verdict=Verdict.WARN, findings=findings
    )


@pytest.fixture
def toy_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "datasets"
    shutil.copytree(DATASETS_DIR / "toy", dst / "toy")
    return dst


def test_split_assignment_is_deterministic() -> None:
    assert assign_split("x", "s") is assign_split("x", "s")
    splits = {assign_split(f"item-{i}", "seed") for i in range(50)}
    assert splits == {Split.DEV, Split.TEST}


def test_overlap_tolerance() -> None:
    d = Defect(file="a.py", line_start=10, line_end=12, category=Category.CORRECTNESS)
    assert overlaps(finding("a.py", 15), d)
    assert not overlaps(finding("a.py", 16), d)
    assert not overlaps(finding("b.py", 11), d)


def test_score_item_matches_each_defect_once() -> None:
    item = Item(
        id="i", kind=ItemKind.SEEDED, source=InlineSource(license="x", origin="y"),
        defects=[
            Defect(file="a.py", line_start=5, line_end=5, category=Category.SWALLOWED_ERROR),
            Defect(file="a.py", line_start=40, line_end=40, category=Category.SECURITY),
        ],
    )  # fmt: skip
    fs = [
        finding("a.py", 5, Category.SWALLOWED_ERROR, 0.9),
        finding("a.py", 6, Category.CORRECTNESS, 0.8),  # duplicate near defect 0 -> FP
        finding("a.py", 41, Category.CORRECTNESS, 0.7),  # right place, wrong category
        finding("z.py", 1),
    ]
    sc = score_item(item, result(fs))
    assert [(m.defect_index, m.category_match) for m in sc.matches] == [(0, True), (1, False)]
    assert len(sc.false_positive_ids) == 2
    s = summarize([item], [sc])
    assert (s.recall, s.recall_strict, s.precision) == (1.0, 0.5, 0.5)
    assert s.recall_by_category == {"swallowed_error": (1, 1), "security": (1, 1)}


def test_materialize_builds_base_and_head(toy_copy: Path, tmp_path: Path) -> None:
    ds = Dataset.load("toy", toy_copy)
    item = next(i for i in ds.items() if i.id == "seeded-001")
    repo = tmp_path / "r"
    base, head = materialize(ds, item, repo)
    changed = git(repo, "diff", "--name-only", f"{base}...{head}").split()
    assert changed == ["demo/io.py"]


def test_frozen_dataset_detects_tampering(toy_copy: Path) -> None:
    ds = Dataset.load("toy", toy_copy)
    ds.freeze()
    Dataset.load("toy", toy_copy).verify()
    p = toy_copy / "toy" / "items" / "clean-001" / "item.json"
    data = json.loads(p.read_text())
    data["task"] = "edited after freeze"
    p.write_text(json.dumps(data))
    with pytest.raises(DatasetError, match="frozen"):
        Dataset.load("toy", toy_copy).verify()


def test_run_dataset_deterministic_and_report(toy_copy: Path, tmp_path: Path) -> None:
    ds = Dataset.load("toy", toy_copy)
    cfg = Config()
    cfg.review.checks = ["secrets", "imports", "hygiene", "ruff", "mypy"]
    split = ds.split_of("seeded-001")
    run = run_dataset(ds, split=split, mode=ReviewMode.DETERMINISTIC, cfg=cfg, offline=True,
                      cache_dir=tmp_path / "cache")  # fmt: skip
    rec = next(r for r in run.items if r.item_id == "seeded-001")
    # Offline, the hallucinated import can't be confirmed as missing; ruff's unused-import
    # finding lands on the same line, so it matches leniently but not strictly.
    assert rec.score.error is None
    got = sorted((m.defect_index, m.category_match) for m in rec.score.matches)
    assert got == [(0, False), (1, True), (2, True)]
    runs_dir = tmp_path / "runs"
    save_run(run, runs_dir)
    table = build_report(runs_dir, toy_copy)
    assert "| toy |" in table
    assert "deterministic" in table


def test_run_dataset_baseline_uses_injected_client(toy_copy: Path, tmp_path: Path) -> None:
    ds = Dataset.load("toy", toy_copy)
    fake = FakeClient(responder=lambda req: LLMResponse(content='{"findings": []}'))
    split = ds.split_of("clean-001")
    run = run_dataset(ds, split=split, mode=ReviewMode.BASELINE, cfg=Config(), llm_client=fake,
                      llm_cache=CacheMode.OFF, cache_dir=tmp_path)  # fmt: skip
    rec = next(r for r in run.items if r.item_id == "clean-001")
    assert rec.score.n_findings == 0
    assert len(fake.requests) == len(run.items)


def test_parallel_run_matches_serial(toy_copy: Path, tmp_path: Path) -> None:
    ds = Dataset.load("toy", toy_copy)
    cfg = Config()
    cfg.review.checks = ["secrets", "hygiene", "ruff"]

    def go(jobs: int) -> RunRecord:
        return run_dataset(
            ds, split=Split.DEV, mode=ReviewMode.DETERMINISTIC, cfg=cfg, offline=True,
            cache_dir=tmp_path, jobs=jobs,
        )  # fmt: skip

    serial, parallel = go(1), go(4)
    assert [r.item_id for r in serial.items] == [r.item_id for r in parallel.items]
    assert [r.score.matches for r in serial.items] == [r.score.matches for r in parallel.items]


def test_sample_items_is_stratified_and_deterministic() -> None:
    from evals.harness import sample_items

    items = [
        Item(id=f"{t}-{i}", kind=ItemKind.SEEDED, source=InlineSource(license="x", origin="y"),
             meta={"template": t})
        for t in ("a", "b", "c") for i in range(5)
    ]  # fmt: skip
    picked = sample_items(items, 6)
    assert picked == sample_items(items, 6)
    assert sorted({p.meta["template"] for p in picked}) == ["a", "b", "c"]
    assert all(sum(p.meta["template"] == t for p in picked) == 2 for t in "abc")


def test_calibration_buckets() -> None:
    from evals.metrics import calibration

    item = Item(
        id="i", kind=ItemKind.SEEDED, source=InlineSource(license="x", origin="y"),
        defects=[Defect(file="a.py", line_start=5, line_end=5, category=Category.CORRECTNESS)],
    )  # fmt: skip
    res = result([finding("a.py", 5, conf=0.95), finding("z.py", 1, conf=0.65)])
    cal = calibration([(score_item(item, res), res)])
    assert ("0.9-1.0", 1, 1) in cal.buckets
    assert ("0.6-0.8", 0, 1) in cal.buckets


def test_harness_refuses_test_split_without_final() -> None:
    res = CliRunner().invoke(harness_app, ["run", "--split", "test"])
    assert res.exit_code == 2
    assert "--final" in res.output


def test_harness_freeze_requires_approval() -> None:
    res = CliRunner().invoke(harness_app, ["freeze", "--dataset", "toy"])
    assert res.exit_code == 2


def test_errors_are_excluded_from_rates_but_counted() -> None:
    from evals.metrics import error_score

    ok = Item(
        id="ok", kind=ItemKind.SEEDED, source=InlineSource(license="x", origin="y"),
        defects=[Defect(file="a.py", line_start=5, line_end=5, category=Category.CORRECTNESS)],
    )  # fmt: skip
    bad = ok.model_copy(update={"id": "bad"})
    res = result([finding("a.py", 5)])
    scores = [score_item(ok, res), error_score(bad, "incomplete review: x", 0.01)]
    s = summarize([ok, bad], scores)
    assert (s.n_items, s.n_errors, s.n_scored) == (2, 1, 1)
    assert s.recall == 1.0 and s.defects == 1  # the errored item's defect is not a miss
    assert s.cost_usd == pytest.approx(0.01)


def test_incomplete_result_becomes_error_record(toy_copy: Path, tmp_path: Path) -> None:
    ds = Dataset.load("toy", toy_copy)

    def boom(req: object) -> LLMResponse:
        from watchman_review.llm.types import LLMError

        raise LLMError("A Timeout Occurred")

    cfg = Config()
    cfg.review.checks = ["secrets"]
    run = run_dataset(
        ds, split=Split.DEV, mode=ReviewMode.AGENT, cfg=cfg, offline=True,
        llm_client=FakeClient(responder=boom), llm_cache=CacheMode.OFF, cache_dir=tmp_path,
        limit=2,
    )  # fmt: skip
    assert all(r.score.error and "incomplete review" in r.score.error for r in run.items)
    assert all(r.result is not None for r in run.items)  # kept for diagnosis


def test_merge_retry_replaces_only_errored_items(toy_copy: Path, tmp_path: Path) -> None:
    from evals.harness import merge_retry
    from evals.metrics import error_score

    ds = Dataset.load("toy", toy_copy)
    cfg = Config()
    cfg.review.checks = ["secrets"]
    full = run_dataset(
        ds, split=Split.DEV, mode=ReviewMode.DETERMINISTIC, cfg=cfg, offline=True,
        cache_dir=tmp_path,
    )  # fmt: skip
    victim = full.items[0]
    item = {i.id: i for i in ds.items()}[victim.item_id]
    broken = full.model_copy(
        update={"items": [victim.model_copy(update={"score": error_score(item, "boom")}),
                          *full.items[1:]]}
    )  # fmt: skip
    retried = run_dataset(
        ds, split=Split.DEV, mode=ReviewMode.DETERMINISTIC, cfg=cfg, offline=True,
        cache_dir=tmp_path, only_ids={victim.item_id},
    )  # fmt: skip
    merged = merge_retry(broken, retried)
    assert [r.item_id for r in merged.items] == [r.item_id for r in full.items]
    assert all(r.score.error is None for r in merged.items)
    assert merged.review_config["retried_from"] == broken.run_id


def test_overseer_arms_are_paired_and_count_gained_and_lost_true_positives() -> None:
    from evals.harness import ItemRecord, RunRecord
    from evals.metrics import score_item as score
    from evals.tune import overseer_arms

    from watchman_review.schema import StageSummary, Usage

    ds = Dataset.load("toy")
    item = next(i for i in ds.items() if i.id == "seeded-002")  # defects in two separate files
    dup = finding("textkit/articles.py", 15, Category.DUPLICATION)
    weak = finding("tests/test_articles.py", 6, Category.WEAK_TEST)
    noise = finding("textkit/slugs.py", 3, Category.COMMENT_NOISE)
    primary = StageSummary(
        name="primary", usage=Usage(cost_usd=0.01), trigger_reasons=["major finding"],
        findings_before_overseer=[dup, noise],
    )  # fmt: skip
    overseer = StageSummary(name="overseer", usage=Usage(cost_usd=0.10), ran=True)
    # The overseer rejected the duplication finding and the noise, and found the weak test.
    full = result([weak]).model_copy(
        update={"stages": [primary, overseer], "usage": Usage(cost_usd=0.11)}
    )
    run = RunRecord(
        run_id="r", started_at="x", dataset="toy", dataset_sha256=None, dataset_frozen=False,
        split=Split.DEV, mode=ReviewMode.AGENT, model="m", watchman_commit="c", review_config={},
        overseer="always",
        items=[ItemRecord(item_id="seeded-002", score=score(item, full), result=full)],
    )  # fmt: skip
    arms = {a.name: a for a in overseer_arms([run])}
    assert arms["primary only"].summary.found_lenient == 1
    assert (arms["overseer always"].gained, arms["overseer always"].lost) == (1, 1)
    assert arms["overseer always"].summary.cost_usd == pytest.approx(0.11)
    assert arms["primary only"].summary.cost_usd == pytest.approx(0.01)
    assert arms["overseer when risky"].triggered == 1.0

    quiet = full.model_copy(
        update={"stages": [primary.model_copy(update={"trigger_reasons": []}), overseer]}
    )
    run2 = run.model_copy(
        update={"items": [ItemRecord(item_id="seeded-002", score=score(item, quiet), result=quiet)]}
    )  # fmt: skip
    risky = {a.name: a for a in overseer_arms([run2])}["overseer when risky"]
    assert risky.triggered == 0.0 and risky.summary.cost_usd == pytest.approx(0.01)
    assert risky.summary.found_lenient == 1  # an untriggered item keeps the primary's findings
