"""Parse declared Python dependencies from project manifests."""

from __future__ import annotations

import configparser
import re
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from watchman_review.pypi import normalize

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

ReadFn = Callable[[str], str | None]

MANIFEST_GLOBS = (
    "pyproject.toml",
    "setup.cfg",
    "requirements*.txt",
    "requirements/*.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
)


def requirement_name(spec: str) -> str | None:
    spec = spec.strip()
    if not spec or spec.startswith(("#", "-", "git+", "http:", "https:", "file:", ".")):
        return None
    m = _REQ_NAME.match(spec)
    return normalize(m.group(1)) if m else None


def _names(specs: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for s in specs:
        if isinstance(s, str) and (n := requirement_name(s)):
            out.add(n)
    return out


def from_pyproject(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    names: set[str] = set()
    project = data.get("project") or {}
    names |= _names(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        names |= _names(group or [])
    for group in (data.get("dependency-groups") or {}).values():
        names |= _names(group or [])
    tool = data.get("tool") or {}
    names |= _names((tool.get("uv") or {}).get("dev-dependencies") or [])
    poetry = tool.get("poetry") or {}
    for key in ("dependencies", "dev-dependencies"):
        names |= {normalize(k) for k in (poetry.get(key) or {}) if k.lower() != "python"}
    for group in (poetry.get("group") or {}).values():
        names |= {normalize(k) for k in ((group or {}).get("dependencies") or {})}
    return names


def from_requirements(text: str) -> set[str]:
    return {n for line in text.splitlines() if (n := requirement_name(line.split(" #")[0]))}


def from_setup_cfg(text: str) -> set[str]:
    cp = configparser.ConfigParser()
    try:
        cp.read_string(text)
    except configparser.Error:
        return set()
    names: set[str] = set()
    if cp.has_option("options", "install_requires"):
        names |= from_requirements(cp.get("options", "install_requires"))
    if cp.has_section("options.extras_require"):
        for _, val in cp.items("options.extras_require"):
            names |= from_requirements(val)
    return names


def from_lock(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    return {normalize(p["name"]) for p in data.get("package") or [] if "name" in p}


def from_pipfile(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    return {normalize(k) for sec in ("packages", "dev-packages") for k in (data.get(sec) or {})}


def parse_manifest(path: str, text: str) -> set[str]:
    name = Path(path).name
    if name == "pyproject.toml":
        return from_pyproject(text)
    if name == "setup.cfg":
        return from_setup_cfg(text)
    if name.endswith(".lock"):
        return from_lock(text)
    if name == "Pipfile":
        return from_pipfile(text)
    if name.endswith(".txt"):
        return from_requirements(text)
    return set()


def is_manifest(path: str) -> bool:
    p = Path(path)
    return any(p.match(g) for g in MANIFEST_GLOBS)


def find_manifests(tree: Path) -> list[str]:
    found: list[str] = []
    for g in MANIFEST_GLOBS:
        found += [p.relative_to(tree).as_posix() for p in tree.glob(g) if p.is_file()]
    return sorted(set(found))


def declared(paths: Iterable[str], read: ReadFn, *, include_locks: bool = True) -> set[str]:
    names: set[str] = set()
    for p in paths:
        if not include_locks and p.endswith(".lock"):
            continue
        text = read(p)
        if text is not None:
            names |= parse_manifest(p, text)
    return names
