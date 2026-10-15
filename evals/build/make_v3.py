"""Build dataset v3: seeded readability and design defects (over_abstraction, convention_violation).

  uv run python -m evals.build.make_v3 --seed 20261015

Uses only upstream commits that v1 and v2 never used. Writes `seeded-v3` unfrozen, with the usual
stratified dev/test split applied at freeze time. It must be reviewed (REVIEW.md) and frozen with
`python -m evals.harness freeze --dataset seeded-v3 --owner-approved`; this module never freezes.
"""

from __future__ import annotations

import random
import subprocess
from typing import Annotated

import typer

from evals.build.make_datasets import (
    build_seeded,
    init_dataset,
    used_upstream_commits,
)
from evals.build.mutators_v3 import SCHEDULE_V3, TEMPLATES_V3
from evals.build.repos import REPOS, Commit, ensure_clone, is_clean_candidate, list_commits
from evals.dataset import DATASETS_DIR

app = typer.Typer(add_completion=False)


@app.command()
def main(seed: Annotated[int, typer.Option()] = 20261015) -> None:
    rng = random.Random(seed)
    skip = used_upstream_commits("v1") | used_upstream_commits("v2")
    typer.echo(f"excluding {len(skip)} upstream commits used by v1 and v2")
    pools: dict[str, list[Commit]] = {}
    for name, r in REPOS.items():
        ensure_clone(r)
        clean = [c for c in list_commits(r) if is_clean_candidate(c, r)]
        rng.shuffle(clean)
        pools[name] = [c for c in clean if c.sha not in skip]
    root = init_dataset("seeded-v3", seed)
    made = build_seeded(pools, set(), root, rng, SCHEDULE_V3, TEMPLATES_V3)
    typer.echo(f"seeded-v3: {sum(made.values())} items: {made}")
    short = {t: n - made.get(t, 0) for t, n in SCHEDULE_V3.items() if made.get(t, 0) < n}
    if short:
        typer.echo(f"  templates short of target: {short}")
    subprocess.run(["git", "worktree", "prune"], cwd=DATASETS_DIR, check=False)


if __name__ == "__main__":
    app()
