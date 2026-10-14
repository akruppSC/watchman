"""Reviewer tools.

Every tool is read-only, except run_tests and run_linter, which execute inside the
sandbox. Paths are confined to the head snapshot, and `.git` is never readable. The
package-registry lookup is the only tool that touches the network.

Tool output is data about untrusted code; the loop wraps it in <untrusted_tool_output> tags.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchman_review.checks.base import CheckContext
from watchman_review.checks.ruff_check import PITFALL_RULES, repo_has_ruff_config, ruff_bin
from watchman_review.checks.tests_check import parse_failures, run_pytest
from watchman_review.config import Config
from watchman_review.gitutil import Diff, GitError, run_git
from watchman_review.llm.types import ToolSpec
from watchman_review.pypi import Lookup, PackageIndex
from watchman_review.sandbox import Sandbox, run_static_tool

MAX_OUTPUT_CHARS = 12_000
MAX_READ_LINES = 400
MAX_BLAME_LINES = 200
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 2_000
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
              ".ruff_cache", ".tox", ".nox", ".wm-venv", "build", "dist"}  # fmt: skip
_SELECTOR_RE = re.compile(r"^[\w./\-]+(?:::[\w\[\]\-.,:]+)*$")


class ToolError(Exception):
    """A tool call failed; the message is returned to the model, never raised to the user."""


@dataclass
class ToolContext:
    repo_root: Path
    tree: Path
    diff: Diff
    config: Config
    head_rev: str | None  # None => live working tree
    sandbox: Sandbox | None
    index: PackageIndex
    conventions: str | None
    task: str | None = None
    check_ctx: CheckContext | None = None


ToolFn = Callable[[ToolContext, dict[str, Any]], str]


@dataclass
class Tool:
    spec: ToolSpec
    fn: ToolFn


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 200] + f"\n... [truncated {len(text) - limit + 200} chars]"


def resolve_in_tree(tree: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel.strip():
        raise ToolError("path must be a non-empty repo-relative string")
    rel = rel.strip().removeprefix("./")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ToolError("absolute paths are not allowed; use a repo-relative path")
    if ".git" in candidate.parts:
        raise ToolError("access to .git is not allowed")
    root = tree.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise ToolError(f"path {rel!r} escapes the repository") from e
    return resolved


def _int(args: dict[str, Any], key: str, default: int | None = None) -> int:
    val = args.get(key, default)
    if val is None:
        raise ToolError(f"missing required integer argument {key!r}")
    try:
        return int(val)
    except (TypeError, ValueError) as e:
        raise ToolError(f"argument {key!r} must be an integer") from e


def _str(args: dict[str, Any], key: str, default: str | None = None) -> str:
    val = args.get(key, default)
    if not isinstance(val, str) or not val:
        raise ToolError(f"missing required string argument {key!r}")
    return val


def _rel(tree: Path, p: Path) -> str:
    return p.relative_to(tree.resolve()).as_posix()


# --- tools -------------------------------------------------------------------


def get_diff(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = args.get("path")
    if not path:
        return truncate(ctx.diff.raw) or "(empty diff)"
    blocks = re.split(r"(?m)^(?=diff --git )", ctx.diff.raw)
    f = ctx.diff.file(str(path))
    if f is None:
        changed = ", ".join(x.path for x in ctx.diff.files[:50])
        raise ToolError(f"{path!r} is not changed in this diff. Changed files: {changed}")
    for block in blocks:
        header = block.split("\n", 1)[0]
        if header.endswith(f" b/{f.path}") or header.endswith(f' "b/{f.path}"'):
            return truncate(block)
    return truncate(f"{f.status} {f.path} (no textual hunks; binary or mode change)")


def read_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    p = resolve_in_tree(ctx.tree, _str(args, "path"))
    if not p.is_file():
        raise ToolError(f"{args['path']!r} does not exist at head")
    if p.stat().st_size > MAX_FILE_BYTES * 5:
        raise ToolError("file too large to read")
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError("binary file")
    lines = data.decode("utf-8", errors="replace").splitlines()
    start = max(1, _int(args, "start", 1))
    end = _int(args, "end", start + 199)
    end = min(len(lines), end, start + MAX_READ_LINES - 1)
    if start > len(lines):
        return f"{_rel(ctx.tree, p)} has {len(lines)} lines; start={start} is past the end."
    width = len(str(end))
    body = "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))
    return truncate(f"{_rel(ctx.tree, p)} lines {start}-{end} of {len(lines)}:\n{body}")


def _iter_files(tree: Path, glob: str | None) -> list[Path]:
    root = tree.resolve()
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        if glob and not fnmatch.fnmatch(rel.as_posix(), glob):
            continue
        out.append(p)
    return out


def search_repo(ctx: ToolContext, args: dict[str, Any]) -> str:
    pattern = _str(args, "pattern")
    if len(pattern) > 200:
        raise ToolError("pattern too long (max 200 chars)")
    use_regex = bool(args.get("regex", False))
    glob = args.get("path_glob") or None
    max_results = min(max(1, _int(args, "max_results", 50)), 200)
    try:
        rx = re.compile(pattern if use_regex else re.escape(pattern))
    except re.error as e:
        raise ToolError(f"invalid regex: {e}") from e
    hits: list[str] = []
    files_with_hits = 0
    for p in _iter_files(ctx.tree, str(glob) if glob else None):
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = p.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        found = False
        for n, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE_CHARS:
                continue
            if rx.search(line):
                found = True
                if len(hits) < max_results:
                    hits.append(f"{_rel(ctx.tree, p)}:{n}: {line.strip()[:200]}")
        files_with_hits += found
    if not hits:
        return f"no matches for {pattern!r}" + (f" in {glob}" if glob else "")
    more = "" if len(hits) < max_results else f"\n(showing first {max_results} matches)"
    return truncate(
        f"{len(hits)} match(es) in {files_with_hits} file(s):\n" + "\n".join(hits) + more
    )


def git_log(ctx: ToolContext, args: dict[str, Any]) -> str:
    limit = min(max(1, _int(args, "limit", 10)), 50)
    path = args.get("path")
    rev = ctx.head_rev or "HEAD"
    cmd = ["log", f"-n{limit}", "--format=%h %ad %an%n    %s", "--date=short", rev]
    if path:
        resolve_in_tree(ctx.tree, str(path))
        cmd += ["--", str(path)]
    try:
        return truncate(run_git(ctx.repo_root, *cmd)) or "(no history)"
    except GitError as e:
        raise ToolError(str(e)) from e


def git_blame(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = _str(args, "path")
    resolve_in_tree(ctx.tree, path)
    start = max(1, _int(args, "start"))
    end = _int(args, "end", start)
    if end < start or end - start + 1 > MAX_BLAME_LINES:
        raise ToolError(f"line range must be 1-{MAX_BLAME_LINES} lines")
    cmd = ["blame", "--date=short", f"-L{start},{end}"]
    if ctx.head_rev:
        cmd.append(ctx.head_rev)
    try:
        return truncate(run_git(ctx.repo_root, *cmd, "--", path))
    except GitError as e:
        raise ToolError(str(e)) from e


def run_tests(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.sandbox is None or ctx.check_ctx is None:
        raise ToolError("no sandbox is available; tests cannot be run in this review")
    raw = args.get("selectors") or args.get("selector") or []
    selectors = [raw] if isinstance(raw, str) else list(raw)
    if not selectors or len(selectors) > 20:
        raise ToolError("provide 1-20 pytest node ids or test file paths")
    for s in selectors:
        if not isinstance(s, str) or s.startswith("-") or not _SELECTOR_RE.match(s):
            raise ToolError(f"invalid selector {s!r}; use node ids like tests/test_x.py::test_y")
        resolve_in_tree(ctx.tree, s.split("::", 1)[0])
    res = run_pytest(ctx.check_ctx, ctx.tree, selectors)
    if res.timed_out:
        return "tests timed out"
    failures = parse_failures(res.combined)
    head = f"exit code {res.returncode} ({'passed' if res.ok else 'failed'})"
    if failures:
        head += "; failed: " + ", ".join(f.node_id for f in failures[:20])
    return truncate(head + "\n" + res.combined[-6000:])


def run_linter(ctx: ToolContext, args: dict[str, Any]) -> str:
    raw = args.get("paths") or []
    paths = [raw] if isinstance(raw, str) else list(raw)
    if not paths or len(paths) > 50:
        raise ToolError("provide 1-50 repo-relative paths")
    rels = [_rel(ctx.tree, resolve_in_tree(ctx.tree, str(p))) for p in paths]
    base = [ruff_bin(), "check", "--output-format=concise", "--no-cache", "--exit-zero"]
    out = run_static_tool(
        [*base, "--isolated", "--select", ",".join(PITFALL_RULES), "--", *rels], ctx.tree
    ).combined
    text = "watchman pitfall rules:\n" + (out or "no violations")
    if repo_has_ruff_config(ctx.tree):
        repo_out = run_static_tool([*base, "--", *rels], ctx.tree).combined
        text += "\n\nrepo ruff config:\n" + (repo_out or "no violations")
    return truncate(text)


def check_package_exists(ctx: ToolContext, args: dict[str, Any]) -> str:
    name = _str(args, "name")
    eco = str(args.get("ecosystem") or "pypi").lower()
    if eco != "pypi":
        raise ToolError(f"ecosystem {eco!r} is not supported (only 'pypi')")
    info = ctx.index.lookup(name)
    if info.status is Lookup.UNKNOWN:
        return f"{info.name}: UNKNOWN (registry unreachable; do not treat as missing)"
    if info.status is Lookup.MISSING:
        return f"{info.name}: does NOT exist on PyPI"
    age = info.age_days()
    age_s = f"{age:.0f} days" if age is not None else "unknown"
    return (
        f"{info.name}: exists on PyPI; {info.release_count} release(s); first release age "
        f"{age_s}; summary: {info.summary or '-'}"
    )


def read_conventions(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not ctx.conventions:
        return "This repository has no CONVENTIONS.md at the base revision."
    return truncate(ctx.conventions)


# --- registry ----------------------------------------------------------------


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [],
            "additionalProperties": False}  # fmt: skip


TOOLS: dict[str, Tool] = {
    t.spec.name: t
    for t in [
        Tool(ToolSpec(
            name="get_diff",
            description="Show the diff under review, for one changed file or all of it.",
            parameters=_obj({"path": {"type": "string", "description": "changed file"}}),
        ), get_diff),
        Tool(ToolSpec(
            name="read_file",
            description="Read lines of a file at the reviewed revision, with line numbers. "
            f"Max {MAX_READ_LINES} lines per call.",
            parameters=_obj({
                "path": {"type": "string"},
                "start": {"type": "integer", "minimum": 1},
                "end": {"type": "integer", "minimum": 1},
            }, ["path"]),
        ), read_file),
        Tool(ToolSpec(
            name="search_repo",
            description="Search all repository files (at the reviewed revision) for a string "
            "or regex. Use it to find callers, existing helpers, and duplicates.",
            parameters=_obj({
                "pattern": {"type": "string"},
                "regex": {"type": "boolean", "description": "default false (literal)"},
                "path_glob": {"type": "string", "description": "e.g. 'src/**/*.py'"},
                "max_results": {"type": "integer"},
            }, ["pattern"]),
        ), search_repo),
        Tool(ToolSpec(
            name="git_log",
            description="Recent commits, optionally for one path.",
            parameters=_obj({"path": {"type": "string"}, "limit": {"type": "integer"}}),
        ), git_log),
        Tool(ToolSpec(
            name="git_blame",
            description="Blame a line range of a file at the reviewed revision.",
            parameters=_obj({
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            }, ["path", "start"]),
        ), git_blame),
        Tool(ToolSpec(
            name="run_tests",
            description="Run specific pytest node ids or test files in the sandbox "
            "(no network). Use it to check whether a test can fail or whether code works.",
            parameters=_obj({
                "selectors": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            }, ["selectors"]),
        ), run_tests),
        Tool(ToolSpec(
            name="run_linter",
            description="Run ruff on files (all lines, not just changed ones).",
            parameters=_obj({
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
            }, ["paths"]),
        ), run_linter),
        Tool(ToolSpec(
            name="check_package_exists",
            description="Check whether a package exists on the registry (PyPI), with its "
            "age and release count.",
            parameters=_obj({
                "name": {"type": "string"},
                "ecosystem": {"type": "string", "enum": ["pypi"]},
            }, ["name"]),
        ), check_package_exists),
        Tool(ToolSpec(
            name="read_conventions",
            description="Read the repository's CONVENTIONS.md (from the base revision).",
            parameters=_obj({}),
        ), read_conventions),
    ]
}  # fmt: skip


def execute(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run a tool. Returns (output, ok). Never raises for tool-level failures."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"error: unknown tool {name!r}; available: {sorted(TOOLS)}", False
    if "__invalid_json__" in args:
        return "error: tool arguments were not valid JSON", False
    try:
        return tool.fn(ctx, args), True
    except ToolError as e:
        return f"error: {e}", False
