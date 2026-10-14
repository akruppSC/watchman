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


class Defect(BaseModel):
    file: str
    line_start: int
    line_end: int
    category: Category
    description: str = ""


class InlineSource(BaseModel):
    type: Literal["inline"] = "inline"
    license: str
    origin: str


class Item(BaseModel):
    id: str
    kind: ItemKind
    task: str | None = None
    source: InlineSource
    defects: list[Defect] = Field(default_factory=list)


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

    def items(self, split: Split | None = None) -> list[Item]:
        out = []
        for path in sorted((self.root / "items").glob("*/item.json")):
            item = Item.model_validate_json(path.read_text())
            if item.id != path.parent.name:
                raise DatasetError(f"item id {item.id!r} != directory {path.parent.name!r}")
            if split is None or self.split_of(item.id) is split:
                out.append(item)
        return out

    def split_of(self, item_id: str) -> Split:
        return self.manifest.splits.get(item_id) or assign_split(item_id, self.manifest.split_seed)

    def item_dir(self, item_id: str) -> Path:
        return self.root / "items" / item_id

    def freeze(self) -> Manifest:
        """Record splits and content hash. Only run with explicit owner approval."""
        m = self.manifest
        m.splits = {i.id: assign_split(i.id, m.split_seed) for i in self.items()}
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


def dump_item_json(item: Item) -> str:
    return json.dumps(item.model_dump(mode="json"), indent=2)
