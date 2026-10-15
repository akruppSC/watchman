from __future__ import annotations

from typing import Any

from evals.live.style_report import render


def run(arm: str, trial: int, pylint: int, documented: int) -> dict[str, Any]:
    return {
        "task": "t", "arm": arm, "trial": trial, "worker_cost": 0.01,
        "watchman": {"cost_usd": 0.05} if arm == "watchman" else {},
        "evaluation": {"correct": True},
        "quality": {"breaches": 0, "tests_added": 5},
        "style_metrics": {
            "pylint": pylint, "pylint_by_symbol": {"invalid-name": pylint},
            "public_defs": 4, "documented": documented, "public_funcs": 3, "annotated": 1,
            "long_functions": 0, "prod_lines_added": 100,
        },
    }  # fmt: skip


def test_render_pairs_runs_and_counts_lower_and_higher() -> None:
    runs = [run("alone", 1, 10, 1), run("watchman", 1, 4, 4), run("alone", 2, 6, 2),
            run("watchman", 2, 6, 4)]  # fmt: skip
    text = render({"worker": "w", "style": "google", "spend": {"worker": 0.1, "reviewer": 0.2},
                   "runs": runs})  # fmt: skip
    assert "4 completed runs (2 paired)" in text and "style = google" in text
    assert "| pylint messages, Google rc (primary) | 8.00 | 5.00 | 1 / 0 (p = 1.00) |" in text
    assert "| public API with a docstring (share) | 0.38 | 1.00 | 0 / 2 (p = 0.50) |" in text
    assert "invalid-name | 16 | 10" in text
