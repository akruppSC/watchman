"""Templates for dataset v3: readability and design defects typical of AI-written code.

v1 and v2 had no items for two categories the design doc names: `over_abstraction`
(needless wrappers, speculative parameters, dead code) and `convention_violation` (departures
from a project's own conventions). These templates add them, in the same mechanical style as
`mutators.py`: deterministic given the RNG, guided by the AST, leaving the file valid, and
returning the exact ground-truth location. No model writes the defects it will be graded on.

They live in their own module so the v1/v2 templates and schedules (and therefore the frozen
datasets' reproducibility) are untouched.
"""

from __future__ import annotations

import ast
import re

from evals.build.mutators import (
    _BUILTINS,
    Ctx,
    Location,
    Mutation,
    Template,
    _choose,
    _indent,
    _lines,
    _parents,
    _parse,
    _pick_file,
    _replace_segment,
    _single_line,
    _valid,
)
from watchman_review.schema import Category

_DEF_LINE = re.compile(
    r"^(?P<head>\s*(?:async\s+)?def\s+\w+\()(?P<params>.*)(?P<tail>\)\s*(?:->.*)?:\s*)$"
)
_EXTRA_PARAMS = ("verbose", "strict", "callback", "extra_options", "debug")


def _module_names(tree: ast.Module) -> set[str]:
    """Names bound at module level: imports, defs, classes, simple assignments."""
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Import | ast.ImportFrom):
            names |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    return names


def _top_level_of(tree: ast.Module, node: ast.AST) -> ast.stmt | None:
    for top in tree.body:
        end = getattr(top, "end_lineno", top.lineno)
        if top.lineno <= getattr(node, "lineno", -1) <= end:
            return top
    return None


def _insert_line_before(top: ast.stmt) -> int:
    decorators = getattr(top, "decorator_list", [])
    return int(min([top.lineno, *[d.lineno for d in decorators]]))


# ---------------------------------------------------------------- over_abstraction


def needless_wrapper(ctx: Ctx) -> Mutation | None:
    """Route an added call through a new pass-through wrapper that adds nothing."""
    for f, tree in _pick_file(ctx, ctx.src):
        parents = _parents(tree)
        module_names = _module_names(tree)
        calls = [
            c for c in ast.walk(tree)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id in module_names and c.func.id not in _BUILTINS
            and c.lineno in f.added and _single_line(c)
            and not c.func.id.startswith("_call_") and parents.get(c) is not None
        ]  # fmt: skip
        call = _choose(ctx.rng, calls)
        if call is None or not isinstance(call.func, ast.Name):
            continue
        top = _top_level_of(tree, call)
        if top is None:
            continue
        target = call.func.id
        name = f"_call_{target.lstrip('_')}"
        if name in module_names or re.search(rf"\b{re.escape(name)}\b", f.text):
            continue
        lines = _lines(f.text)
        line = lines[call.lineno - 1]
        assert call.func.end_col_offset is not None
        lines[call.lineno - 1] = _replace_segment(
            line, call.func.col_offset, call.func.end_col_offset, name
        )
        block = [
            f"def {name}(*args, **kwargs):\n",
            f"    return {target}(*args, **kwargs)\n",
            "\n",
            "\n",
        ]
        at = _insert_line_before(top)
        if call.lineno < at:
            continue
        lines[at - 1 : at - 1] = block
        new = "".join(lines)
        if not _valid(new):
            continue
        return Mutation(
            "needless_wrapper", Category.OVER_ABSTRACTION,
            f"Added `{name}`, a wrapper that only forwards to `{target}`, and routed a call "
            "through it for no reason.",
            {f.path: new}, Location(f.path, at, at + 1),
            [Location(f.path, call.lineno + len(block), call.lineno + len(block))],
        )  # fmt: skip
    return None


def unused_parameter(ctx: Ctx) -> Mutation | None:
    """Give an added function a keyword-only parameter that nothing uses."""
    for f, tree in _pick_file(ctx, ctx.src):
        fns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.lineno in f.added
            and not n.args.vararg and not n.args.kwarg and not n.args.kwonlyargs
            and not n.decorator_list and not n.name.startswith("__")
        ]  # fmt: skip
        fn = _choose(ctx.rng, fns)
        if fn is None:
            continue
        lines = _lines(f.text)
        m = _DEF_LINE.match(lines[fn.lineno - 1].rstrip("\n"))
        if m is None:
            continue
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        params = {a.arg for a in [*fn.args.args, *fn.args.posonlyargs]}
        choices = [p for p in _EXTRA_PARAMS if p not in used | params]
        pname = _choose(ctx.rng, choices)
        if pname is None:
            continue
        existing = m.group("params").strip()
        extra = f"*, {pname}=None"
        new_params = f"{existing}, {extra}" if existing else extra
        lines[fn.lineno - 1] = f"{m.group('head')}{new_params}{m.group('tail')}\n"
        new = "".join(lines)
        if not _valid(new):
            continue
        return Mutation(
            "unused_parameter", Category.OVER_ABSTRACTION,
            f"`{fn.name}` gained a keyword-only parameter `{pname}` that is never used "
            "(speculative generality).",
            {f.path: new}, Location(f.path, fn.lineno, fn.lineno),
        )  # fmt: skip
    return None


