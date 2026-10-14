"""Build the OSS eval datasets A (seeded), B (reverted fixes), C (clean).

  uv run python -m evals.build.make_datasets --seed 20261014

Deterministic for a given seed and the pinned upstream histories. The output is written
unfrozen; it must be reviewed and then frozen with
`python -m evals.harness freeze --dataset <name> --owner-approved`.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from evals.build.mutators import SCHEDULE, TEMPLATES, Ctx, FileChange, Mutation
from evals.build.repos import (
    REPOS,
    Commit,
    FileStat,
    SourceRepo,
    ensure_clone,
    git,
    is_clean_candidate,
    is_fix_candidate,
    list_commits,
)
from evals.dataset import (
    DATASETS_DIR,
    Defect,
    GitSource,
    Item,
    ItemKind,
    LineRange,
    Manifest,
)
from watchman_review.gitutil import parse_diff
from watchman_review.pypi import Lookup, PackageIndex
from watchman_review.schema import Category

app = typer.Typer(add_completion=False)

N_CLEAN_PER_REPO = 15
N_FIX_TOTAL = 30
DIFF_ARGS = ["--no-color", "--no-ext-diff", "--no-renames", "--full-index", "--binary"]


class BuildError(Exception):
    pass


@contextmanager
def worktree(r: SourceRepo, sha: str) -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="wm-build-"))
    path = tmp / "wt"
    git(r.path, "worktree", "add", "-q", "--detach", str(path), sha)
    try:
        yield path
    finally:
        git(r.path, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def added_lines(r: SourceRepo, parent: str, sha: str, path: str) -> set[int]:
    d = parse_diff(git(r.path, "diff", "--unified=0", parent, sha, "--", path))
    f = d.file(path)
    return set(f.added) if f else set()


def package_files(tree: Path, r: SourceRepo) -> list[str]:
    out = []
    for prefix in r.src_prefixes:
        base = tree / prefix
        if base.is_dir():
            out += [p.relative_to(tree).as_posix() for p in sorted(base.rglob("*.py"))]
    return out


def write_item(root: Path, item: Item, patches: dict[str, str]) -> None:
    d = root / "items" / item.id
    d.mkdir(parents=True, exist_ok=True)
    for name, text in patches.items():
        (d / name).write_text(text)
    (d / "item.json").write_text(json.dumps(item.model_dump(mode="json"), indent=2) + "\n")


def source(r: SourceRepo, base: str, upstream: str, origin: str) -> GitSource:
    return GitSource(
        repo=r.name, url=r.url, license=r.license, base_commit=base, upstream_commit=upstream,
        origin=origin,
    )  # fmt: skip


def task_text(c: Commit) -> str:
    subject = re.sub(r"\s*\(#\d+\)\s*$", "", c.subject).strip()
    return subject


def verify_locations(patch: str, defects: list[Defect]) -> None:
    """Every defect range must touch lines added by the reviewed diff."""
    d = parse_diff(patch)
    for defect in defects:
        ok = False
        for rng in defect.ranges():
            f = d.file(rng.file)
            if f and any(rng.line_start <= n <= rng.line_end for n in f.added):
                ok = True
        if not ok:
            raise BuildError(f"defect {defect.description!r} not on added lines of the diff")


# --- dataset C: clean ---------------------------------------------------------


def build_clean(pools: dict[str, list[Commit]], out: Path) -> set[str]:
    used: set[str] = set()
    for name, pool in pools.items():
        r = REPOS[name]
        for c in pool[:N_CLEAN_PER_REPO]:
            patch = git(r.path, "diff", *DIFF_ARGS, c.parent, c.sha)
            item = Item(
                id=f"{name}-clean-{c.sha[:8]}", kind=ItemKind.CLEAN, task=task_text(c),
                source=source(r, c.parent, c.sha, "upstream commit, unmodified"),
                meta={"upstream_subject": c.subject, "date": c.date},
            )  # fmt: skip
            write_item(out, item, {"change.patch": patch})
            used.add(c.sha)
    return used


# --- dataset B: reverted fixes ------------------------------------------------


_DEF_RE = re.compile(r"(?:def|class)\s+(\w+)")


def build_reverted(fixes: dict[str, list[Commit]], out: Path, rng: random.Random) -> int:
    per_repo = {n: N_FIX_TOTAL // len(fixes) for n in fixes}
    for n in list(fixes)[: N_FIX_TOTAL % len(fixes)]:
        per_repo[n] += 1
    count = 0
    for name, pool in fixes.items():
        r = REPOS[name]
        taken = 0
        for c in pool:
            if taken >= per_repo[name]:
                break
            src = [f.path for f in c.src_files(r)]
            base_patch = git(r.path, "diff", *DIFF_ARGS, c.parent, c.sha, "--", *src)
            change = git(r.path, "diff", *DIFF_ARGS, c.sha, c.parent, "--", *src)
            d = parse_diff(change)
            defects: list[Defect] = []
            names: list[str] = []
            for f in d.files:
                for h in f.hunks:
                    m = _DEF_RE.search(h.header)
                    if m:
                        names.append(m.group(1))
                rows = sorted(f.added) or [h.new_start for h in f.hunks]
                if not rows:
                    continue
                spans = [(rows[0], rows[-1])] if rows[-1] - rows[0] <= 40 else [
                    (h.new_start, h.new_start + max(h.new_len, 1) - 1) for h in f.hunks
                ]  # fmt: skip
                first, *rest = spans
                defects.append(
                    Defect(
                        file=f.path, line_start=first[0], line_end=first[1],
                        category=Category.CORRECTNESS,
                        description=f"Reintroduces the bug fixed upstream by {c.sha[:8]}: "
                        f"{c.subject}",
                        alt_ranges=[LineRange(file=f.path, line_start=a, line_end=b)
                                    for a, b in rest],
                    )
                )  # fmt: skip
            if not defects:
                continue
            uniq = list(dict.fromkeys(names))[:3]
            target = ", ".join(f"`{n}`" for n in uniq) or ", ".join(Path(p).name for p in src)
            item = Item(
                id=f"{name}-revert-{c.sha[:8]}", kind=ItemKind.REVERTED,
                task=f"Simplify the implementation of {target}.",
                source=source(r, c.parent, c.sha, "upstream bug fix, reverted (source only)"),
                defects=defects, meta={"upstream_subject": c.subject, "date": c.date},
            )  # fmt: skip
            write_item(out, item, {"base.patch": base_patch, "change.patch": change})
            taken += 1
            count += 1
    return count


# --- dataset A: seeded ----------------------------------------------------------


def _file_changes(r: SourceRepo, c: Commit, tree: Path, files: list[FileStat]) -> list[FileChange]:
    return [
        FileChange(f.path, (tree / f.path).read_text(), added_lines(r, c.parent, c.sha, f.path))
        for f in files
        if (tree / f.path).exists()
    ]


def try_seed(
    r: SourceRepo, c: Commit, template: str, rng: random.Random
) -> tuple[Mutation, str] | None:
    with worktree(r, c.sha) as tree:
        ctx = Ctx(
            tree=tree,
            src=_file_changes(r, c, tree, c.src_files(r)),
            tests=_file_changes(r, c, tree, c.test_files(r)),
            package_files=package_files(tree, r),
            rng=rng,
        )  # fmt: skip
        mutation = TEMPLATES[template](ctx)
        if mutation is None:
            return None
        for path, text in mutation.edits.items():
            (tree / path).write_text(text)
        git(tree, "add", "-A")
        patch = git(tree, "diff", "--cached", *DIFF_ARGS, c.parent)
    return mutation, patch


def build_seeded(
    pools: dict[str, list[Commit]], skip: set[str], out: Path, rng: random.Random
) -> dict[str, int]:
    schedule = [t for t, n in SCHEDULE.items() for _ in range(n)]
    rng.shuffle(schedule)
    names = list(pools)
    used = set(skip)
    made: dict[str, int] = {}
    for i, template in enumerate(schedule):
        for k in range(len(names)):  # round-robin repos, fall through if none applies
            name = names[(i + k) % len(names)]
            r = REPOS[name]
            hit = None
            for c in [c for c in pools[name] if c.sha not in used][:60]:
                res = try_seed(r, c, template, rng)
                if res is not None:
                    hit = (c, *res)
                    break
            if hit is None:
                continue
            c, m, patch = hit
            defect = Defect(
                file=m.primary.file, line_start=m.primary.line_start,
                line_end=m.primary.line_end, category=m.category, description=m.description,
                alt_ranges=[LineRange(file=a.file, line_start=a.line_start, line_end=a.line_end)
                            for a in m.alternates],
            )  # fmt: skip
            verify_locations(patch, [defect])
            item = Item(
                id=f"{name}-{template.replace('_', '-')}-{c.sha[:8]}", kind=ItemKind.SEEDED,
                task=task_text(c),
                source=source(r, c.parent, c.sha, f"upstream commit + seeded defect ({template})"),
                defects=[defect],
                meta={"template": template, "upstream_subject": c.subject, "date": c.date},
            )  # fmt: skip
            write_item(out, item, {"change.patch": patch})
            used.add(c.sha)
            made[template] = made.get(template, 0) + 1
            break
    return made


def init_dataset(name: str, seed: int) -> Path:
    root = DATASETS_DIR / name
    if root.exists():
        shutil.rmtree(root)
    (root / "items").mkdir(parents=True)
    m = Manifest(name=name, version=1, split_seed=f"{name}-{seed}")
    (root / "manifest.json").write_text(m.model_dump_json(indent=2) + "\n")
    return root


def check_fake_packages() -> None:
    from evals.build.mutators import FAKE_DECORATORS

    index = PackageIndex()
    for module, _ in FAKE_DECORATORS:
        for cand in (module, module.replace("_", "-")):
            if index.lookup(cand).status is not Lookup.MISSING:
                raise BuildError(f"fake package {cand!r} exists (or PyPI unreachable)")


@app.command()
def main(seed: Annotated[int, typer.Option()] = 20261014) -> None:
    rng = random.Random(seed)
    check_fake_packages()
    clean_pools: dict[str, list[Commit]] = {}
    fix_pools: dict[str, list[Commit]] = {}
    for name, r in REPOS.items():
        ensure_clone(r)
        commits = list_commits(r)
        clean = [c for c in commits if is_clean_candidate(c, r)]
        fixes = [c for c in commits if is_fix_candidate(c, r)]
        rng.shuffle(clean)
        rng.shuffle(fixes)
        clean_pools[name], fix_pools[name] = clean, fixes

    c_root = init_dataset("clean-v1", seed)
    used = build_clean(clean_pools, c_root)
    typer.echo(f"clean-v1: {len(used)} items")

    b_root = init_dataset("reverted-v1", seed)
    typer.echo(f"reverted-v1: {build_reverted(fix_pools, b_root, rng)} items")

    a_root = init_dataset("seeded-v1", seed)
    made = build_seeded(clean_pools, used, a_root, rng)
    typer.echo(f"seeded-v1: {sum(made.values())} items: {made}")
    missing = {t: n - made.get(t, 0) for t, n in SCHEDULE.items() if made.get(t, 0) < n}
    if missing:
        typer.echo(f"  templates short of target: {missing}")
    subprocess.run(["git", "worktree", "prune"], cwd=DATASETS_DIR, check=False)


if __name__ == "__main__":
    app()
