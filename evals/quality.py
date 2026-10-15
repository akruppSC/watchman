"""Objective, model-free code-quality metrics for the code a change touched.

These measure maintainability (modularity, analysability, testability) with plain,
reproducible numbers. They don't prove "good engineering"; they are
proxies that anyone can recompute:

  complexity   McCabe cyclomatic complexity per function (decisions + 1)
  length       lines per function
  params       positional/keyword parameters per function
  nesting      deepest block nesting inside a function
  docs         public functions and classes touched without a docstring
  duplication  added 5-line windows that already exist elsewhere in production code
  lint         ruff findings on added lines under a maintainability-oriented rule set

Only functions that contain an added line are measured, so pre-existing code is never
blamed on the change.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from watchman_review.checks.quality import (
    MAX_COMPLEXITY,
    MAX_LENGTH,
    MAX_NESTING,
    MAX_PARAMS,
    complexity,
    nesting,
)
from watchman_review.gitutil import parse_diff

# A fixed, maintainability-oriented rule set (separate from Watchman's pitfall rules so the
# measurement doesn't just echo what the reviewer enforces).
QUALITY_RULES = (
    *("E", "F", "W", "N", "UP", "SIM", "C4", "C901"),
    *("PLR0911", "PLR0912", "PLR0913", "PLR0915"),
)
WINDOW = 5


def is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name
    return "tests" in parts or "test" in parts or name.startswith("test_") or name == "conftest.py"


@dataclass
class FuncMetric:
    path: str
    name: str
    line: int
    complexity: int
    length: int
    params: int
    nesting: int
    public: bool
    documented: bool


def _functions(path: str, source: str, added: set[int]) -> tuple[list[FuncMetric], int, int]:
    """Functions containing an added line, plus (undocumented, total) public classes touched."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], 0, 0
    out: list[FuncMetric] = []
    undocumented_classes = 0
    public_classes = 0
    for node in ast.walk(tree):
        end: int = getattr(node, "end_lineno", None) or getattr(node, "lineno", 0) or 0
        span = set(range(getattr(node, "lineno", 0), end + 1))
        if isinstance(node, ast.ClassDef) and span & added and not node.name.startswith("_"):
            public_classes += 1
            undocumented_classes += ast.get_docstring(node) is None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and span & added:
            a = node.args
            n_params = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
            n_params += (a.vararg is not None) + (a.kwarg is not None)
            if node.args.args and node.args.args[0].arg in ("self", "cls"):
                n_params -= 1
            out.append(
                FuncMetric(
                    path=path,
                    name=node.name,
                    line=node.lineno,
                    complexity=complexity(node),
                    length=end - node.lineno + 1,
                    params=n_params,
                    nesting=nesting(node),
                    public=not node.name.startswith("_"),
                    documented=ast.get_docstring(node) is not None,
                )
            )
    return out, undocumented_classes, public_classes


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def duplicate_windows(
    tree: Path, added_by_file: dict[str, dict[int, str]], files: list[str]
) -> int:
    """Added WINDOW-line runs (production code only) that also appear elsewhere in production
    code: a cheap copy-paste signal, independent of any reviewer."""
    prod = [p for p in files if not is_test_path(p)]
    corpus: dict[str, list[tuple[str, int]]] = {}
    for p in sorted(tree.rglob("*.py")):
        rel = p.relative_to(tree).as_posix()
        if is_test_path(rel) or ".venv" in p.parts:
            continue
        lines = [_norm(x) for x in p.read_text(errors="replace").splitlines()]
        for i in range(len(lines) - WINDOW + 1):
            win = lines[i : i + WINDOW]
            if sum(len(x) > 3 for x in win) >= WINDOW - 1:  # skip blank/trivial windows
                corpus.setdefault("\n".join(win), []).append((rel, i + 1))
    dup = 0
    for rel in prod:
        added = added_by_file.get(rel, {})
        src = (tree / rel).read_text(errors="replace").splitlines()
        lines = [_norm(x) for x in src]
        for i in range(len(lines) - WINDOW + 1):
            if not all((i + k + 1) in added for k in range(WINDOW)):
                continue
            hits = corpus.get("\n".join(lines[i : i + WINDOW]), [])
            if any(h != (rel, i + 1) for h in hits):
                dup += 1
    return dup


