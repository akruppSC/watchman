from __future__ import annotations

from pathlib import Path

from tests.conftest import commit_files, git
from watchman_review.gitutil import (
    WORKTREE,
    FileStatus,
    get_diff,
    parse_diff,
    rev_parse,
    snapshot,
)

SAMPLE = """\
diff --git a/pkg/mod.py b/pkg/mod.py
index 1111111..2222222 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,4 +1,5 @@
 import os
-import sys
+import json
+import requests
 
 def f():
@@ -10,2 +11,3 @@ def g():
     a = 1
+    b = 2
     return a
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-z = 3
diff --git a/old name.py b/new name.py
similarity index 100%
rename from old name.py
rename to new name.py
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


def test_parse_diff_line_numbers_and_statuses() -> None:
    d = parse_diff(SAMPLE)
    by_path = {f.path: f for f in d.files}
    mod = by_path["pkg/mod.py"]
    assert mod.status is FileStatus.MODIFIED
    assert mod.added == {2: "import json", 3: "import requests", 12: "    b = 2"}
    assert mod.removed == ["import sys"]
    assert by_path["new.py"].status is FileStatus.ADDED
    assert by_path["new.py"].added == {1: "x = 1", 2: "y = 2"}
    assert by_path["gone.py"].status is FileStatus.DELETED
    renamed = by_path["new name.py"]
    assert renamed.status is FileStatus.RENAMED
    assert renamed.old_path == "old name.py"
    assert by_path["img.png"].is_binary
    assert d.lines_added == 5
    assert d.lines_removed == 2
    assert d.touches_line("pkg/mod.py", 12)
    assert not d.touches_line("pkg/mod.py", 5)
    assert d.touches_line("pkg/mod.py", 5, slack=2)


def test_get_diff_uses_merge_base(git_repo: Path) -> None:
    commit_files(git_repo, {"a.py": "x = 1\n"}, "base")
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit_files(git_repo, {"a.py": "x = 1\ny = 2\n"}, "feature work")
    git(git_repo, "checkout", "-q", "main")
    commit_files(git_repo, {"other.py": "z = 1\n"}, "unrelated main work")
    d = get_diff(git_repo, "main", "feature")
    assert [f.path for f in d.files] == ["a.py"]
    assert d.files[0].added == {2: "y = 2"}


def test_worktree_diff_includes_untracked(git_repo: Path) -> None:
    commit_files(git_repo, {"a.py": "x = 1\n"}, "base")
    (git_repo / "a.py").write_text("x = 2\n")
    (git_repo / "fresh.py").write_text("new = True\n")
    d = get_diff(git_repo, "main", WORKTREE)
    paths = {f.path for f in d.files}
    assert paths == {"a.py", "fresh.py"}
    assert d.file("fresh.py") is not None
    assert d.file("fresh.py").added == {1: "new = True"}  # type: ignore[union-attr]


def test_snapshot_is_isolated_and_cleaned_up(git_repo: Path) -> None:
    first = commit_files(git_repo, {"a.py": "v1\n"}, "one")
    commit_files(git_repo, {"a.py": "v2\n"}, "two")
    (git_repo / "a.py").write_text("dirty\n")
    with snapshot(git_repo, first) as tree:
        assert (tree / "a.py").read_text() == "v1\n"
        saved = tree
    assert not saved.exists()
    assert "watchman-snap" not in git(git_repo, "worktree", "list")
    assert rev_parse(git_repo, "HEAD") != first
