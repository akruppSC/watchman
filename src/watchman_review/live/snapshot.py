"""Snapshots of a worker's working tree.

Live mode reviews what the worker has done so far, never half-written files and never by
reading the files while the worker is editing them. A snapshot is a commit object built with
git plumbing and a throwaway index:

- the worker's real index, refs, HEAD, and working tree are never touched;
- tracked edits, deletions, and untracked files are all captured (`.gitignore` is honored);
- if the tree is identical to HEAD the snapshot *is* HEAD, so nothing new is created.

The commit is not referenced by any ref, so it is garbage-collected by git later.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from watchman_review.gitutil import GitError

# Files Watchman itself installs into the worker's repo. Never part of what is reviewed.
ARTIFACT_EXCLUDES = (".watchman/",)

_IDENTITY = {
    "GIT_AUTHOR_NAME": "watchman",
    "GIT_AUTHOR_EMAIL": "watchman@localhost",
    "GIT_COMMITTER_NAME": "watchman",
    "GIT_COMMITTER_EMAIL": "watchman@localhost",
    # A fixed date makes the snapshot a pure function of (tree, parent): the same working tree
    # always yields the same sha, which is how "nothing changed since the last review" is known.
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
}


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            env=full_env,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)} failed: {e}") from e
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def head_sha(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    except GitError:
        return None


def snapshot_commit(repo: Path, *, message: str = "watchman snapshot") -> str:
    """Return a commit sha capturing the working tree as it is right now."""
    head = head_sha(repo)
    if head is None:
        raise GitError("the repository has no commits yet; make one so there is a base to review")
    with tempfile.TemporaryDirectory(prefix="watchman-index-") as tmp:
        env = {**_IDENTITY, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", "HEAD", env=env)  # start from HEAD so deletions show up
        _git(repo, "add", "-A", "--", ".", env=env)
        # Drop our own files from the throwaway index afterwards. An exclude pathspec on
        # `git add` would make git fail outright when those paths are ignored (they are,
        # via .git/info/exclude, once the integration is installed).
        for artifact in ARTIFACT_EXCLUDES:
            _git(repo, "rm", "-r", "--cached", "-q", "--ignore-unmatch", artifact, env=env)
        tree = _git(repo, "write-tree", env=env)
        if tree == _git(repo, "rev-parse", "HEAD^{tree}"):
            return head
        return _git(repo, "commit-tree", tree, "-p", head, "-m", message, env=env)


def changed_since(repo: Path, base: str, sha: str) -> list[str]:
    """Paths that differ between `base` and the snapshot (for quick 'anything to review?')."""
    out = _git(repo, "diff", "--name-only", f"{base}...{sha}")
    return [line for line in out.splitlines() if line]
