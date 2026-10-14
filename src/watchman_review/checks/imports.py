"""Dependency/import existence check: catches hallucinated packages and slopsquatting.

For every import added by the diff:
  stdlib or repo-local           -> ok
  declared in a manifest         -> ok
  installed in repo venv only    -> minor: used but not declared
  exists on PyPI, not declared   -> major: undeclared dependency (and a security flag if the
                                    package looks freshly registered)
  not on PyPI under any name     -> blocker: hallucinated dependency

Newly added manifest entries are also checked against PyPI.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from watchman_review.checks.base import Check, CheckContext
from watchman_review.checks.manifests import declared, find_manifests, is_manifest
from watchman_review.gitutil import GitError, run_git
from watchman_review.pypi import Lookup, PackageIndex, PackageInfo, normalize
from watchman_review.sandbox import find_repo_python, run_static_tool
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source

# import name -> distribution name, for common packages whose names differ.
IMPORT_TO_DIST: dict[str, str] = {
    "PIL": "pillow", "yaml": "pyyaml", "cv2": "opencv-python", "sklearn": "scikit-learn",
    "skimage": "scikit-image", "bs4": "beautifulsoup4", "dateutil": "python-dateutil",
    "attr": "attrs", "jwt": "pyjwt", "dotenv": "python-dotenv", "serial": "pyserial",
    "Crypto": "pycryptodome", "OpenSSL": "pyopenssl", "git": "gitpython", "docx": "python-docx",
    "pptx": "python-pptx", "zmq": "pyzmq", "magic": "python-magic", "multipart": "python-multipart",
    "jose": "python-jose", "slugify": "python-slugify", "telegram": "python-telegram-bot",
    "google": "google-api-core", "grpc": "grpcio", "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary", "pkg_resources": "setuptools", "setuptools": "setuptools",
    "_pytest": "pytest", "pytest": "pytest", "Levenshtein": "levenshtein", "fitz": "pymupdf",
    "win32api": "pywin32", "win32con": "pywin32", "usb": "pyusb", "nacl": "pynacl",
    "socks": "pysocks", "lxml": "lxml", "ruamel": "ruamel-yaml", "markdown": "markdown",
    "tomli_w": "tomli-w", "typing_extensions": "typing-extensions", "websocket": "websocket-client",
}  # fmt: skip

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "site-packages",
              "__pycache__", ".tox", ".nox", ".mypy_cache", ".ruff_cache", ".wm-venv"}  # fmt: skip

_DIST_PROBE = (
    "import importlib.metadata as m, json; "
    "print(json.dumps({k: v for k, v in m.packages_distributions().items()}))"
)


@dataclass
class AddedImport:
    module: str  # top-level name
    file: str
    line: int
    guarded: bool  # inside try/except ImportError


def local_module_names(tree: Path) -> set[str]:
    names: set[str] = set()
    for path in tree.rglob("*.py"):
        rel = path.relative_to(tree)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        names.add(path.stem)
        names.update(rel.parts[:-1])
    return names


def _import_guarded(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.Try):
            for h in cur.handlers:
                names = []
                if isinstance(h.type, ast.Name):
                    names = [h.type.id]
                elif isinstance(h.type, ast.Tuple):
                    names = [e.id for e in h.type.elts if isinstance(e, ast.Name)]
                if h.type is None or {"ImportError", "ModuleNotFoundError", "Exception"} & set(
                    names
                ):
                    return True
        cur = parents.get(cur)
    return False


def added_imports(source: str, path: str, added_lines: set[int]) -> list[AddedImport]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    out: list[AddedImport] = []
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module]
        else:
            continue
        span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if not span & added_lines:
            continue
        guarded = _import_guarded(node, parents)
        out += [AddedImport(m.split(".")[0], path, node.lineno, guarded) for m in mods]
    return sorted(out, key=lambda i: (i.line, i.module))


def dist_candidates(module: str) -> list[str]:
    cands = [IMPORT_TO_DIST.get(module, module), module, f"python-{module}", f"py{module}"]
    seen: list[str] = []
    for c in cands:
        n = normalize(c)
        if n not in seen:
            seen.append(n)
    return seen


def venv_packages(repo_root: Path) -> dict[str, set[str]] | None:
    """Map top-level import name -> normalized dists, from the repo's own venv."""
    py = find_repo_python(repo_root)
    if py is None:
        return None
    res = run_static_tool([str(py), "-c", _DIST_PROBE], repo_root, timeout=30)
    if not res.ok:
        return None
    try:
        raw = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    return {k: {normalize(d) for d in v} for k, v in raw.items()}


def _finding(
    cat: Category, sev: Severity, conf: float, file: str, line: int, summary: str, evidence: str,
    rule: str, fix: str | None = None,
) -> Finding:  # fmt: skip
    return Finding(
        category=cat, severity=sev, confidence=conf, file=file, line_start=line, line_end=line,
        summary=summary, evidence=evidence, suggested_fix=fix, source=Source.DETERMINISTIC,
        rule=rule,
    )  # fmt: skip


def _describe(info: PackageInfo) -> str:
    age = info.age_days()
    age_s = f"{age:.0f} days old" if age is not None else "unknown age"
    return f"{info.release_count} release(s), {age_s}"


