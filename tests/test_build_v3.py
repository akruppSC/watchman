from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest
from evals.build import mutators as m
from evals.build.make_datasets import BuildError, init_dataset
from evals.build.mutators_v3 import SCHEDULE_V3, TEMPLATES_V3
from evals.dataset import DATASETS_DIR

from watchman_review.schema import Category

SRC = """\
import os

from pkg.util import clamp


def load(path, retries=3, strict=False):
    row_count = 0
    if path is not None:
        data = os.stat(path)
        row_count = int(data.st_size)
    total = clamp(row_count, 0, 100)
    return total + row_count
"""


def make_ctx(tmp_path: Path, seed: int = 0) -> m.Ctx:
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "util.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n")
    (tree / "pkg" / "mod.py").write_text(SRC)
    n = len(SRC.splitlines())
    return m.Ctx(
        tree=tree,
        src=[m.FileChange("pkg/mod.py", SRC, set(range(1, n + 1)))],
        tests=[],
        package_files=["pkg/mod.py", "pkg/util.py"],
        rng=random.Random(seed),
    )


def apply(tmp_path: Path, name: str) -> tuple[m.Mutation, list[str]]:
    mut = TEMPLATES_V3[name](make_ctx(tmp_path))
    assert mut is not None, name
    (text,) = mut.edits.values()
    ast.parse(text)
    return mut, text.splitlines()


def test_v3_adds_templates_without_touching_v1() -> None:
    assert set(SCHEDULE_V3) == set(TEMPLATES_V3) and sum(SCHEDULE_V3.values()) == 30
    assert not set(TEMPLATES_V3) & set(m.TEMPLATES)  # v1/v2 templates and schedules unchanged


def test_needless_wrapper_routes_a_call_through_a_pass_through(tmp_path: Path) -> None:
    mut, lines = apply(tmp_path, "needless_wrapper")
    assert mut.category is Category.OVER_ABSTRACTION
    block = lines[mut.primary.line_start - 1 : mut.primary.line_end]
    assert block[0].startswith("def _call_clamp(*args, **kwargs):")
    assert "return clamp(*args, **kwargs)" in block[1]
    assert any("_call_clamp(row_count" in ln for ln in lines)
    alt = mut.alternates[0]
    assert "_call_clamp(" in lines[alt.line_start - 1]


def test_unused_parameter_adds_a_kwonly_param_nothing_reads(tmp_path: Path) -> None:
    mut, lines = apply(tmp_path, "unused_parameter")
    sig = lines[mut.primary.line_start - 1]
    assert sig.startswith("def load(") and ", *, " in sig and "=None):" in sig
    name = sig.split("*, ")[1].split("=")[0]
    assert sum(name in ln for ln in lines) == 1  # only the signature mentions it


def test_dead_code_adds_an_unreferenced_alias(tmp_path: Path) -> None:
    mut, lines = apply(tmp_path, "dead_code")
    block = lines[mut.primary.line_start - 1 : mut.primary.line_end]
    assert block[0] == "def _load_legacy(*args, **kwargs):"
    assert sum("_load_legacy" in ln for ln in lines) == 1


def test_camel_case_rename_changes_every_use_in_the_function(tmp_path: Path) -> None:
    mut, lines = apply(tmp_path, "camel_case_rename")
    assert mut.category is Category.CONVENTION_VIOLATION
    assert not any("row_count" in ln for ln in lines) and sum("rowCount" in ln for ln in lines) == 4
    assert len(mut.alternates) == 3


def test_wildcard_import_replaces_an_explicit_import(tmp_path: Path) -> None:
    mut, lines = apply(tmp_path, "wildcard_import")
    assert lines[mut.primary.line_start - 1] == "from pkg.util import *"


def test_debug_print_follows_an_assignment_in_a_function(tmp_path: Path) -> None:
    mut, lines = apply(tmp_path, "debug_print")
    printed = lines[mut.primary.line_start - 1]
    assert printed.strip().startswith('print(f"DEBUG ')
    assert lines[mut.primary.line_start - 2].strip().split(" = ")[0] in printed


def test_templates_decline_when_nothing_applies(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.src[0].added.clear()
    assert all(t(ctx) is None for t in TEMPLATES_V3.values())


def test_the_builder_refuses_to_rebuild_a_frozen_dataset() -> None:
    assert (DATASETS_DIR / "seeded-v1" / "manifest.json").exists()
    with pytest.raises(BuildError, match="frozen"):
        init_dataset("seeded-v1", 1)
    assert (DATASETS_DIR / "seeded-v1" / "items").is_dir()  # still intact


def test_unused_parameter_skips_dunder_methods(tmp_path: Path) -> None:
    src = "class C:\n    def __iter__(self):\n        return iter(())\n"
    ctx = make_ctx(tmp_path)
    ctx.src = [m.FileChange("pkg/mod.py", src, {1, 2, 3})]
    assert TEMPLATES_V3["unused_parameter"](ctx) is None
