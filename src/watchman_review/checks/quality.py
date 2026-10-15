"""Maintainability check: does this change make the code harder to read, test, or change?

Deterministic and conservative. A finding is raised only when the change **creates** a
problem in a function it touches (a new function over the limit, or an existing one that this
change pushes across it), never for debt that was already there: adding a branch to a function
that was already too complex is not what makes it too complex. The measures are ordinary and
reproducible:

  complexity   McCabe cyclomatic complexity > 10 (McCabe's proposed limit, cited by NIST 500-235)
  length       a function longer than 60 lines
  params       more than 5 parameters (ruff's PLR0913 default)
  nesting      blocks nested more than 4 deep
  docstring    a new public function/class without a docstring, but only in a module where the
               existing public API is almost always documented (follow the code's own practice)
  api docs     three or more new public functions/classes in one file with fewer than half
               documented (a new module has no convention yet; this is the common-sense floor)
  duplicate    six or more added lines that already exist elsewhere in production code

These are proxies for maintainability (modularity, analysability, modifiability, testability), not a
certification of any standard or style guide.
Limits are fixed here, not repo-configurable: a reviewed repo must not loosen its own review.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from watchman_review.checks.base import DETERMINISTIC_CONFIDENCE, Check, CheckContext
from watchman_review.checks.scope import is_test_path
from watchman_review.config import StyleGuide
from watchman_review.gitutil import FileDiff, FileStatus, GitError, run_git
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

MAX_COMPLEXITY = 10
MAX_LENGTH = 60
MAX_LENGTH_GOOGLE = (
    40  # §3.18: "if a function exceeds about 40 lines, think about whether it can be broken up"
)
MAX_PARAMS = 5
MAX_NESTING = 4
DUP_WINDOW = 6
DOC_MIN_PUBLIC = 3  # other documented/undocumented public defs needed to infer a convention
DOC_MIN_SHARE = 0.8
API_MIN_NEW = 3  # new public definitions in a file before its documentation is judged
API_MIN_SHARE = 0.5

_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.Assert)
_NESTING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_TRIVIAL = re.compile(r"^(?:import |from \S+ import |@|return$|pass$|else:$|try:$|\)|\]|\}|#)")


def complexity(fn: ast.AST) -> int:
    """McCabe cyclomatic complexity: decision points + 1 (boolean operators and
    comprehension conditions count; nested definitions count toward their parent)."""
    n = 1
    for node in ast.walk(fn):
        if isinstance(node, _BRANCHES):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            n += 1 + len(node.ifs)
        elif isinstance(node, ast.Match):
            n += len(node.cases)
    return n


def nesting(node: ast.AST, depth: int = 0) -> int:
    """Deepest block nesting; an `elif` is not deeper than its `if`."""
    deepest = depth
    elifs = {
        id(c.orelse[0])
        for c in ast.walk(node)
        if isinstance(c, ast.If) and len(c.orelse) == 1 and isinstance(c.orelse[0], ast.If)
    }
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFS):
            continue  # nested definitions are measured on their own
        deeper = isinstance(child, _NESTING) and id(child) not in elifs
        deepest = max(deepest, nesting(child, depth + 1 if deeper else depth))
    return deepest


def param_count(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    a = fn.args
    n = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
    n += (a.vararg is not None) + (a.kwarg is not None)
    if a.args and a.args[0].arg in ("self", "cls"):
        n -= 1
    return n


@dataclass
class FuncInfo:
    qualname: str
    line: int
    end: int
    complexity: int
    length: int
    params: int
    nesting: int
    public: bool
    documented: bool
    in_function: bool = False  # defined inside another function (a local helper)


def _collect(tree: ast.AST) -> dict[str, FuncInfo]:
    out: dict[str, FuncInfo] = {}

    def visit(node: ast.AST, prefix: str, in_func: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, prefix, in_func)
                continue
            is_func = not isinstance(child, ast.ClassDef)
            q = f"{prefix}{child.name}"
            end = child.end_lineno or child.lineno
            measured = is_func  # classes carry no complexity, length or parameter counts
            out[q] = FuncInfo(
                qualname=q,
                line=child.lineno,
                end=end,
                complexity=complexity(child) if measured else 0,
                length=end - child.lineno + 1 if measured else 0,
                params=0 if isinstance(child, ast.ClassDef) else param_count(child),
                nesting=nesting(child) if measured else 0,
                public=not child.name.startswith("_"),
                documented=ast.get_docstring(child) is not None,
                in_function=in_func,
            )
            visit(child, f"{q}.", in_func or is_func)

    visit(tree, "")
    return out


def parse_functions(source: str) -> dict[str, FuncInfo] | None:
    try:
        return _collect(ast.parse(source))
    except SyntaxError:
        return None


def _finding(
    path: str, info: FuncInfo, rule: str, summary: str, evidence: str, fix: str
) -> Finding:
    return Finding(
        category=Category.MAINTAINABILITY,
        severity=Severity.MINOR,
        confidence=DETERMINISTIC_CONFIDENCE - 0.1,
        file=path,
        line_start=info.line,
        line_end=info.line,
        summary=summary,
        evidence=evidence,
        suggested_fix=fix,
        source=Source.DETERMINISTIC,
        rule=rule,
    )


def _base_source(ctx: CheckContext, f: FileDiff) -> str | None:
    if f.status is FileStatus.ADDED:
        return None
    rev = ctx.diff.merge_base or ctx.diff.base
    if not rev:
        return None
    try:
        return run_git(ctx.repo_root, "show", f"{rev}:{f.old_path or f.path}", check=True)
    except GitError:
        return None


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


class QualityCheck(Check):
    name = "quality"

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        files = [
            f
            for f in ctx.changed_python_files()
            if not is_test_path(f.path) and not f.path.endswith(".pyi")
        ]
        for f in files:
            findings += self._structure(ctx, f)
        findings += self._duplicates(ctx, files)
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        summary = (
            f"{len(findings)} maintainability issue(s) introduced by this change"
            if findings
            else "no function the change touched crossed a maintainability limit"
        )
        return self.result(status, summary, findings)

    def _structure(self, ctx: CheckContext, f: FileDiff) -> list[Finding]:
        head = parse_functions((ctx.tree / f.path).read_text(errors="replace"))
        if head is None:
            return []
        base_src = _base_source(ctx, f)
        base = parse_functions(base_src) if base_src is not None else {}
        base = base or {}
        max_length = (
            MAX_LENGTH_GOOGLE if ctx.config.review.style is StyleGuide.GOOGLE else MAX_LENGTH
        )
        added = set(f.added)
        out: list[Finding] = []
        public_total = [i for i in head.values() if i.public]
        for q, info in head.items():
            if not added & set(range(info.line, info.end + 1)):
                continue  # the change didn't touch this function
            old = base.get(q)
            checks = (
                ("complexity", info.complexity, MAX_COMPLEXITY, old.complexity if old else 0),
                ("length", info.length, max_length, old.length if old else 0),
                ("params", info.params, MAX_PARAMS, old.params if old else 0),
                ("nesting", info.nesting, MAX_NESTING, old.nesting if old else 0),
            )
            for rule, now, limit, before in checks:
                if now > limit >= before:  # new (before = 0) or crossed the limit here
                    out.append(self._structure_finding(f.path, info, rule, now, limit, before, old))
            if old is None and info.public and not info.documented:
                out += self._docstring(f.path, info, q, head, public_total)
        if not any(x.rule == "quality:docstring" for x in out):
            out += self._api_docs(f.path, head, base)
        return out

    @staticmethod
    def _api_docs(path: str, head: dict[str, FuncInfo], base: dict[str, FuncInfo]) -> list[Finding]:
        new = [
            i
            for q, i in head.items()
            if q not in base
            and not i.in_function
            and all(not part.startswith("_") for part in q.split("."))
        ]
        undocumented = [i for i in new if not i.documented]
        if len(new) < API_MIN_NEW or len(new) - len(undocumented) >= len(new) * API_MIN_SHARE:
            return []
        first = min(undocumented, key=lambda i: i.line)
        return [
            _finding(
                path,
                first,
                "quality:api-docs",
                f"{len(undocumented)} of {len(new)} new public definitions in {path} have no "
                "docstring",
                f"{path}: new public definitions without a docstring: "
                f"{', '.join(i.qualname for i in undocumented[:6])}.",
                "add a one-line docstring saying what each does, its arguments and what it raises",
            )
        ]

    @staticmethod
    def _structure_finding(
        path: str,
        info: FuncInfo,
        rule: str,
        now: int,
        limit: int,
        before: int,
        old: FuncInfo | None,
    ) -> Finding:
        what = {
            "complexity": (
                "cyclomatic complexity",
                "split it into smaller functions (one per "
                "branch of the logic), or replace the if/elif chain with a lookup table",
            ),
            "length": ("lines long", "extract coherent blocks into named helper functions"),
            "params": (
                "parameters",
                "group related parameters into a small dataclass, or split the function",
            ),
            "nesting": (
                "levels of nesting",
                "use early returns/continue (guard clauses) or "
                "extract the inner block into a helper",
            ),
        }[rule]
        label = (
            f"`{info.qualname}` has {now} {what[0]}"
            if rule != "complexity"
            else (f"`{info.qualname}` has cyclomatic complexity {now}")
        )
        trend = "is new" if old is None else f"grew from {before}"
        return _finding(
            path,
            info,
            f"quality:{rule}",
            f"{label} (limit {limit})",
            f"{path}:{info.line} `{info.qualname}`: {rule} = {now}, over the limit of {limit}; "
            f"this function {trend} in this change.",
            what[1],
        )

    @staticmethod
    def _docstring(
        path: str, info: FuncInfo, q: str, head: dict[str, FuncInfo], public: list[FuncInfo]
    ) -> list[Finding]:
        others = [i for i in public if i.qualname != q and "." not in i.qualname]
        if "." in q or len(others) < DOC_MIN_PUBLIC:
            return []
        share = sum(i.documented for i in others) / len(others)
        if share < DOC_MIN_SHARE:
            return []
        return [
            _finding(
                path,
                info,
                "quality:docstring",
                f"New public `{q}` has no docstring, unlike the rest of this module",
                f"{path}:{info.line}: {round(share * 100)}% of the module's other public "
                f"definitions ({len(others)}) have docstrings; this one doesn't.",
                "add a short docstring in the same style as its neighbours",
            )
        ]

    def _duplicates(self, ctx: CheckContext, files: list[FileDiff]) -> list[Finding]:
        corpus: dict[str, set[tuple[str, int]]] = {}
        for p in sorted(ctx.tree.rglob("*.py")):
            rel = p.relative_to(ctx.tree).as_posix()
            if is_test_path(rel) or any(x in p.parts for x in (".venv", ".wm-venv", ".git")):
                continue
            lines = [_norm(x) for x in p.read_text(errors="replace").splitlines()]
            for i in range(len(lines) - DUP_WINDOW + 1):
                win = lines[i : i + DUP_WINDOW]
                if _substantive(win):
                    corpus.setdefault("\n".join(win), set()).add((rel, i + 1))
        out: list[Finding] = []
        for f in files:
            lines = [_norm(x) for x in (ctx.tree / f.path).read_text(errors="replace").splitlines()]
            i = 0
            while i <= len(lines) - DUP_WINDOW:
                win = lines[i : i + DUP_WINDOW]
                span_added = all((i + k + 1) in f.added for k in range(DUP_WINDOW))
                others = (
                    {h for h in corpus.get("\n".join(win), set()) if h != (f.path, i + 1)}
                    if span_added and _substantive(win)
                    else set()
                )
                others = {h for h in others if h[0] != f.path or abs(h[1] - (i + 1)) >= DUP_WINDOW}
                if others:
                    where = sorted(others)[0]
                    out.append(
                        Finding(
                            category=Category.DUPLICATION,
                            severity=Severity.MINOR,
                            confidence=DETERMINISTIC_CONFIDENCE - 0.15,
                            file=f.path,
                            line_start=i + 1,
                            line_end=i + DUP_WINDOW,
                            summary=f"Copy of existing code at {where[0]}:{where[1]}",
                            evidence=f"{f.path}:{i + 1}-{i + DUP_WINDOW} added {DUP_WINDOW}+ lines "
                            f"that already exist at {where[0]}:{where[1]}.",
                            suggested_fix="reuse or extract the existing code, don't copy it",
                            source=Source.DETERMINISTIC,
                            rule="quality:duplicate",
                        )
                    )
                    i += DUP_WINDOW  # one finding per copied block
                else:
                    i += 1
        return out


def _substantive(win: list[str]) -> bool:
    real = [x for x in win if len(x) > 3 and not _TRIVIAL.match(x)]
    return len(real) >= DUP_WINDOW - 1
