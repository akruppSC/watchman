from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest
from evals.build import mutators as m
from evals.dataset import (
    Dataset,
    Defect,
    GitSource,
    InlineSource,
    Item,
    ItemKind,
    LineRange,
    Split,
    materialize,
    stratified_splits,
)
from evals.metrics import overlaps

from tests.conftest import commit_files, git
from watchman_review.schema import Category, Finding, Severity, Source

SRC = """\
import os

from pkg.util import clamp


def load(path, retries=3, strict=False):
    if path is not None:
        data = os.stat(path)
    try:
        value = int(data.st_size)
    except ValueError:
        value = 0
    if value < 10:
        raise ValueError("too small")
    total = 0
    total += clamp(value, 0, 100)
    os.utime(path)
    return total
"""

UTIL = '''\
def clamp(x, lo, hi):
    """Clamp x."""
    return max(lo, min(x, hi))


def other(items):
    result = []
    for item in items:
        result.append(item)
    return result
'''

TEST = """\
from pkg.mod import load


def test_load():
    assert load("x") == 3
    assert len([1, 2]) == 2
"""


def make_ctx(tmp_path: Path, *, prior_def: bool = True, seed: int = 0) -> m.Ctx:
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "__init__.py").write_text("")
    (tree / "pkg" / "mod.py").write_text(SRC)
    (tree / "pkg" / "util.py").write_text(UTIL)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_mod.py").write_text(TEST)
    n = len(SRC.splitlines())
    added = set(range(7 if prior_def else 1, n + 1))  # def line pre-exists when prior_def
    return m.Ctx(
        tree=tree,
        src=[m.FileChange("pkg/mod.py", SRC, added)],
        tests=[m.FileChange("tests/test_mod.py", TEST, set(range(1, 7)))],
        package_files=["pkg/__init__.py", "pkg/mod.py", "pkg/util.py"],
        rng=random.Random(seed),
    )


def apply(ctx: m.Ctx, template: str) -> tuple[m.Mutation, dict[str, list[str]]]:
    mut = m.TEMPLATES[template](ctx)
    assert mut is not None, template
    for text in mut.edits.values():
        ast.parse(text)
    return mut, {p: t.splitlines() for p, t in mut.edits.items()}


def line(files: dict[str, list[str]], loc: m.Location) -> str:
    return files[loc.file][loc.line_start - 1]


def test_swallow_exception(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "swallow_exception")
    assert mut.category is Category.SWALLOWED_ERROR
    block = files[mut.primary.file][mut.primary.line_start - 1 : mut.primary.line_end]
    assert block[0].strip() == "try:"
    assert block[2].strip() == "except Exception:"


def test_broaden_except(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "broaden_except")
    assert line(files, mut.primary).strip() == "except Exception:"


def test_raise_to_return(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "raise_to_return")
    assert line(files, mut.primary).strip() == "return None"
    assert mut.category is Category.SILENT_BEHAVIOR_CHANGE


def test_flip_comparison(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "flip_comparison")
    assert line(files, mut.primary).strip() == "if value <= 10:"


def test_negate_condition_inverts_operator(tmp_path: Path) -> None:
    for seed in range(10):
        mut, files = apply(make_ctx(tmp_path / str(seed), seed=seed), "negate_condition")
        text = line(files, mut.primary).strip()
        assert text in {"if path is None:", "if not (value < 10):"}


def test_change_default_only_existing_functions(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path, prior_def=True), "change_default")
    assert "retries=4" in line(files, mut.primary) or "strict=True" in line(files, mut.primary)
    assert m.change_default(make_ctx(tmp_path / "new", prior_def=False)) is None


def test_hallucinated_package_locations(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path, prior_def=False), "hallucinated_package")
    assert line(files, mut.primary).startswith("from ")
    assert line(files, mut.alternates[0]).startswith("@")


def test_hallucinated_stdlib_attr(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path, prior_def=False), "hallucinated_stdlib_attr")
    assert line(files, mut.primary).startswith("@functools.")
    assert line(files, mut.alternates[0]) == "import functools"


def test_weaken_test_requires_all_asserts(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "weaken_test")
    assert mut.category is Category.WEAK_TEST
    test_lines = [ln.strip() for ln in files["tests/test_mod.py"]]
    assert 'assert load("x") is not None' in test_lines
    assert "assert len([1, 2]) is not None" in test_lines


