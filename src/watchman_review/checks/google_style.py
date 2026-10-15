"""Google Python Style Guide check (opt-in: `[review] style = "google"` in user config).

Deterministic and conservative, like the rest of Layer 1: it reports only on lines the change
added (or on functions the change introduced), caps repeats per file, and cites the guide's section
number in every finding. It covers the rules a program can decide reliably:

  §3.2      lines of at most 80 characters (not for imports, URLs or one-token lines)
  §3.16     naming (CapWords classes, lower_with_under functions/variables/arguments/modules)
  §2.21     type annotations on new public functions
  §3.8      module docstrings, one-line summaries ending in punctuation, Args:/Returns: sections
  §2.2/2.3  no relative imports
  §2.7      no comprehension with several `for` clauses or filters
  §2.17     no @staticmethod
  §2.5      no `global` statements
  §2.14     implicit truthiness (`len(x) == 0`, `== None`, `== True`)
  §3.10.1   no f-strings in logging calls
  §3.12     TODO comments carry a context link: `TODO(link): text`
  §3.17     no unguarded `main()` call at import time
  §2.4      try bodies kept small

Not covered, on purpose: other formatting (indentation, whitespace, blank lines, §3.3 to §3.6)
belongs to a formatter; "import modules, not
names" (§2.2) and `assert` use (§2.4) are judgment calls the agent gets through the style preset.
This is a best-effort detector of departures. It does not certify compliance with the guide.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter
from collections.abc import Iterator
from pathlib import PurePosixPath

from watchman_review.checks.base import DETERMINISTIC_CONFIDENCE, Check, CheckContext
from watchman_review.checks.quality import _base_source, parse_functions
from watchman_review.checks.scope import is_test_path
from watchman_review.gitutil import FileDiff, FileStatus
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

GUIDE = "Google Python Style Guide"
MAX_PER_RULE_PER_FILE = 3
MAX_TRY_BODY_LINES = 30  # a heuristic: the guide says "minimize" and gives no number
SUMMARY_MAX = 80
LINE_MAX = 80  # §3.2

_SNAKE = re.compile(r"_{0,2}[a-z][a-z0-9_]*")
_CAPS = re.compile(r"_?[A-Z][A-Z0-9_]*")
_CAPWORDS = re.compile(r"_?[A-Z][a-zA-Z0-9]*")
_UNITTEST_NAMES = {
    "setUp",
    "tearDown",
    "setUpClass",
    "tearDownClass",
    "setUpModule",
    "tearDownModule",
    "runTest",
    "asyncSetUp",
    "asyncTearDown",
}
_LOG_RECEIVERS = {"logging", "logger", "log", "_logger", "_log", "LOGGER", "_LOGGER"}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal"}
_LONG_CONSTANT = re.compile(r"[\w.]+\s*(?::[^=]+)?=\s*[rRbBuUfF]?(['\"])\S*\1,?")
_TODO = re.compile(r"\bTODO\b(?!\([^)\s][^)]*\):)")
_STATIC_ALLOWED_RECEIVERS = {"self", "cls"}


def _dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


_CAMEL = re.compile(r"_{0,2}[a-z][a-z0-9]*[A-Z]\w*")


def bad_variable(name: str) -> bool:
    """camelCase variables. PascalCase is left alone: it is usually a class or type alias."""
    return bool(_CAMEL.fullmatch(name))


def bad_snake(name: str) -> bool:
    """True if `name` is camelCase/PascalCase where lower_with_under is expected."""
    return not (_dunder(name) or _SNAKE.fullmatch(name) or name == "_")


def _target_names(node: ast.AST) -> Iterator[ast.Name]:
    if isinstance(node, ast.Name):
        yield node
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _target_names(elt)


def _own_nodes(fn: ast.AST) -> Iterator[ast.AST]:
    """Nodes in a function's own body, not in nested functions or classes."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


class _Emitter:
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []
        self.counts: Counter[str] = Counter()

    def add(
        self,
        rule: str,
        section: str,
        line: int,
        summary: str,
        fix: str,
        category: Category = Category.CONVENTION_VIOLATION,
    ) -> None:
        if self.counts[rule] >= MAX_PER_RULE_PER_FILE:
            return
        self.counts[rule] += 1
        self.findings.append(
            Finding(
                category=category,
                severity=Severity.MINOR,
                confidence=DETERMINISTIC_CONFIDENCE - 0.1,
                file=self.path,
                line_start=line,
                line_end=line,
                summary=f"{summary} ({GUIDE} §{section})"[:300],
                evidence=f"{self.path}:{line}: {summary}. {GUIDE} §{section}.",
                suggested_fix=fix,
                source=Source.DETERMINISTIC,
                rule=f"google:{rule}",
            )
        )


