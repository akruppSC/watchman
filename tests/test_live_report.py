from __future__ import annotations

from typing import Any

import pytest
from evals.live.report import merge, pairs, render, sign_test, verdicts


def run(task: str, arm: str, trial: int, *, correct=True, clean=True, strong=True, **kw: Any):
    rec: dict[str, Any] = {
        "task": task, "arm": arm, "trial": trial, "worker": "w", "worker_cost": 0.01,
        "seconds": 10.0, "timed_out": False, "done_feedback": None,
        "watchman": (
            {"cost_usd": 0.05, "reviews": 3, "findings_shown": 1} if arm == "watchman" else {}
        ),
        "evaluation": {
            "correct": correct, "clean": clean, "strong_tests": strong,
            "probe_hits": {} if clean else {"swallowed:app/x.py": ["hit"]},
        },
    }  # fmt: skip
    rec.update(kw)
    return rec


def test_sign_test_matches_known_values() -> None:
    assert sign_test(0, 0) == 1.0
    assert sign_test(5, 0) == pytest.approx(0.0625)
    assert sign_test(3, 3) == 1.0
    assert sign_test(9, 1) == pytest.approx(0.021484375)


def test_verdicts_treat_no_mutant_target_as_strong() -> None:
    assert verdicts(run("t", "alone", 1, strong=None))["good"] is True
    assert verdicts(run("t", "alone", 1, strong=False))["good"] is False
    assert verdicts(run("t", "alone", 1, clean=False))["good"] is False


def test_pairs_match_the_same_task_and_trial_across_arms() -> None:
    runs = [
        run("a", "alone", 1),
        run("a", "watchman", 1),
        run("a", "alone", 2),
        run("b", "watchman", 1),
    ]
    runs.append({"task": "a", "arm": "watchman", "trial": 2, "error": "boom"})
    ps = pairs(runs)
    assert [(a["task"], a["trial"]) for a, _ in ps] == [("a", 1)]  # errored/unmatched runs excluded


def test_render_reports_counts_pairs_and_watchman_activity() -> None:
    runs = [
        run("a", "alone", 1, strong=False), run("a", "watchman", 1),
        run("a", "alone", 2), run("a", "watchman", 2),
        run("b", "alone", 1, clean=False), run("b", "watchman", 1, clean=False),
    ]  # fmt: skip
    text = render({"worker": "w", "spend": {"worker": 0.1, "reviewer": 0.2}, "runs": runs})
    assert "6 completed runs of 6 started" in text
    assert "Only the supervised run good: 1" in text and "Only the unsupervised run good: 0" in text
    assert "| a | 1/2 | 2/2 |" in text
    assert "swallowed x2" in text
    assert "Runs where Watchman showed the worker at least one finding: 3 of 3" in text


def test_merge_lets_later_runs_override_but_not_skipped_ones() -> None:
    first = {"worker": "w", "runs": [run("a", "alone", 1, strong=False), run("a", "watchman", 1)]}
    second = {
        "worker": "w",
        "runs": [
            run("a", "alone", 1),
            {"task": "a", "arm": "watchman", "trial": 1, "skipped": True},
        ],
    }
    m = merge([first, second])
    by_arm = {r["arm"]: r for r in m["runs"]}
    assert len(m["runs"]) == 2
    assert verdicts(by_arm["alone"])["strong"] is True  # replaced by the later run
    assert is_done_(by_arm["watchman"])  # the skipped record did not clobber it
    assert m["spend"]["reviewer"] == pytest.approx(0.05)


def is_done_(rec: dict[str, Any]) -> bool:
    return "evaluation" in rec


def test_post_hoc_tasks_are_marked() -> None:
    runs = [
        run("a", "alone", 1),
        run("a", "watchman", 1),
        run("p", "alone", 1),
        run("p", "watchman", 1),
    ]
    text = render({"worker": "w", "spend": {"worker": 0, "reviewer": 0}, "runs": runs}, {"p"})
    assert "| p † |" in text and "| a |" in text and "post hoc" in text