def test_weaken_test_skips_when_a_real_assert_remains(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    text = TEST + '    assert load("y") > 2\n'
    ctx.tests = [m.FileChange("tests/test_mod.py", text, set(range(1, 8)))]
    assert m.weaken_test(ctx) is None


def test_comment_noise(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "comment_noise")
    for loc in [mut.primary, *mut.alternates]:
        assert line(files, loc).strip().startswith("# ")


def test_duplicate_helper(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "duplicate_helper")
    assert line(files, mut.primary) == "def _clamp_impl(x, lo, hi):"
    assert "_clamp_impl(value, 0, 100)" in line(files, mut.alternates[0])


def test_scope_creep_rename_touches_other_module(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path), "scope_creep_rename")
    assert mut.primary.file == "pkg/util.py"
    assert "res = []" in [ln.strip() for ln in files["pkg/util.py"]]


def test_prompt_injection(tmp_path: Path) -> None:
    mut, files = apply(make_ctx(tmp_path, prior_def=False), "prompt_injection")
    assert "AI" in line(files, mut.primary)


def test_schedule_covers_registered_templates() -> None:
    assert set(m.SCHEDULE) == set(m.TEMPLATES)
    assert sum(m.SCHEDULE.values()) == 60


# --- splits, matching, materialization -----------------------------------------


def _item(i: int, template: str) -> Item:
    return Item(
        id=f"item-{template}-{i}", kind=ItemKind.SEEDED,
        source=InlineSource(license="x", origin="y"), meta={"template": template},
    )  # fmt: skip


def test_stratified_splits_balance_each_template() -> None:
    items = [_item(i, t) for t in ("a", "b") for i in range(10)]
    splits = stratified_splits(items, "seed")
    for t in ("a", "b"):
        n_test = sum(splits[f"item-{t}-{i}"] is Split.TEST for i in range(10))
        assert n_test == 3
    assert splits == stratified_splits(items, "seed")


def test_alt_ranges_match() -> None:
    d = Defect(
        file="a.py", line_start=5, line_end=5, category=Category.HALLUCINATED_DEPENDENCY,
        alt_ranges=[LineRange(file="a.py", line_start=100, line_end=100)],
    )  # fmt: skip
    f = Finding(
        category=Category.HALLUCINATED_DEPENDENCY, severity=Severity.MAJOR, confidence=0.9,
        file="a.py", line_start=101, line_end=101, summary="s", evidence="e", source=Source.AGENT,
    )  # fmt: skip
    assert overlaps(f, d)


def test_materialize_git_item_hides_future(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = commit_files(git_repo, {"a.py": "x = 1\n"}, "base")
    fix = commit_files(git_repo, {"a.py": "x = 2\n"}, "Fix the bug")
    cache = tmp_path / "cache"
    cache.mkdir()
    git(git_repo, "clone", "-q", str(git_repo), str(cache / "demo"))
    monkeypatch.setattr("evals.build.repos.CACHE", cache)

    root = tmp_path / "ds"
    item_dir = root / "items" / "demo-revert"
    item_dir.mkdir(parents=True)
    (root / "manifest.json").write_text('{"name": "ds"}')
    (item_dir / "base.patch").write_text(git(git_repo, "diff", base, fix))
    (item_dir / "change.patch").write_text(git(git_repo, "diff", fix, base))
    item = Item(
        id="demo-revert", kind=ItemKind.REVERTED, task="Simplify a",
        source=GitSource(repo="demo", url="unused", license="x", base_commit=base,
                         upstream_commit=fix, origin="test"),
    )  # fmt: skip
    (item_dir / "item.json").write_text(item.model_dump_json())

    dest = tmp_path / "out"
    b, h = materialize(Dataset(root), item, dest)
    assert git(dest, "show", f"{b}:a.py") == "x = 2\n"
    assert git(dest, "show", f"{h}:a.py") == "x = 1\n"
    log = git(dest, "log", "--format=%s%n%H", "--all")
    assert "Fix the bug" not in log
    assert fix not in log
    assert set(git(dest, "branch", "--format=%(refname:short)").split()) == {"base", "head"}