class GoogleStyleCheck(Check):
    name = "google_style"

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        for f in ctx.changed_python_files():
            if f.path.endswith(".pyi"):
                continue
            source = (ctx.tree / f.path).read_text(errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            em = _Emitter(f.path)
            test = is_test_path(f.path)
            added = set(f.added)
            self._file_level(em, f, tree, test)
            self._nodes(em, tree, added, test)
            self._comments(em, source, added)
            self._line_length(em, f)
            self._new_functions(em, ctx, f, source, test)
            findings += em.findings
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        summary = (
            f"{len(findings)} departure(s) from the {GUIDE}"
            if findings
            else f"no departure from the {GUIDE} that the check can detect"
        )
        return self.result(status, summary, findings)

    # ------------------------------------------------------------------ file level
    @staticmethod
    def _file_level(em: _Emitter, f: FileDiff, tree: ast.Module, test: bool) -> None:
        if f.status is not FileStatus.ADDED:
            return
        stem = PurePosixPath(f.path).stem
        if not _dunder(stem) and not re.fullmatch(r"_?[a-z][a-z0-9_]*", stem):
            em.add(
                "3.16.3-filename",
                "3.16.3",
                1,
                f"Module file name `{stem}.py` is not lower_with_under",
                "rename the file to lower_with_under (no dashes or capitals) so it can be imported",
            )
        has_defs = any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in tree.body
        )
        if not test and not _dunder(stem) and has_defs and ast.get_docstring(tree) is None:
            em.add(
                "3.8.2-module-docstring",
                "3.8.2",
                1,
                f"New module `{f.path}` has no module docstring",
                "start the file with a docstring: a one-line summary, then what it holds",
                Category.MAINTAINABILITY,
            )

    # ------------------------------------------------------------------ AST rules
    def _nodes(self, em: _Emitter, tree: ast.Module, added: set[int], test: bool) -> None:
        module_level = {id(n) for n in tree.body}
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            if line not in added:
                continue
            if isinstance(node, ast.ClassDef):
                if not _CAPWORDS.fullmatch(node.name):
                    em.add(
                        "3.16-class-name",
                        "3.16",
                        line,
                        f"Class `{node.name}` is not CapWords",
                        "name classes in CapWords, e.g. `InventoryItem`",
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function_signature(em, node, test)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for name in _target_names(t):
                        if bad_variable(name.id):
                            em.add(
                                "3.16-variable-name",
                                "3.16",
                                line,
                                f"Variable `{name.id}` is not lower_with_under",
                                "use lower_with_under (CAPS_WITH_UNDER for constants)",
                            )
            elif isinstance(node, ast.ImportFrom) and node.level > 0:
                em.add(
                    "2.2-relative-import",
                    "2.2",
                    line,
                    "Relative import",
                    "import with the full package path, e.g. `from app import notes`",
                )
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                filters = sum(len(g.ifs) for g in node.generators)
                if len(node.generators) > 1 or filters > 1:
                    em.add(
                        "2.7-comprehension",
                        "2.7",
                        line,
                        "Comprehension with several `for` clauses or filters",
                        "write it as an ordinary loop; optimize for readability, not brevity",
                    )
            elif isinstance(node, ast.Global):
                em.add(
                    "2.5-global",
                    "2.5",
                    line,
                    "`global` statement (mutable global state)",
                    "pass the value in or keep it on an object instead of mutating a global",
                )
            elif isinstance(node, ast.Compare):
                self._compare(em, node, test)
            elif isinstance(node, ast.Call):
                self._call(em, node)
            elif isinstance(node, ast.Try):
                self._try(em, node)
            elif isinstance(node, ast.Expr) and id(node) in module_level and not test:
                call = node.value
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "main"
                ):
                    em.add(
                        "3.17-main-guard",
                        "3.17",
                        line,
                        "`main()` runs at import time",
                        'wrap it in `if __name__ == "__main__":`',
                    )

    @staticmethod
    def _function_signature(
        em: _Emitter, node: ast.FunctionDef | ast.AsyncFunctionDef, test: bool
    ) -> None:
        name = node.name
        exempt = _dunder(name) or name in _UNITTEST_NAMES or (test and name.startswith("test"))
        if not exempt and bad_snake(name):
            em.add(
                "3.16-function-name",
                "3.16",
                node.lineno,
                f"Function `{name}` is not lower_with_under",
                "name functions and methods in lower_with_under",
            )
        for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if bad_snake(a.arg) and a.arg not in ("self", "cls"):
                em.add(
                    "3.16-argument-name",
                    "3.16",
                    node.lineno,
                    f"Argument `{a.arg}` is not lower_with_under",
                    "name arguments in lower_with_under",
                )
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == "staticmethod":
                em.add(
                    "2.17-staticmethod",
                    "2.17",
                    node.lineno,
                    f"`@staticmethod` on `{name}`",
                    "write a module-level function instead of a staticmethod",
                )

    @staticmethod
    def _compare(em: _Emitter, node: ast.Compare, test: bool) -> None:
        operands = [node.left, *node.comparators]
        for op, right in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant):
                if right.value is None:
                    em.add(
                        "2.14-none-compare",
                        "2.14",
                        node.lineno,
                        "Comparison to None with `==`/`!=`",
                        "use `is None` / `is not None`",
                    )
                elif right.value is True or right.value is False:
                    em.add(
                        "2.14-bool-compare",
                        "2.14",
                        node.lineno,
                        f"Comparison to {right.value} with `==`/`!=`",
                        "test the value directly: `if x:` / `if not x:`",
                    )
        for operand in () if test else operands:
            if (
                isinstance(operand, ast.Call)
                and isinstance(operand.func, ast.Name)
                and operand.func.id == "len"
                and any(isinstance(c, ast.Constant) and c.value == 0 for c in operands)
                and not any(isinstance(c, ast.Constant) and c.value not in (0,) for c in operands)
            ):
                em.add(
                    "2.14-len-compare",
                    "2.14",
                    node.lineno,
                    "`len(...)` compared with 0",
                    "use the sequence's truthiness: `if not items:` / `if items:`",
                )
                return

    @staticmethod
    def _call(em: _Emitter, node: ast.Call) -> None:
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr in _LOG_METHODS
            and isinstance(fn.value, ast.Name)
            and fn.value.id in _LOG_RECEIVERS
            and node.args
            and isinstance(node.args[0], ast.JoinedStr)
        ):
            em.add(
                "3.10.1-logging-fstring",
                "3.10.1",
                node.lineno,
                "f-string passed to a logging call",
                'use %-style placeholders: `logger.info("loaded %s items", count)`',
            )

    @staticmethod
    def _try(em: _Emitter, node: ast.Try) -> None:
        if not node.body:
            return
        end = node.body[-1].end_lineno or node.body[-1].lineno
        size = end - node.body[0].lineno + 1
        if size > MAX_TRY_BODY_LINES:
            em.add(
                "2.4-try-body",
                "2.4",
                node.lineno,
                f"`try` body is {size} lines long",
                "keep only the statements that can raise the handled exception inside `try`",
            )

    # ------------------------------------------------------------------ line length
    @staticmethod
    def _line_length(em: _Emitter, f: FileDiff) -> None:
        """One finding per file (not per line) so a wide module can't crowd out other feedback."""
        long_lines: list[int] = []
        for line_no, text in sorted(f.added.items()):
            stripped = text.strip()
            if len(text.rstrip()) <= LINE_MAX:
                continue
            exempt = (
                stripped.startswith(("import ", "from "))
                or "http://" in stripped
                or "https://" in stripped
                or "pylint:" in stripped
                or " " not in stripped  # a single long token (path, URL, identifier)
                or _LONG_CONSTANT.fullmatch(stripped)  # `NAME = "no-whitespace-string"`
            )
            if not exempt:
                long_lines.append(line_no)
        if long_lines:
            where = ", ".join(str(n) for n in long_lines[:5])
            more = f" (+{len(long_lines) - 5} more)" if len(long_lines) > 5 else ""
            em.add(
                "3.2-line-length", "3.2", long_lines[0],
                f"{len(long_lines)} added line(s) exceed {LINE_MAX} characters: {where}{more}",
                "wrap them inside parentheses, or run the formatter with a line length of 80",
            )  # fmt: skip

    # ------------------------------------------------------------------ comments
    @staticmethod
    def _comments(em: _Emitter, source: str, added: set[int]) -> None:
        try:
            for tok in tokenize.generate_tokens(io.StringIO(source).readline):
                if (
                    tok.type == tokenize.COMMENT
                    and tok.start[0] in added
                    and _TODO.search(tok.string)
                ):
                    em.add(
                        "3.12-todo",
                        "3.12",
                        tok.start[0],
                        "TODO comment without a context link",
                        "write `# TODO(link-or-bug): what and when`, e.g. `# TODO(b/12345): ...`",
                    )
        except (tokenize.TokenError, IndentationError):
            return

    # ------------------------------------------------------------------ new public functions
    def _new_functions(
        self, em: _Emitter, ctx: CheckContext, f: FileDiff, source: str, test: bool
    ) -> None:
        if test:
            return
        stub = (ctx.tree / f.path).with_suffix(".pyi")
        annotated_in_stub = stub.is_file()  # §2.21 allows annotations in a .pyi next to the module
        head = parse_functions(source)
        base_src = _base_source(ctx, f)
        base = (parse_functions(base_src) if base_src is not None else {}) or {}
        tree = ast.parse(source)
        nodes = {
            n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for n in cls.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes[f"{cls.name}.{n.name}"] = n
        for q, node in nodes.items():
            info = (head or {}).get(q)
            if info is None or q in base or not info.public or "._" in f".{q}":
                continue
            if not all(not part.startswith("_") for part in q.split(".")):
                continue
            if not annotated_in_stub:
                self._annotations(em, node, q)
            self._docstring_rules(em, node, q)

    @staticmethod
    def _annotations(em: _Emitter, node: ast.FunctionDef | ast.AsyncFunctionDef, q: str) -> None:
        a = node.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs]
        if params and params[0].arg in ("self", "cls"):
            params = params[1:]
        missing = [p.arg for p in params if p.annotation is None]
        if a.vararg and a.vararg.annotation is None:
            missing.append(f"*{a.vararg.arg}")
        if a.kwarg and a.kwarg.annotation is None:
            missing.append(f"**{a.kwarg.arg}")
        no_return = node.returns is None and node.name != "__init__"
        if missing or no_return:
            what = ", ".join([*missing, *(["return type"] if no_return else [])])
            em.add(
                "2.21-annotations",
                "2.21",
                node.lineno,
                f"New public `{q}` is missing type annotations ({what})",
                "annotate every parameter and the return type of public functions",
                Category.MAINTAINABILITY,
            )

    @staticmethod
    def _docstring_rules(
        em: _Emitter, node: ast.FunctionDef | ast.AsyncFunctionDef, q: str
    ) -> None:
        doc = ast.get_docstring(node)
        if doc is None:
            return
        summary = doc.splitlines()[0].strip()
        if len(summary) > SUMMARY_MAX or not summary.endswith((".", "?", "!")):
            em.add(
                "3.8.1-summary",
                "3.8.1",
                node.lineno,
                f"Docstring summary of `{q}` is not one line of at most {SUMMARY_MAX} "
                "characters ending in punctuation",
                "make the first line a one-sentence summary ending in a period",
                Category.MAINTAINABILITY,
            )
        if "\n" not in doc.strip():
            return  # a one-line docstring may omit the sections (§3.8.3)
        a = node.args
        params = [
            p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs] if p.arg not in ("self", "cls")
        ]
        if params and "Args:" not in doc:
            em.add(
                "3.8.3-args",
                "3.8.3",
                node.lineno,
                f"Docstring of `{q}` has no `Args:` section",
                "list each parameter under `Args:` with a description",
                Category.MAINTAINABILITY,
            )
        returns_value = any(
            isinstance(n, ast.Return)
            and n.value is not None
            and not (isinstance(n.value, ast.Constant) and n.value.value is None)
            for n in _own_nodes(node)
        )
        yields = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own_nodes(node))
        head = doc.lstrip().split(None, 1)[0].rstrip(":").lower() if doc.strip() else ""
        starts_ret = head in ("return", "returns", "yield", "yields")
        if (
            (returns_value or yields)
            and not starts_ret
            and "Returns:" not in doc
            and "Yields:" not in doc
        ):
            label = "Yields:" if yields else "Returns:"
            em.add(
                "3.8.3-returns",
                "3.8.3",
                node.lineno,
                f"Docstring of `{q}` has no `{label}` section",
                f"describe the value under `{label}`",
                Category.MAINTAINABILITY,
            )