def ruff_added(tree: Path, added_by_file: dict[str, set[int]]) -> list[str]:
    from ruff.__main__ import find_ruff_bin

    files = [f for f in added_by_file if (tree / f).exists()]
    if not files:
        return []
    proc = subprocess.run(
        [str(find_ruff_bin()), "check", "--isolated", "--no-cache", "--exit-zero",
         "--output-format=json", "--select", ",".join(QUALITY_RULES),
         "--line-length", "100", "--target-version=py312", *files],
        cwd=tree, capture_output=True, text=True, check=False,
    )  # fmt: skip
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for it in items:
        rel = Path(it["filename"]).resolve().relative_to(tree.resolve()).as_posix()
        if it["location"]["row"] in added_by_file.get(rel, set()):
            out.append(f"{it['code']}:{rel}:{it['location']['row']}")
    return out


@dataclass
class Quality:
    functions: int = 0
    max_complexity: int = 0
    mean_complexity: float = 0.0
    over_complexity: int = 0  # functions above MAX_COMPLEXITY (10)
    max_length: int = 0
    over_length: int = 0  # above MAX_LENGTH (60 lines)
    max_params: int = 0
    over_params: int = 0  # above MAX_PARAMS (5)
    over_nesting: int = 0  # above MAX_NESTING (4)
    breaches: int = 0  # functions over ANY limit above (primary outcome of experiment 2)
    max_nesting: int = 0
    public_undocumented: int = 0
    public_defs: int = 0  # public functions and classes the change touched
    duplicated_windows: int = 0
    lint: int = 0
    prod_lines_added: int = 0
    test_lines_added: int = 0
    tests_added: int = 0
    detail: list[FuncMetric] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["detail"] = [asdict(f) for f in self.detail]
        return d


def measure(tree: Path, diff_text: str) -> Quality:
    """Measure the code a unified diff (against a base) added to the repo at `tree`."""
    diff = parse_diff(diff_text)
    q = Quality()
    added_text = {f.path: f.added for f in diff.files if f.is_python}
    added_lines = {p: set(a) for p, a in added_text.items()}
    undocumented_classes = 0
    public_classes = 0
    for f in diff.files:
        if not f.is_python or not (tree / f.path).exists():
            continue
        if is_test_path(f.path):
            q.test_lines_added += len(f.added)
            q.tests_added += sum(1 for t in f.added.values() if re.match(r"\s*def test_", t))
            continue
        q.prod_lines_added += len(f.added)
        funcs, undoc, n_classes = _functions(
            f.path, (tree / f.path).read_text(errors="replace"), set(f.added)
        )
        q.detail += funcs
        undocumented_classes += undoc
        public_classes += n_classes
    d = q.detail
    q.functions = len(d)
    if d:
        q.max_complexity = max(x.complexity for x in d)
        q.mean_complexity = round(sum(x.complexity for x in d) / len(d), 2)
        q.over_complexity = sum(x.complexity > MAX_COMPLEXITY for x in d)
        q.max_length = max(x.length for x in d)
        q.over_length = sum(x.length > MAX_LENGTH for x in d)
        q.max_params = max(x.params for x in d)
        q.over_params = sum(x.params > MAX_PARAMS for x in d)
        q.over_nesting = sum(x.nesting > MAX_NESTING for x in d)
        q.breaches = sum(
            x.complexity > MAX_COMPLEXITY
            or x.length > MAX_LENGTH
            or x.params > MAX_PARAMS
            or x.nesting > MAX_NESTING
            for x in d
        )
        q.max_nesting = max(x.nesting for x in d)
    q.public_undocumented = sum(x.public and not x.documented for x in d) + undocumented_classes
    q.public_defs = sum(x.public for x in d) + public_classes
    q.duplicated_windows = duplicate_windows(tree, added_text, list(added_text))
    q.lint = len(ruff_added(tree, added_lines))
    return q
