from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import commit_files, git
from watchman_review.cli import app
from watchman_review.config import LivePolicy
from watchman_review.live import opencode
from watchman_review.live.snapshot import changed_since, snapshot_commit


@pytest.fixture
def repo(git_repo: Path) -> Path:
    commit_files(git_repo, {"a.py": "x = 1\n"}, "base")
    return git_repo


def baked_config(plugin: Path) -> dict[str, object]:
    text = plugin.read_text()
    match = re.search(r"const CONFIG = (\{.*?\n\})\n", text, re.S)
    assert match, "plugin has no baked CONFIG"
    return json.loads(match.group(1))  # type: ignore[no-any-return]


def test_install_bakes_the_policy_and_the_command_into_the_plugin(repo: Path) -> None:
    [plugin] = opencode.install(repo, policy=LivePolicy.BLOCKING, round_cap=2, done=False)
    cfg = baked_config(plugin)
    assert cfg["policy"] == "blocking" and cfg["round_cap"] == 2 and cfg["done"] is False
    assert cfg["cmd"] == [sys.executable, "-m", "watchman_review"]
    assert cfg["repo"] == str(repo)


def test_installed_files_are_invisible_to_git_and_to_the_reviewer(repo: Path) -> None:
    opencode.install(repo, mcp=True)
    (repo / ".watchman").mkdir()
    (repo / ".watchman" / "state.json").write_text("{}")
    assert git(repo, "status", "--porcelain") == ""
    git(repo, "add", "-A")  # what a worker does
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert changed_since(repo, "HEAD", snapshot_commit(repo)) == []


def test_mcp_registration_merges_into_an_existing_opencode_config(repo: Path) -> None:
    existing = {
        "$schema": "x",
        "model": "some/model",
        "mcp": {"other": {"type": "remote", "url": "u"}},
    }
    (repo / "opencode.json").write_text(json.dumps(existing))
    opencode.install(repo, mcp=True)
    data = json.loads((repo / "opencode.json").read_text())
    assert data["model"] == "some/model" and data["mcp"]["other"] == existing["mcp"]["other"]
    entry = data["mcp"]["watchman"]
    assert entry["type"] == "local" and entry["enabled"] is True
    assert entry["command"][-4:] == ["live", "mcp", "--repo", str(repo)]
    opencode.uninstall(repo)
    after = json.loads((repo / "opencode.json").read_text())
    assert after == existing  # only our entry was removed


def test_uninstall_removes_everything_it_added(repo: Path) -> None:
    opencode.install(repo, mcp=True)
    removed = opencode.uninstall(repo)
    assert {p.name for p in removed} == {"watchman.js", "opencode.json"}
    assert not (repo / ".opencode" / "plugins" / "watchman.js").exists()
    assert not (repo / "opencode.json").exists()  # we created it, so we delete it
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert "watchman" not in exclude
    assert opencode.uninstall(repo) == []  # idempotent


def test_install_is_idempotent_and_does_not_duplicate_excludes(repo: Path) -> None:
    opencode.install(repo)
    opencode.install(repo, policy=LivePolicy.BLOCKING)
    lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert len(lines) == len(set(lines))
    assert baked_config(repo / ".opencode/plugins/watchman.js")["policy"] == "blocking"


def test_cli_install_status_reset_and_uninstall(repo: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        app, ["live", "install-opencode", "--repo", str(repo), "--policy", "blocking"]
    )
    assert res.exit_code == 0 and "watchman.js" in res.output
    (repo / "a.py").write_text("x = 2\n")
    start = runner.invoke(
        app, ["live", "start", "--repo", str(repo), "--session", "s", "--task", "T"]
    )
    assert start.exit_code == 0 and "session s: base" in start.output
    status = runner.invoke(app, ["live", "status", "--repo", str(repo), "--session", "s"])
    assert "task: T" in status.output and "0 review(s)" in status.output
    assert (
        runner.invoke(app, ["live", "reset", "--repo", str(repo), "--session", "s"]).exit_code == 0
    )
    gone = runner.invoke(app, ["live", "uninstall-opencode", "--repo", str(repo)])
    assert "removed" in gone.output and not (repo / ".opencode/plugins/watchman.js").exists()


def test_live_check_json_includes_the_rendered_text(repo: Path) -> None:
    # Nothing changed since the base, so this is free and needs no reviewer.
    res = CliRunner().invoke(
        app, ["live", "check", "--kind", "done", "--json", "--repo", str(repo), "--session", "s"]
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["status"] == "nothing" and payload["text"].startswith("WATCHMAN done: nothing")