class ImportsCheck(Check):
    name = "imports"

    def __init__(self, index: PackageIndex | None = None) -> None:
        self._index = index

    def run(self, ctx: CheckContext) -> CheckResult:
        index = self._index or PackageIndex(
            http=ctx.http, cache_dir=ctx.cache_dir, offline=ctx.offline
        )
        manifests = find_manifests(ctx.tree)
        has_manifest = any(not m.endswith(".lock") for m in manifests)

        def read_head(p: str) -> str | None:
            try:
                return (ctx.tree / p).read_text(errors="replace")
            except OSError:
                return None

        declared_head = declared(manifests, read_head)
        venv = venv_packages(ctx.repo_root)
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        local = local_module_names(ctx.tree)
        findings: list[Finding] = []
        unknown: set[str] = set()
        checked = 0

        for f in ctx.changed_python_files():
            source = (ctx.tree / f.path).read_text(errors="replace")
            for imp in added_imports(source, f.path, f.changed_line_numbers()):
                checked += 1
                mod = imp.module
                if mod in stdlib or mod in local:
                    continue
                cands = dist_candidates(mod)
                provided = (venv or {}).get(mod, set())
                if (set(cands) | provided) & declared_head:
                    continue
                if provided:
                    if has_manifest:
                        findings.append(_finding(
                            Category.CORRECTNESS, Severity.MINOR, 0.75, f.path, imp.line,
                            f"Import '{mod}' is installed but not declared as a dependency",
                            f"'{mod}' is provided by {sorted(provided)} in the repo venv, but no "
                            f"manifest ({', '.join(manifests) or 'none'}) declares it.",
                            "imports:undeclared", "Add it to the project dependencies.",
                        ))  # fmt: skip
                    continue
                infos = [index.lookup(c) for c in cands]
                existing = [i for i in infos if i.status is Lookup.EXISTS]
                if any(i.status is Lookup.UNKNOWN for i in infos) and not existing:
                    unknown.add(mod)
                    continue
                if not existing:
                    findings.append(_finding(
                        Category.HALLUCINATED_DEPENDENCY,
                        Severity.MINOR if imp.guarded else Severity.BLOCKER,
                        0.6 if imp.guarded else 0.9, f.path, imp.line,
                        f"Import '{mod}' does not resolve to stdlib, the repo, or any PyPI package",
                        f"Not in stdlib; no local module '{mod}'; not declared; PyPI lookup "
                        f"returned 404 for {cands}."
                        + (" Import is guarded by try/except ImportError." if imp.guarded else ""),
                        "imports:missing",
                    ))  # fmt: skip
                    continue
                best = existing[0]
                if best.looks_suspicious():
                    findings.append(_finding(
                        Category.SECURITY, Severity.MAJOR, 0.7, f.path, imp.line,
                        f"Import '{mod}' maps to a very new or barely-published PyPI package",
                        f"PyPI '{best.name}': {_describe(best)}. Possible slopsquatting of a "
                        "hallucinated name.",
                        "imports:suspicious-package",
                    ))  # fmt: skip
                if has_manifest:
                    findings.append(_finding(
                        Category.CORRECTNESS, Severity.MAJOR, 0.8, f.path, imp.line,
                        f"Third-party import '{mod}' is not declared as a dependency",
                        f"PyPI has '{best.name}' ({_describe(best)}) but no manifest declares "
                        f"it ({', '.join(manifests)}); a fresh install will fail to import.",
                        "imports:undeclared", f"Add '{best.name}' to the project dependencies.",
                    ))  # fmt: skip

        findings += self._check_new_manifest_entries(ctx, manifests, index, unknown)

        summary = f"checked {checked} added import(s)"
        if unknown:
            summary += f"; PyPI unreachable for {sorted(unknown)}"
        if findings:
            return self.result(CheckStatus.FAILED, summary, findings)
        return self.result(CheckStatus.PASSED, summary)

    def _check_new_manifest_entries(
        self, ctx: CheckContext, manifests: list[str], index: PackageIndex, unknown: set[str]
    ) -> list[Finding]:
        changed = [
            f for f in ctx.diff.files if is_manifest(f.path) and not f.path.endswith(".lock")
        ]
        if not changed:
            return []

        def read_base(p: str) -> str | None:
            try:
                return run_git(ctx.repo_root, "show", f"{ctx.diff.base}:{p}")
            except GitError:
                return None

        def read_head(p: str) -> str | None:
            try:
                return (ctx.tree / p).read_text(errors="replace")
            except OSError:
                return None

        paths = [f.path for f in changed]
        new = declared(paths, read_head, include_locks=False) - declared(
            paths, read_base, include_locks=False
        )
        findings: list[Finding] = []
        for name in sorted(new):
            info = index.lookup(name)
            file, line = paths[0], 0
            for f in changed:
                hit = next((n for n, t in f.added.items() if name in normalize(t)), None)
                if hit is not None:
                    file, line = f.path, hit
                    break
            if info.status is Lookup.UNKNOWN:
                unknown.add(name)
            elif info.status is Lookup.MISSING:
                findings.append(_finding(
                    Category.HALLUCINATED_DEPENDENCY, Severity.BLOCKER, 0.95, file, line,
                    f"New dependency '{name}' does not exist on PyPI",
                    f"Added to {file}; PyPI lookup for '{name}' returned 404.",
                    "imports:manifest-missing",
                ))  # fmt: skip
            elif info.looks_suspicious():
                findings.append(_finding(
                    Category.SECURITY, Severity.MAJOR, 0.7, file, line,
                    f"New dependency '{name}' is a very new or barely-published package",
                    f"PyPI '{name}': {_describe(info)}. Verify it is the intended package.",
                    "imports:suspicious-package",
                ))  # fmt: skip
        return findings