def dead_code(ctx: Ctx) -> Mutation | None:
    """Leave an unused 'compatibility' alias next to a brand-new function."""
    for f, tree in _pick_file(ctx, ctx.src):
        fns = [
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.lineno in f.added
            and not n.name.startswith("__") and not n.decorator_list
        ]  # fmt: skip
        fn = _choose(ctx.rng, fns)
        if fn is None or fn.end_lineno is None:
            continue
        name = f"_{fn.name.strip('_')}_legacy"
        if re.search(rf"\b{re.escape(name)}\b", f.text):
            continue
        block = [
            "\n",
            "\n",
            f"def {name}(*args, **kwargs):\n",
            '    """Kept for backwards compatibility."""\n',
            f"    return {fn.name}(*args, **kwargs)\n",
        ]
        lines = _lines(f.text)
        lines[fn.end_lineno : fn.end_lineno] = block
        new = "".join(lines)
        if not _valid(new):
            continue
        start = fn.end_lineno + 3
        return Mutation(
            "dead_code", Category.OVER_ABSTRACTION,
            f"`{name}` is an unused alias of the new `{fn.name}`; nothing calls it "
            "(dead code).",
            {f.path: new}, Location(f.path, start, start + 2),
        )  # fmt: skip
    return None


# ---------------------------------------------------------------- convention_violation


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.capitalize() for p in rest)


def camel_case_rename(ctx: Ctx) -> Mutation | None:
    """Rename a snake_case local variable to camelCase inside an added function."""
    for f, tree in _pick_file(ctx, ctx.src):
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.lineno in f.added]
        ctx.rng.shuffle(fns)
        for fn in fns:
            if any(isinstance(n, ast.Global | ast.Nonlocal) for n in ast.walk(fn)):
                continue
            params = {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs]}
            all_names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            stores = [
                n for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                and "_" in n.id.strip("_") and n.id.islower() and n.id not in params
                and n.lineno in f.added
            ]  # fmt: skip
            store = _choose(ctx.rng, stores)
            if store is None:
                continue
            old, new_id = store.id, _camel(store.id.strip("_"))
            if new_id in all_names or new_id in _module_names(tree) or new_id == old:
                continue
            occ = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == old]
            if len(occ) < 2:
                continue
            lines = _lines(f.text)
            for n in sorted(occ, key=lambda n: (n.lineno, n.col_offset), reverse=True):
                assert n.end_col_offset is not None
                lines[n.lineno - 1] = _replace_segment(
                    lines[n.lineno - 1], n.col_offset, n.end_col_offset, new_id
                )
            new = "".join(lines)
            if not _valid(new):
                continue
            rows = sorted({n.lineno for n in occ})
            return Mutation(
                "camel_case_rename", Category.CONVENTION_VIOLATION,
                f"Local variable `{old}` renamed to camelCase `{new_id}` in `{fn.name}`, "
                "against the project's snake_case convention.",
                {f.path: new}, Location(f.path, rows[0], rows[0]),
                [Location(f.path, r, r) for r in rows[1:]],
            )  # fmt: skip
    return None


def wildcard_import(ctx: Ctx) -> Mutation | None:
    """Replace an added `from x import a, b` with `from x import *`."""
    for f, tree in _pick_file(ctx, ctx.src):
        cands = [
            n for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.lineno in f.added and _single_line(n)
            and n.module != "__future__" and n.names and n.names[0].name != "*"
        ]  # fmt: skip
        node = _choose(ctx.rng, cands)
        if node is None:
            continue
        lines = _lines(f.text)
        line = lines[node.lineno - 1]
        mod = "." * node.level + (node.module or "")
        lines[node.lineno - 1] = f"{_indent(line)}from {mod} import *\n"
        new = "".join(lines)
        if not _valid(new):
            continue
        return Mutation(
            "wildcard_import", Category.CONVENTION_VIOLATION,
            f"`from {mod} import *` replaces explicit imports, hiding where names come from.",
            {f.path: new}, Location(f.path, node.lineno, node.lineno),
        )  # fmt: skip
    return None


def debug_print(ctx: Ctx) -> Mutation | None:
    """Leave a debugging `print` after an added assignment in library code."""
    for f, tree in _pick_file(ctx, ctx.src):
        parents = _parents(tree)
        cands = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name) and _single_line(n) and n.lineno in f.added
            and isinstance(parents.get(n), ast.FunctionDef | ast.If | ast.For | ast.With)
        ]  # fmt: skip
        node = _choose(ctx.rng, cands)
        if node is None or not isinstance(node.targets[0], ast.Name):
            continue
        lines = _lines(f.text)
        ind = _indent(lines[node.lineno - 1])
        name = node.targets[0].id
        lines.insert(node.lineno, f'{ind}print(f"DEBUG {name}: {{{name}!r}}")\n')
        new = "".join(lines)
        if not _valid(new) or _parse(new) is None:
            continue
        return Mutation(
            "debug_print", Category.CONVENTION_VIOLATION,
            f"Debugging `print` of `{name}` left in library code.",
            {f.path: new}, Location(f.path, node.lineno + 1, node.lineno + 1),
        )  # fmt: skip
    return None


TEMPLATES_V3: dict[str, Template] = {
    "needless_wrapper": needless_wrapper,
    "unused_parameter": unused_parameter,
    "dead_code": dead_code,
    "camel_case_rename": camel_case_rename,
    "wildcard_import": wildcard_import,
    "debug_print": debug_print,
}

# 30 items: five per template, for the two categories v1 and v2 do not cover.
SCHEDULE_V3: dict[str, int] = {t: 5 for t in TEMPLATES_V3}
