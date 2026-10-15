from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import commit_files, git
from tests.test_checks import FAKE_GH
from watchman_review import hook
from watchman_review.cli import app

typer_testing = pytest.importorskip("typer.testing")
CliRunner = typer_testing.CliRunner


@pytest.fixture
def pushable(git_repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A repo with one commit on main, wired to a bare remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    commit_files(git_repo, {"a.py": "x = 1\n"}, "base")
    git(git_repo, "remote", "add", "origin", str(remote))
    git(git_repo, "push", "-q", "origin", "main")
    git(git_repo, "remote", "set-head", "origin", "main")
    return git_repo, remote


def push(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "push", *args], cwd=repo, capture_output=True, text=True, env=full_env, timeout=240
    )


def install(repo: Path, **kw: object) -> Path:
    # --offline keeps the test off PyPI; the python path is the venv running the tests.
    kwargs: dict[str, object] = {"extra_args": ["--offline"], "python": sys.executable}
    kwargs.update(kw)
    return hook.install(repo, **kwargs)  # type: ignore[arg-type]


def test_install_status_uninstall_roundtrip(git_repo: Path) -> None:
    assert not hook.status(git_repo).installed
    path = install(git_repo)
    assert os.access(path, os.X_OK)
    text = path.read_text()
    assert hook.HOOK_MARKER in text and "--fail-on block" in text
    assert "--checks secrets,imports,hygiene,scope,ruff,mypy" in text  # fast: no pytest
    assert hook.status(git_repo).installed
    assert hook.uninstall(git_repo) == path and not path.exists()
    assert hook.uninstall(git_repo) is None


def test_with_tests_adds_pytest_and_mode_is_passed_through(git_repo: Path) -> None:
    text = install(git_repo, with_tests=True, mode="agent").read_text()
    assert ",pytest" in text and "--mode agent" in text


def test_foreign_hook_is_protected_then_replaced_and_restored(git_repo: Path) -> None:
    path = hook.hook_path(git_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(hook.HookError, match="already exists"):
        install(git_repo)
    install(git_repo, force=True)
    assert hook.HOOK_MARKER in path.read_text()
    hook.uninstall(git_repo)
    assert path.read_text() == "#!/bin/sh\necho mine\n"  # the displaced hook is back
    path.unlink()
    path.write_text("#!/bin/sh\n")
    with pytest.raises(hook.HookError):
        hook.uninstall(git_repo)  # won't delete a hook it didn't write


def test_hook_honors_core_hookspath(git_repo: Path, tmp_path: Path) -> None:
    custom = tmp_path / "myhooks"
    git(git_repo, "config", "core.hooksPath", str(custom))
    assert install(git_repo) == custom / "pre-push"


def test_push_with_a_leaked_secret_is_blocked(pushable: tuple[Path, Path]) -> None:
    repo, remote = pushable
    install(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"cfg.py": f'TOKEN = "{FAKE_GH}"\n'}, "add config")
    res = push(repo, "origin", "feature")
    assert res.returncode != 0
    out = res.stdout + res.stderr  # git forwards a hook's stdout (the report) to the terminal
    assert "push blocked" in res.stderr and "secrets" in out
    assert FAKE_GH not in out  # the value is redacted everywhere
    heads = subprocess.run(["git", "branch", "--list"], cwd=remote, capture_output=True, text=True)
    assert "feature" not in heads.stdout  # nothing reached the remote


def test_clean_push_goes_through_and_reviews_only_new_commits(pushable: tuple[Path, Path]) -> None:
    repo, _ = pushable
    install(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"b.py": "y = 2\n"}, "clean change")
    first = push(repo, "-u", "origin", "feature")
    assert first.returncode == 0, first.stderr
    assert "reviewing refs/heads/feature against origin/main" in first.stderr
    commit_files(repo, {"c.py": "z = 3\n"}, "second change")
    second = push(repo, "origin", "feature")
    assert second.returncode == 0, second.stderr
    assert "reviewing refs/heads/feature against " in second.stderr
    assert "against origin/main" not in second.stderr  # now against the remote branch tip


@pytest.mark.parametrize("how", ["env", "no-verify"])
def test_hook_can_be_bypassed(pushable: tuple[Path, Path], how: str) -> None:
    repo, _ = pushable
    install(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"cfg.py": f'TOKEN = "{FAKE_GH}"\n'}, "leak")
    if how == "env":
        res = push(repo, "origin", "feature", env={"WATCHMAN_SKIP": "1"})
    else:
        res = push(repo, "--no-verify", "origin", "feature")
    assert res.returncode == 0, res.stderr
    assert "watchman" not in res.stderr


def test_a_broken_reviewer_never_blocks_a_push(pushable: tuple[Path, Path]) -> None:
    repo, _ = pushable
    install(repo, python="/nonexistent/python")
    git(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"b.py": "y = 2\n"}, "change")
    res = push(repo, "origin", "feature")
    assert res.returncode == 0
    assert "could not complete" in res.stderr


def test_tags_and_branch_deletes_are_not_reviewed(pushable: tuple[Path, Path]) -> None:
    repo, _ = pushable
    install(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"b.py": "y = 2\n"}, "change")
    assert push(repo, "--no-verify", "origin", "feature").returncode == 0
    git(repo, "tag", "v1")
    tag = push(repo, "origin", "v1")
    assert tag.returncode == 0 and "reviewing" not in tag.stderr
    delete = push(repo, "origin", ":feature")
    assert delete.returncode == 0 and "reviewing" not in delete.stderr


def test_cli_hook_commands(git_repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["hook", "install", "--repo", str(git_repo)])
    assert result.exit_code == 0 and "installed" in result.output and "--no-verify" in result.output
    assert "installed" in runner.invoke(app, ["hook", "status", "--repo", str(git_repo)]).output
    again = runner.invoke(app, ["hook", "install", "--repo", str(git_repo), "--force"])
    assert again.exit_code == 0
    assert "removed" in runner.invoke(app, ["hook", "uninstall", "--repo", str(git_repo)]).output
    assert "absent" in runner.invoke(app, ["hook", "status", "--repo", str(git_repo)]).output
