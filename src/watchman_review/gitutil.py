"""Git access: diffs, file contents, and snapshot worktrees.

All git invocations use argument lists (never a shell). The review always runs against a
*snapshot* of `head` (a detached worktree), never against a live, half-edited tree,
unless the caller explicitly asks for the working tree.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

WORKTREE = "WORKTREE"  # sentinel head meaning "the current working tree, uncommitted changes"


class GitError(Exception):
    pass


def run_git(repo: Path, *args: str, check: bool = True, timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)} failed: {e}") from e
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def repo_root(path: Path) -> Path:
    return Path(run_git(path, "rev-parse", "--show-toplevel").strip())


def rev_parse(repo: Path, rev: str) -> str:
    return run_git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


class FileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: FileStatus
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    added: dict[int, str] = field(default_factory=dict)  # new-file line no -> text
    removed: list[str] = field(default_factory=list)

    @property
    def is_python(self) -> bool:
        return self.path.endswith((".py", ".pyi"))

    def changed_line_numbers(self) -> set[int]:
        return set(self.added)


@dataclass
class Diff:
    base: str
    head: str
    files: list[FileDiff]
    raw: str
    merge_base: str = ""  # resolved commit the diff starts from

    @property
    def lines_added(self) -> int:
        return sum(len(f.added) for f in self.files)

    @property
    def lines_removed(self) -> int:
        return sum(len(f.removed) for f in self.files)

    def file(self, path: str) -> FileDiff | None:
        return next((f for f in self.files if f.path == path), None)

    def touches_line(self, path: str, line: int, slack: int = 0) -> bool:
        f = self.file(path)
        if f is None:
            return False
        return any(abs(line - n) <= slack for n in f.added)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_GIT_RE = re.compile(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$')


def _unquote(p: str) -> str:
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        p = p.encode("latin-1", "replace").decode("utf-8", "replace")
    return p


def _strip_prefix(p: str) -> str | None:
    p = _unquote(p.split("\t", 1)[0])
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(raw: str, base: str = "", head: str = "") -> Diff:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    new_line = 0

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            a, b = (m.group(1), m.group(2)) if m else ("", "")
            cur = FileDiff(path=b, old_path=a, status=FileStatus.MODIFIED)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                cur.status = FileStatus.ADDED
                cur.old_path = None
            elif line.startswith("deleted file mode"):
                cur.status = FileStatus.DELETED
            elif line.startswith("rename from "):
                cur.status = FileStatus.RENAMED
                cur.old_path = line[len("rename from ") :]
            elif line.startswith("rename to "):
                cur.path = line[len("rename to ") :]
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur.is_binary = True
            elif line.startswith("--- "):
                old = _strip_prefix(line[4:])
                if old is None:
                    cur.status = FileStatus.ADDED
                cur.old_path = old if cur.status is not FileStatus.ADDED else None
                continue
            elif line.startswith("+++ "):
                new = _strip_prefix(line[4:])
                if new is None:
                    cur.status = FileStatus.DELETED
                    cur.path = cur.old_path or cur.path
                else:
                    cur.path = new
                continue
        m = _HUNK_RE.match(line)
        if m:
            hunk = Hunk(
                old_start=int(m.group(1)),
                old_len=int(m.group(2) or 1),
                new_start=int(m.group(3)),
                new_len=int(m.group(4) or 1),
                header=m.group(5).strip(),
            )
            cur.hunks.append(hunk)
            new_line = hunk.new_start
            continue
        if hunk is None:
            continue
        hunk.lines.append(line)
        if line.startswith("+"):
            cur.added[new_line] = line[1:]
            new_line += 1
        elif line.startswith("-"):
            cur.removed.append(line[1:])
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            new_line += 1
    return Diff(base=base, head=head, files=files, raw=raw)


def get_diff(repo: Path, base: str, head: str, *, context: int = 3) -> Diff:
    """Diff from merge-base(base, head) to head, like a PR. head=WORKTREE includes edits."""
    common = ["diff", "--no-color", "--no-ext-diff", "-M", f"--unified={context}"]
    merge_base = run_git(repo, "merge-base", base, "HEAD" if head == WORKTREE else head).strip()
    if head == WORKTREE:
        raw = run_git(repo, *common, merge_base)
        # Include untracked files so brand-new modules are reviewed too.
        untracked = run_git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        for path in filter(None, untracked.split("\0")):
            raw += run_git(
                repo, "diff", "--no-color", "--no-index", "--", "/dev/null", path, check=False
            )
    else:
        raw = run_git(repo, *common, merge_base, head)
    d = parse_diff(raw, base=base, head=head)
    d.merge_base = merge_base
    return d


def changed_paths_since(repo: Path, base: str, head: str) -> list[str]:
    return [f.path for f in get_diff(repo, base, head, context=0).files]


def git_log(repo: Path, rev: str, path: str | None = None, limit: int = 10) -> str:
    args = ["log", f"-n{limit}", "--format=%h %ad %an%n    %s", "--date=short", rev]
    if path:
        args += ["--", path]
    return run_git(repo, *args)


def git_blame(repo: Path, rev: str, path: str, start: int, end: int) -> str:
    return run_git(repo, "blame", "-s", f"-L{start},{end}", rev, "--", path)


@contextmanager
def snapshot(repo: Path, head: str) -> Iterator[Path]:
    """Yield a directory containing `head`. WORKTREE yields the repo itself."""
    if head == WORKTREE:
        yield repo
        return
    sha = rev_parse(repo, head)
    tmp = Path(tempfile.mkdtemp(prefix="watchman-snap-"))
    target = tmp / "tree"
    run_git(repo, "worktree", "add", "--detach", "--quiet", str(target), sha)
    try:
        yield target
    finally:
        run_git(repo, "worktree", "remove", "--force", str(target), check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        run_git(repo, "worktree", "prune", check=False)
