"""Install or remove Watchman's OpenCode integration in a repository.

Everything Watchman adds to the worker's repo is hidden from git through `.git/info/exclude`
(a local, uncommitted ignore file), so the worker's `git add -A` never commits our plugin and
the reviewer never sees our own files as part of the change.
"""

from __future__ import annotations

import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from watchman_review.config import LivePolicy
from watchman_review.gitutil import run_git

PLUGIN_REL = Path(".opencode") / "plugins" / "watchman.js"
CONFIG_REL = Path("opencode.json")
EXCLUDES = [".opencode/plugins/watchman.js", ".watchman/"]
MCP_NAME = "watchman"


def _exclude_file(repo: Path) -> Path:
    raw = run_git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    p = Path(raw)
    return p if p.is_absolute() else repo / p


def _add_excludes(repo: Path, entries: list[str]) -> None:
    path = _exclude_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    have = path.read_text().splitlines() if path.exists() else []
    missing = [e for e in entries if e not in have]
    if missing:
        path.write_text("\n".join([*have, *missing]) + "\n")


def _remove_excludes(repo: Path, entries: list[str]) -> None:
    path = _exclude_file(repo)
    if path.exists():
        keep = [line for line in path.read_text().splitlines() if line not in entries]
        path.write_text("\n".join(keep) + ("\n" if keep else ""))


def watchman_command(python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "watchman_review"]


def render_plugin(
    repo: Path,
    *,
    policy: LivePolicy,
    round_cap: int,
    done: bool = True,
    python: str | None = None,
    timeout_s: int = 900,
) -> str:
    config: dict[str, Any] = {
        "cmd": watchman_command(python),
        "repo": str(repo),
        "policy": policy.value,
        "round_cap": round_cap,
        "done": done,
        "timeoutMs": timeout_s * 1000,
    }
    template = (files("watchman_review.live") / "opencode_plugin.js").read_text()
    return template.replace("__CONFIG__", json.dumps(config, indent=2))


def install(
    repo: Path,
    *,
    policy: LivePolicy = LivePolicy.ADVISORY,
    round_cap: int = 3,
    done: bool = True,
    mcp: bool = False,
    python: str | None = None,
) -> list[Path]:
    written: list[Path] = []
    plugin = repo / PLUGIN_REL
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(
        render_plugin(repo, policy=policy, round_cap=round_cap, done=done, python=python)
    )
    written.append(plugin)
    excludes = list(EXCLUDES)
    if mcp:
        written.append(_set_mcp(repo, python))
        excludes.append(CONFIG_REL.as_posix())
    _add_excludes(repo, excludes)
    return written


def _set_mcp(repo: Path, python: str | None) -> Path:
    path = repo / CONFIG_REL
    data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault("$schema", "https://opencode.ai/config.json")
    data.setdefault("mcp", {})[MCP_NAME] = {
        "type": "local",
        "command": [*watchman_command(python), "live", "mcp", "--repo", str(repo)],
        "enabled": True,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def uninstall(repo: Path) -> list[Path]:
    removed: list[Path] = []
    plugin = repo / PLUGIN_REL
    if plugin.exists():
        plugin.unlink()
        removed.append(plugin)
    cfg = repo / CONFIG_REL
    if cfg.exists():
        data = json.loads(cfg.read_text())
        if MCP_NAME in data.get("mcp", {}):
            del data["mcp"][MCP_NAME]
            if not data["mcp"]:
                del data["mcp"]
            if set(data) <= {"$schema"}:
                cfg.unlink()
            else:
                cfg.write_text(json.dumps(data, indent=2) + "\n")
            removed.append(cfg)
    _remove_excludes(repo, [*EXCLUDES, CONFIG_REL.as_posix()])
    return removed
