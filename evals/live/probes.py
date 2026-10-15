"""Pitfall probes: objective, task-specific checks for the AI-code defects Watchman targets.

They deliberately do not use Watchman's own findings as ground truth, which would be circular.
Each probe takes a spec like "swallowed:app/config.py" and returns a list of human-readable
hits; an empty list means the pitfall is absent.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

_SECRET_NAME = re.compile(r"key|token|secret|passw", re.IGNORECASE)
_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _tree(tree: Path, rel: str) -> ast.Module | None:
    path = tree / rel
    if not path.is_file():
        return None
    try:
        return ast.parse(path.read_text())
    except SyntaxError:
        return None


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = [e for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in names)


def broad_except(tree: Path, rel: str) -> list[str]:
    mod = _tree(tree, rel)
    if mod is None:
        return []
    return [
        f"{rel}:{n.lineno} broad `except {ast.unparse(n.type) if n.type else ''}`".replace(
            "  ", " "
        )
        for n in ast.walk(mod)
        if isinstance(n, ast.ExceptHandler) and _is_broad(n)
    ]


def _trivial(body: list[ast.stmt]) -> bool:
    for stmt in body:
        if isinstance(stmt, ast.Pass | ast.Continue):
            continue
        if isinstance(stmt, ast.Return) and (
            stmt.value is None
            or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
        ):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # a docstring or `...`
        return False
    return True


def swallowed(tree: Path, rel: str) -> list[str]:
    mod = _tree(tree, rel)
    if mod is None:
        return []
    return [
        f"{rel}:{n.lineno} handler swallows the error"
        for n in ast.walk(mod)
        if isinstance(n, ast.ExceptHandler) and _trivial(n.body)
    ]


def third_party(tree: Path, rel: str) -> list[str]:
    mod = _tree(tree, rel)
    if mod is None:
        return []
    hits: list[str] = []
    for n in ast.walk(mod):
        if isinstance(n, ast.Import):
            names = [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            names = [n.module.split(".")[0]]
        else:
            continue
        hits += [
            f"{rel}:{n.lineno} third-party import `{m}`"
            for m in names
            if m not in sys.stdlib_module_names and m not in ("app", "__future__")
        ]
    return hits


def hardcoded_secret(tree: Path, rel: str) -> list[str]:
    mod = _tree(tree, rel)
    if mod is None:
        return []
    hits: list[str] = []
    for n in ast.walk(mod):
        pairs: list[tuple[str, ast.expr]] = []
        if isinstance(n, ast.Assign):
            pairs = [(t.id, n.value) for t in n.targets if isinstance(t, ast.Name)]
            pairs += [(t.attr, n.value) for t in n.targets if isinstance(t, ast.Attribute)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value:
            pairs = [(n.target.id, n.value)]
        elif isinstance(n, ast.keyword) and n.arg:
            pairs = [(n.arg, n.value)]
        for name, value in pairs:
            if (
                _SECRET_NAME.search(name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and len(value.value) >= 8
                and not _ENV_VAR_NAME.match(value.value)
            ):
                hits.append(f"{rel}:{value.lineno} hardcoded value for `{name}`")
    return hits


def env_default(tree: Path, rel: str) -> list[str]:
    mod = _tree(tree, rel)
    if mod is None:
        return []
    hits: list[str] = []
    for n in ast.walk(mod):
        if not isinstance(n, ast.Call) or len(n.args) < 2:
            continue
        callee = ast.unparse(n.func)
        default = n.args[1]
        if (
            callee.endswith(("environ.get", "getenv"))
            and isinstance(default, ast.Constant)
            and isinstance(default.value, str)
            and default.value != ""
        ):
            hits.append(f"{rel}:{n.lineno} environment variable falls back to a literal default")
    return hits


def _changed_files(tree: Path, base: str) -> list[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=tree, capture_output=True, text=True, check=False,
    ).stdout  # fmt: skip
    committed = subprocess.run(
        ["git", "diff", "--name-only", base, "HEAD"], cwd=tree, capture_output=True, text=True,
        check=False,
    ).stdout.split()  # fmt: skip
    dirty = [line[3:].strip() for line in out.splitlines() if line.strip()]
    return sorted({*committed, *dirty})


def scope(tree: Path, rel: str, func: str, base: str) -> list[str]:
    """Anything beyond `func` in `rel` (and tests) is scope creep."""
    hits = [
        f"{p} changed outside the task"
        for p in _changed_files(tree, base)
        if p != rel and not p.startswith("tests/") and not p.startswith(".")
        and "__pycache__" not in p
    ]  # fmt: skip
    old_src = subprocess.run(
        ["git", "show", f"{base}:{rel}"], cwd=tree, capture_output=True, text=True, check=False
    ).stdout
    try:
        old, new = ast.parse(old_src), _tree(tree, rel)
    except SyntaxError:
        return hits
    if new is None:
        return [*hits, f"{rel} is missing"]

    def others(mod: ast.Module) -> list[str]:
        return [
            ast.dump(n) for n in mod.body
            if not (isinstance(n, ast.FunctionDef) and n.name == func)
        ]  # fmt: skip

    if others(old) != others(new):
        hits.append(f"{rel}: code other than `{func}` was changed")
    return hits


def run_probe(spec: str, tree: Path, base: str) -> list[str]:
    name, _, rest = spec.partition(":")
    rel, _, arg = rest.partition(":")
    if name == "scope":
        return scope(tree, rel, arg, base)
    fn = {
        "broad_except": broad_except, "swallowed": swallowed, "third_party": third_party,
        "hardcoded_secret": hardcoded_secret, "env_default": env_default,
    }[name]  # fmt: skip
    return fn(tree, rel)
