"""Source repositories for the OSS eval datasets, and helpers to clone and mine them."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / ".cache" / "repos"
SINCE = "2021-01-01"


@dataclass(frozen=True)
class SourceRepo:
    name: str
    slug: str  # github owner/name
    license: str
    src_prefixes: tuple[str, ...]
    test_prefixes: tuple[str, ...] = ("tests/",)

    @property
    def url(self) -> str:
        return f"https://github.com/{self.slug}.git"

    @property
    def path(self) -> Path:
        return CACHE / self.name


REPOS: dict[str, SourceRepo] = {
    r.name: r
    for r in [
        SourceRepo("more-itertools", "more-itertools/more-itertools", "MIT", ("more_itertools/",)),
        SourceRepo("attrs", "python-attrs/attrs", "MIT", ("src/attr/", "src/attrs/")),
        SourceRepo("click", "pallets/click", "BSD-3-Clause", ("src/click/",)),
        SourceRepo("httpx", "encode/httpx", "BSD-3-Clause", ("httpx/",)),
    ]
}


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {proc.stderr[:400]}")
    return proc.stdout


def ensure_clone(repo: SourceRepo) -> Path:
    if not repo.path.exists():
        repo.path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", repo.url, str(repo.path)], check=True)
    return repo.path


@dataclass
class FileStat:
    path: str
    added: int
    removed: int


@dataclass
class Commit:
    repo: str
    sha: str
    parent: str
    date: str
    subject: str
    body: str
    files: list[FileStat] = field(default_factory=list)

    def src_files(self, r: SourceRepo) -> list[FileStat]:
        return [
            f for f in self.files if f.path.endswith(".py") and f.path.startswith(r.src_prefixes)
        ]

    def test_files(self, r: SourceRepo) -> list[FileStat]:
        return [
            f for f in self.files if f.path.endswith(".py") and f.path.startswith(r.test_prefixes)
        ]


_SKIP_SUBJECT = re.compile(
    r"(?i)\b(revert|bump|release|version|typo|black|ruff|pre-commit|lint|isort|flake8|mypy|"
    r"pyupgrade|deprecat|changelog|readme|docs?\b|ci\b|style|format|merge|vendor|typing)"
)
_FIX_SUBJECT = re.compile(r"(?i)\b(fix(es|ed)?|bug|regression|incorrect|wrong|crash)\b")


def list_commits(r: SourceRepo) -> list[Commit]:
    """Non-merge commits since SINCE, with numstat, oldest first."""
    sep = "\x1e"
    out = git(
        r.path, "log", "--no-merges", f"--since={SINCE}", "--reverse",
        f"--format={sep}%H%x00%P%x00%ad%x00%s%x00%b%x00", "--date=short", "--numstat",
    )  # fmt: skip
    commits: list[Commit] = []
    for chunk in out.split(sep)[1:]:
        head, _, stats = chunk.partition("\x00\n")
        parts = head.split("\x00")
        if len(parts) < 5:
            continue
        sha, parents, date, subject, body = parts[:5]
        if len(parents.split()) != 1:
            continue
        c = Commit(r.name, sha, parents.strip(), date, subject.strip(), body.strip())
        for line in stats.strip().splitlines():
            cols = line.split("\t")
            if len(cols) == 3 and cols[0].isdigit() and cols[1].isdigit():
                c.files.append(FileStat(cols[2], int(cols[0]), int(cols[1])))
        commits.append(c)
    return commits


def is_clean_candidate(c: Commit, r: SourceRepo) -> bool:
    """Small, self-contained source change that isn't described as a bug fix."""
    src = c.src_files(r)
    if not src or len(src) > 3 or len(c.files) > 8:
        return False
    if any("=>" in f.path or "{" in f.path for f in c.files):  # renames
        return False
    added = sum(f.added for f in src)
    churn = sum(f.added + f.removed for f in src)
    return (
        5 <= added <= 120
        and churn <= 200
        and not _SKIP_SUBJECT.search(c.subject)
        and not _FIX_SUBJECT.search(c.subject)
    )


def is_fix_candidate(c: Commit, r: SourceRepo) -> bool:
    """A small bug fix with its own regression test."""
    src = c.src_files(r)
    if not src or len(src) > 2 or len(c.files) > 8 or not c.test_files(r):
        return False
    if any("=>" in f.path or "{" in f.path for f in c.files):
        return False
    churn = sum(f.added + f.removed for f in src)
    return (
        2 <= churn <= 40
        and bool(_FIX_SUBJECT.search(c.subject))
        and not _NOT_A_BUG_FIX.search(c.subject)
    )


# "Fix" commits that aren't behavioral bug fixes (seen while reviewing the first build).
_NOT_A_BUG_FIX = re.compile(
    r"(?i)\b(revert|typos?|docs?|docstrings?|typing|types|annotations?|mypy|lint|ci|review|"
    r"comments?|perf\w*|speed\w*|warnings?|tests?|flaky|build|deprecat\w*|checks|pyright|"
    r"type errors?)\b"
)
