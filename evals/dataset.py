"""Eval dataset loading, freezing, splitting, and materialization into git repos.

Layout:
  evals/datasets/<name>/manifest.json
  evals/datasets/<name>/items/<id>/item.json
  evals/datasets/<name>/items/<id>/base/...   (inline items: full base tree)
  evals/datasets/<name>/items/<id>/head/...   (inline items: full head tree)

A frozen dataset records a SHA-256 over every file. The harness refuses to run if the
content no longer matches, so a dataset can't be edited silently after freezing.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from watchman_review.schema import Category

DATASETS_DIR = Path(__file__).parent / "datasets"
TEST_FRACTION = 0.3


class Split(StrEnum):
    DEV = "dev"
    TEST = "test"


class ItemKind(StrEnum):
    SEEDED = "seeded"  # dataset A
    REVERTED = "reverted"  # dataset B
    CLEAN = "clean"  # dataset C


class LineRange(BaseModel):
    file: str
    line_start: int
    line_end: int


class Defect(BaseModel):
    file: str
    line_start: int
    line_end: int
    category: Category
    description: str = ""
    # Other places a reviewer could legitimately point at for the same defect (e.g. the
    # import *and* the call site of a hallucinated package). Any one of them counts.
    alt_ranges: list[LineRange] = Field(default_factory=list)

    def ranges(self) -> list[LineRange]:
        primary = LineRange(file=self.file, line_start=self.line_start, line_end=self.line_end)
        return [primary, *self.alt_ranges]


class InlineSource(BaseModel):
    type: Literal["inline"] = "inline"
    license: str
    origin: str


class GitSource(BaseModel):
    """Pinned upstream commit plus patches stored next to item.json.

    base = base_commit (+ base.patch if present); head = base + change.patch.
    """

    type: Literal["git"] = "git"
    repo: str
    url: str
    license: str
    base_commit: str
    upstream_commit: str
    origin: str


class Item(BaseModel):
    id: str
    kind: ItemKind
    task: str | None = None
    source: InlineSource | GitSource = Field(discriminator="type")
    defects: list[Defect] = Field(default_factory=list)
    meta: dict[str, str] = Field(default_factory=dict)


class Manifest(BaseModel):
    name: str
    version: int = 1
    split_seed: str = "watchman-v1"
    frozen: bool = False
    content_sha256: str | None = None
    splits: dict[str, Split] = Field(default_factory=dict)


class DatasetError(Exception):
    pass


def assign_split(item_id: str, seed: str) -> Split:
    h = int(hashlib.sha256(f"{seed}:{item_id}".encode()).hexdigest()[:8], 16)
    return Split.TEST if h / 0xFFFFFFFF < TEST_FRACTION else Split.DEV


def strata_key(item: Item) -> str:
    repo = item.source.repo if isinstance(item.source, GitSource) else "inline"
    return item.meta.get("template") or f"{item.kind.value}:{repo}"


def stratified_splits(items: list[Item], seed: str) -> dict[str, Split]:
    """Exactly round(TEST_FRACTION * n) items per stratum (template for seeded items, repo
    otherwise) go to test, chosen by a seeded hash. Each split then covers every template
    and repo, which a per-item coin flip doesn't guarantee on small datasets."""
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(strata_key(it), []).append(it)
    out: dict[str, Split] = {}
    for group in groups.values():
        ranked = sorted(
            group, key=lambda it: hashlib.sha256(f"{seed}:{it.id}".encode()).hexdigest()
        )
        n_test = round(TEST_FRACTION * len(ranked))
        for i, it in enumerate(ranked):
            out[it.id] = Split.TEST if i < n_test else Split.DEV
    return out


