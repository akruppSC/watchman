"""Style-conformance metrics for what a worker wrote, graded mostly by an independent tool.

  pylint     Google's own published pylintrc (evals/live/google_pylintrc, Apache-2.0), run over
             the production files the worker added or changed; only messages on added lines count.
             Written by the guide's authors and unrelated to Watchman's check.
  docs       share of public functions and classes touched that have a docstring (§3.8.3)
  annotated  share of public functions touched with every parameter and the return annotated (§2.21)
  long       functions over 40 lines (§3.18's advisory figure)

pylint is the independent measure; docs, annotated and long overlap with what the
`google_style` and `quality` checks enforce, so they show that the worker responds to feedback.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from watchman_review.checks.scope import is_test_path
from watchman_review.gitutil import parse_diff

RC = Path(__file__).resolve().parent / "live" / "google_pylintrc"
LONG = 40


def pylint_on_added(tree: Path, added: dict[str, set[int]]) -> list[dict[str, Any]]:
    files = [f for f in added if (tree / f).is_file()]
    if not files:
        return []
    proc = subprocess.run(
        [sys.executable, "-m", "pylint", f"--rcfile={RC}", "--output-format=json",
         "--score=n", "--jobs=1", *files],
        cwd=tree, capture_output=True, text=True, check=False, timeout=300,
    )  # fmt: skip
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [m for m in items if m.get("line") in added.get(m.get("path", ""), set())]


def _public_defs(
    source: str, added: set[int]
) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []

    def visit(node: ast.AST, public_parent: bool, in_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                public = public_parent and not child.name.startswith("_")
                end = child.end_lineno or child.lineno
                if public and not in_func and set(range(child.lineno, end + 1)) & added:
                    out.append(child)
                visit(child, public, in_func or not isinstance(child, ast.ClassDef))
            else:
                visit(child, public_parent, in_func)

    visit(tree, True, False)
    return out


def _fully_annotated(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    a = fn.args
    params = [*a.posonlyargs, *a.args, *a.kwonlyargs]
    if params and params[0].arg in ("self", "cls"):
        params = params[1:]
    extra = [x for x in (a.vararg, a.kwarg) if x is not None]
    ok = all(p.annotation is not None for p in [*params, *extra])
    return ok and (fn.returns is not None or fn.name == "__init__")


def measure_style(tree: Path, diff_text: str) -> dict[str, Any]:
    diff = parse_diff(diff_text)
    prod = {
        f.path: set(f.added)
        for f in diff.files
        if f.is_python and not f.path.endswith(".pyi") and not is_test_path(f.path)
        and (tree / f.path).is_file()
    }  # fmt: skip
    messages = pylint_on_added(tree, prod)
    defs = [
        d for path, lines in prod.items()
        for d in _public_defs((tree / path).read_text(errors="replace"), lines)
    ]  # fmt: skip
    funcs = [d for d in defs if not isinstance(d, ast.ClassDef)]
    documented = sum(ast.get_docstring(d) is not None for d in defs)
    annotated = sum(_fully_annotated(f) for f in funcs)
    long_fns = 0
    for path, lines in prod.items():
        for d in ast.walk(ast.parse((tree / path).read_text(errors="replace"))):
            if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = d.end_lineno or d.lineno
                if end - d.lineno + 1 > LONG and set(range(d.lineno, end + 1)) & lines:
                    long_fns += 1
    by_symbol: dict[str, int] = {}
    for m in messages:
        by_symbol[m["symbol"]] = by_symbol.get(m["symbol"], 0) + 1
    return {
        "pylint": len(messages),
        "pylint_by_symbol": by_symbol,
        "public_defs": len(defs),
        "documented": documented,
        "public_funcs": len(funcs),
        "annotated": annotated,
        "long_functions": long_fns,
        "prod_lines_added": sum(len(v) for v in prod.values()),
    }
