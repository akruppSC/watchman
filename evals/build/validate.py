"""Validate datasets: every item materializes, parses, and hides the future.

uv run python -m evals.build.validate seeded-v1 reverted-v1 clean-v1
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

from evals.dataset import Dataset, GitSource, ItemKind, materialize
from watchman_review.gitutil import get_diff


def validate(name: str) -> list[str]:
    ds = Dataset.load(name)
    problems: list[str] = []
    for it in ds.items():
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            try:
                base, head = materialize(ds, it, repo)
            except subprocess.CalledProcessError as e:
                problems.append(f"{it.id}: materialize failed: {(e.stderr or b'')[-200:]!r}")
                continue
            diff = get_diff(repo, base, head)
            if not diff.files:
                problems.append(f"{it.id}: empty diff")
            refs = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)"], cwd=repo, capture_output=True,
                text=True, check=True,
            ).stdout.split()  # fmt: skip
            if set(refs) != {"refs/heads/base", "refs/heads/head"}:
                problems.append(f"{it.id}: unexpected refs {refs}")
            if isinstance(it.source, GitSource) and it.kind is ItemKind.REVERTED:
                log = subprocess.run(
                    ["git", "log", "--format=%H", "head"], cwd=repo, capture_output=True,
                    text=True, check=True,
                ).stdout.split()  # fmt: skip
                if it.source.upstream_commit in log:
                    problems.append(f"{it.id}: upstream fix reachable from head")
            for f in diff.files:
                p = repo / f.path
                if f.path.endswith(".py") and p.exists():
                    try:
                        ast.parse(p.read_text())
                    except SyntaxError as e:
                        problems.append(f"{it.id}: syntax error {f.path}:{e.lineno}")
            for d in it.defects:
                if not any(
                    (
                        (fd := diff.file(r.file)) is not None
                        and any(r.line_start <= n <= r.line_end for n in fd.added)
                    )
                    or (fd is not None and not fd.added)
                    for r in d.ranges()
                ):
                    problems.append(f"{it.id}: defect not on an added line")
    return problems


def main() -> None:
    names = sys.argv[1:] or ["seeded-v1", "reverted-v1", "clean-v1"]
    bad = 0
    for name in names:
        problems = validate(name)
        bad += len(problems)
        print(f"{name}: {len(Dataset.load(name).items())} items, {len(problems)} problem(s)")
        for p in problems:
            print("  ", p)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
