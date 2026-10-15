from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import commit_files, git
from watchman_review.gitutil import GitError
from watchman_review.live.snapshot import changed_since, head_sha, snapshot_commit


@pytest.fixture
def repo(git_repo: Path) -> Path:
    commit_files(
        git_repo, {"a.py": "x = 1\n", "gone.py": "y = 2\n", ".gitignore": "*.log\n"}, "base"
    )
    return git_repo


def state(repo: Path) -> tuple[str, str, str]:
    """HEAD, the real index (as status sees it), and every ref: all must stay untouched."""
    return (
        git(repo, "rev-parse", "HEAD").strip(),
        git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        git(repo, "for-each-ref"),
    )


def test_clean_tree_snapshot_is_head(repo: Path) -> None:
    assert snapshot_commit(repo) == head_sha(repo)


def test_snapshot_captures_edits_deletions_and_untracked_files(repo: Path) -> None:
    (repo / "a.py").write_text("x = 2\n")
    (repo / "gone.py").unlink()
    (repo / "new.py").write_text("z = 3\n")
    (repo / "debug.log").write_text("ignored\n")
    sha = snapshot_commit(repo)
    assert sha != head_sha(repo)
    assert git(repo, "show", f"{sha}:a.py") == "x = 2\n"
    assert git(repo, "show", f"{sha}:new.py") == "z = 3\n"
    files = git(repo, "ls-tree", "-r", "--name-only", sha).split()
    assert "gone.py" not in files and "debug.log" not in files
    assert sorted(changed_since(repo, "HEAD", sha)) == ["a.py", "gone.py", "new.py"]


def test_snapshot_never_touches_index_refs_or_worktree(repo: Path) -> None:
    (repo / "a.py").write_text("x = 2\n")
    (repo / "staged.py").write_text("s = 1\n")
    git(repo, "add", "staged.py")
    (repo / "new.py").write_text("z = 3\n")
    before = state(repo)
    contents = {p.name: p.read_text() for p in repo.glob("*.py")}
    snapshot_commit(repo)
    assert state(repo) == before
    assert {p.name: p.read_text() for p in repo.glob("*.py")} == contents
    assert git(repo, "diff", "--cached", "--name-only").split() == ["staged.py"]


def test_snapshot_ignores_watchmans_own_state_directory(repo: Path) -> None:
    (repo / ".watchman").mkdir()
    (repo / ".watchman" / "state.json").write_text("{}")
    assert snapshot_commit(repo) == head_sha(repo)


def test_snapshot_needs_a_base_commit(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path / "empty")], check=True)
    with pytest.raises(GitError, match="no commits"):
        snapshot_commit(tmp_path / "empty")


def test_snapshot_is_stable_while_the_worker_keeps_editing(repo: Path) -> None:
    (repo / "a.py").write_text("x = 2\n")
    sha = snapshot_commit(repo)
    (repo / "a.py").write_text("x = 3  # edited after the snapshot\n")
    assert git(repo, "show", f"{sha}:a.py") == "x = 2\n"


def test_snapshot_works_when_watchmans_directory_is_git_ignored(repo: Path) -> None:
    """The installer hides .watchman/ via .git/info/exclude; git add must not choke on it."""
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".watchman/\n.opencode/plugins/watchman.js\n")
    (repo / ".watchman").mkdir()
    (repo / ".watchman" / "state.json").write_text("{}")
    (repo / "a.py").write_text("x = 5\n")
    sha = snapshot_commit(repo)
    assert changed_since(repo, "HEAD", sha) == ["a.py"]


def test_snapshot_drops_watchman_files_even_when_they_are_not_ignored(repo: Path) -> None:
    (repo / ".watchman").mkdir()
    (repo / ".watchman" / "state.json").write_text("{}")
    (repo / "a.py").write_text("x = 5\n")
    assert changed_since(repo, "HEAD", snapshot_commit(repo)) == ["a.py"]


def test_the_same_tree_always_yields_the_same_snapshot(repo: Path) -> None:
    import time

    (repo / "a.py").write_text("x = 2\n")
    first = snapshot_commit(repo)
    time.sleep(1.1)  # a timestamped commit would differ across a second boundary
    assert snapshot_commit(repo) == first
    (repo / "a.py").write_text("x = 3\n")
    assert snapshot_commit(repo) != first
