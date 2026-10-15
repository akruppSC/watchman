from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from evals.quality import measure

from watchman_review.checks.quality import complexity, nesting

BRANCHY = """
def route(cmd, a, b):
    if cmd == "x" and a:
        return 1
    elif cmd == "y" or b:
        for i in range(3):
            if i:
                return i
    try:
        pass
    except ValueError:
        return [z for z in a if z]
    return 0
"""


def diff_for(tmp: Path, rel: str, body: str) -> str:
    """A diff that adds `body` as a new file `rel` (against an empty tree)."""
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    out = subprocess.run(
        ["git", "diff", "--no-index", "--", "/dev/null", str(p)],
        capture_output=True, text=True, check=False,
    ).stdout  # fmt: skip
    return out.replace(f"a{p}", f"a/{rel}").replace(f"b{p}", f"b/{rel}")


def test_complexity_counts_decisions() -> None:
    fn = ast.parse(BRANCHY).body[0]
    # 1 + if + (and) + elif + (or) + for + if + except + comprehension(1 + 1 if)
    assert complexity(fn) == 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 2


def test_nesting_depth() -> None:
    fn = ast.parse(BRANCHY).body[0]
    assert nesting(fn) == 3  # elif -> for -> if


def test_measure_reports_only_touched_functions_and_docs(tmp_path: Path) -> None:
    body = (
        'def small(a):\n    """Doc."""\n    return a\n\n\n'
        "def big(a, b, c, d, e, f):\n    if a:\n        return b\n    return c\n"
    )
    d = diff_for(tmp_path, "app/m.py", body)
    q = measure(tmp_path, d)
    assert q.functions == 2
    assert q.max_params == 6 and q.over_params == 1 and q.breaches == 1
    assert q.public_undocumented == 1  # `big`
    assert q.prod_lines_added == len(body.splitlines())


def test_tests_are_counted_separately(tmp_path: Path) -> None:
    d = diff_for(
        tmp_path,
        "tests/test_a.py",
        "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n",
    )
    q = measure(tmp_path, d)
    assert q.tests_added == 2 and q.functions == 0 and q.prod_lines_added == 0


def test_duplicate_windows_detect_copy_paste(tmp_path: Path) -> None:
    block = "\n".join(f"    value_{i} = compute_something({i}) + {i}" for i in range(6))
    (tmp_path / "app").mkdir()
    (tmp_path / "app/old.py").write_text(f"def old():\n{block}\n    return 1\n")
    d = diff_for(tmp_path, "app/new.py", f"def new():\n{block}\n    return 2\n")
    assert measure(tmp_path, d).duplicated_windows >= 1
