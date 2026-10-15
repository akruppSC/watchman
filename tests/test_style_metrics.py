from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from evals.live.run import watchman_config
from evals.style_metrics import measure_style

from watchman_review.config import StyleGuide, load_config


def add_file_diff(tmp: Path, rel: str, body: str) -> str:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    out = subprocess.run(
        ["git", "diff", "--no-index", "--", "/dev/null", str(p)],
        capture_output=True, text=True, check=False,
    ).stdout  # fmt: skip
    return out.replace(f"a{p}", f"a/{rel}").replace(f"b{p}", f"b/{rel}")


def test_scores_a_sloppy_module_worse_than_a_tidy_one(tmp_path: Path) -> None:
    sloppy = (
        "def doStuff(x):\n    return x\n\n\nclass thing:\n    def Run(self):\n        return 1\n"
    )
    tidy = '"""Tidy."""\n\n\ndef do_stuff(x: int) -> int:\n    """Returns x."""\n    return x\n'
    bad = measure_style(tmp_path, add_file_diff(tmp_path, "app/a.py", sloppy))
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good = measure_style(good_dir, add_file_diff(good_dir, "app/b.py", tidy))
    assert bad["pylint"] > good["pylint"] == 0
    assert (bad["documented"], bad["public_defs"]) == (0, 3)
    assert (good["documented"], good["annotated"], good["public_funcs"]) == (1, 1, 1)
    assert "invalid-name" in bad["pylint_by_symbol"]


def test_the_supervised_arm_config_carries_the_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WATCHMAN_CONFIG", str(watchman_config(tmp_path, "google")))
    assert load_config(None)[0].review.style is StyleGuide.GOOGLE
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("WATCHMAN_CONFIG", str(watchman_config(other, "none")))
    assert load_config(None)[0].review.style is StyleGuide.NONE