def content_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted((root / "items").rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


class Dataset:
    def __init__(self, root: Path) -> None:
        self.root = root
        mpath = root / "manifest.json"
        if not mpath.exists():
            raise DatasetError(f"no manifest at {mpath}")
        self.manifest = Manifest.model_validate_json(mpath.read_text())
        self._splits: dict[str, Split] | None = None

    @classmethod
    def load(cls, name: str, base: Path = DATASETS_DIR) -> Dataset:
        return cls(base / name)

    def verify(self) -> None:
        if not self.manifest.frozen:
            return
        actual = content_hash(self.root)
        if actual != self.manifest.content_sha256:
            raise DatasetError(
                f"dataset {self.manifest.name!r} is frozen but its content changed "
                f"(expected {self.manifest.content_sha256}, got {actual}). "
                "Frozen datasets may only change with owner approval."
            )

    def all_items(self) -> list[Item]:
        out = []
        for path in sorted((self.root / "items").glob("*/item.json")):
            item = Item.model_validate_json(path.read_text())
            if item.id != path.parent.name:
                raise DatasetError(f"item id {item.id!r} != directory {path.parent.name!r}")
            out.append(item)
        return out

    def items(self, split: Split | None = None) -> list[Item]:
        items = self.all_items()
        if split is None:
            return items
        return [it for it in items if self.split_of(it.id) is split]

    def split_of(self, item_id: str) -> Split:
        if item_id in self.manifest.splits:
            return self.manifest.splits[item_id]
        if self._splits is None:
            self._splits = stratified_splits(self.all_items(), self.manifest.split_seed)
        return self._splits.get(item_id) or assign_split(item_id, self.manifest.split_seed)

    def item_dir(self, item_id: str) -> Path:
        return self.root / "items" / item_id

    def freeze(self) -> Manifest:
        """Record splits and content hash. Only run with explicit owner approval."""
        m = self.manifest
        computed = stratified_splits(self.all_items(), m.split_seed)
        m.splits = {i.id: m.splits.get(i.id) or computed[i.id] for i in self.all_items()}
        m.frozen = True
        m.content_sha256 = content_hash(self.root)
        (self.root / "manifest.json").write_text(m.model_dump_json(indent=2) + "\n")
        return m


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=watchman-eval", "-c", "user.email=eval@watchman.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip


def materialize(ds: Dataset, item: Item, dest: Path) -> tuple[str, str]:
    """Create a git repo at dest with base and head commits. Returns (base, head) refs."""
    src = ds.item_dir(item.id)
    if dest.exists():
        shutil.rmtree(dest)
    if isinstance(item.source, GitSource):
        return _materialize_git(item, item.source, src, dest)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q", "-b", "base")
    shutil.copytree(src / "base", dest, dirs_exist_ok=True)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", "base")
    _git(dest, "checkout", "-q", "-b", "head")
    for p in dest.iterdir():
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(src / "head", dest, dirs_exist_ok=True)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", item.task or "change")
    return "base", "head"


def _materialize_git(item: Item, source: GitSource, src: Path, dest: Path) -> tuple[str, str]:
    from evals.build.repos import CACHE

    cache = CACHE / source.repo
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", source.url, str(cache)], check=True)
    # --shared borrows objects from the cache (fast); --no-tags plus deleting the default
    # branch keeps commits *after* the item out of reach, so the reviewer can't see the
    # future (e.g. the upstream fix for a reverted-fix item).
    subprocess.run(
        ["git", "clone", "-q", "--shared", "--no-tags", "--no-checkout", str(cache), str(dest)],
        check=True, capture_output=True,
    )  # fmt: skip
    _git(dest, "checkout", "-q", "-b", "base", source.base_commit)
    for ref in subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=dest, capture_output=True, text=True, check=True,
    ).stdout.split():  # fmt: skip
        if ref != "base":
            _git(dest, "branch", "-q", "-D", ref)
    _git(dest, "remote", "remove", "origin")
    base_patch = src / "base.patch"
    if base_patch.exists():
        _git(dest, "apply", "--whitespace=nowarn", str(base_patch))
        _git(dest, "add", "-A")
        _git(dest, "commit", "-q", "-m", "Snapshot")
    _git(dest, "checkout", "-q", "-b", "head")
    _git(dest, "apply", "--whitespace=nowarn", str(src / "change.patch"))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", item.task or "change")
    return "base", "head"


def dump_item_json(item: Item) -> str:
    return json.dumps(item.model_dump(mode="json"), indent=2)
