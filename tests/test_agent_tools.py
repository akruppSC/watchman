from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from tests.conftest import commit_files, git
from watchman_review.agent.tools import TOOLS, ToolContext, execute
from watchman_review.checks.base import CheckContext
from watchman_review.config import Config
from watchman_review.gitutil import get_diff, rev_parse
from watchman_review.pypi import PackageIndex
from watchman_review.sandbox import SubprocessSandbox


@pytest.fixture
def tctx(git_repo: Path) -> ToolContext:
    commit_files(
        git_repo,
        {
            "pkg/__init__.py": "",
            "pkg/text.py": "def slugify(s):\n    return s.lower().replace(' ', '-')\n",
            "tests/test_text.py": (
                "from pkg.text import slugify\n\n\ndef test_slugify():\n"
                "    assert slugify('A B') == 'a-b'\n"
            ),
        },
        "base",
    )
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit_files(
        git_repo,
        {"pkg/util.py": "def to_slug(text):\n    return text.lower().replace(' ', '-')\n"},
        "add to_slug",
    )
    diff = get_diff(git_repo, "main", "feature")
    cfg = Config()
    sandbox = SubprocessSandbox()
    check_ctx = CheckContext(
        repo_root=git_repo, tree=git_repo, diff=diff, config=cfg, sandbox=sandbox
    )

    def pypi(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    index = PackageIndex(http=httpx.Client(transport=httpx.MockTransport(pypi)))
    return ToolContext(
        repo_root=git_repo, tree=git_repo, diff=diff, config=cfg,
        head_rev=rev_parse(git_repo, "feature"), sandbox=sandbox, index=index,
        conventions="Use snake_case.", task="Add a slug helper", check_ctx=check_ctx,
    )  # fmt: skip


def run(tctx: ToolContext, tool: str, **args: object) -> tuple[str, bool]:
    return execute(tctx, tool, dict(args))


def test_all_tools_have_object_schemas() -> None:
    for name, tool in TOOLS.items():
        assert tool.spec.name == name
        assert tool.spec.parameters["type"] == "object"


def test_read_file_numbers_lines(tctx: ToolContext) -> None:
    out, ok = run(tctx, "read_file", path="pkg/text.py", start=2, end=2)
    assert ok
    assert "2|     return s.lower()" in out
    assert "lines 2-2 of 2" in out


@pytest.mark.parametrize(
    "path", ["../outside.txt", "/etc/passwd", ".git/config", "pkg/../../x", ""]
)
def test_read_file_jail(tctx: ToolContext, path: str) -> None:
    out, ok = run(tctx, "read_file", path=path)
    assert not ok
    assert out.startswith("error:")


def test_read_file_rejects_symlink_escape(tctx: ToolContext, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    os.symlink(secret, tctx.tree / "link.txt")
    out, ok = run(tctx, "read_file", path="link.txt")
    assert not ok
    assert "escapes" in out
    assert "top secret" not in out


def test_search_repo_finds_existing_helper(tctx: ToolContext) -> None:
    out, ok = run(tctx, "search_repo", pattern=".lower().replace(' ', '-')")
    assert ok
    assert "pkg/text.py:2" in out
    assert "pkg/util.py:2" in out


def test_search_repo_regex_and_glob(tctx: ToolContext) -> None:
    out, ok = run(tctx, "search_repo", pattern=r"def \w*slug", regex=True, path_glob="pkg/*.py")
    assert ok
    assert "2 match(es)" in out
    assert "tests/" not in out


def test_search_repo_skips_git_dir(tctx: ToolContext) -> None:
    out, _ = run(tctx, "search_repo", pattern="refs/heads")
    assert ".git/" not in out


def test_search_repo_bad_regex(tctx: ToolContext) -> None:
    out, ok = run(tctx, "search_repo", pattern="(unclosed", regex=True)
    assert not ok
    assert "invalid regex" in out


def test_get_diff_single_file_and_unchanged(tctx: ToolContext) -> None:
    out, ok = run(tctx, "get_diff", path="pkg/util.py")
    assert ok
    assert "+def to_slug(text):" in out
    out, ok = run(tctx, "get_diff", path="pkg/text.py")
    assert not ok
    assert "not changed" in out


def test_git_log_and_blame(tctx: ToolContext) -> None:
    log, ok = run(tctx, "git_log", path="pkg/util.py")
    assert ok and "add to_slug" in log
    blame, ok = run(tctx, "git_blame", path="pkg/text.py", start=1, end=2)
    assert ok and "def slugify" in blame
    _, ok = run(tctx, "git_blame", path="pkg/text.py", start=1, end=500)
    assert not ok


def test_run_tests_selector(tctx: ToolContext) -> None:
    out, ok = run(tctx, "run_tests", selectors=["tests/test_text.py::test_slugify"])
    assert ok
    assert "exit code 0 (passed)" in out


@pytest.mark.parametrize(
    "selectors", [["--collect-only"], ["-p", "evil"], ["tests/../../x.py"], [], ["a; rm -rf /"]]
)
def test_run_tests_rejects_option_injection(tctx: ToolContext, selectors: list[str]) -> None:
    out, ok = run(tctx, "run_tests", selectors=selectors)
    assert not ok
    assert out.startswith("error:")


def test_run_linter(tctx: ToolContext) -> None:
    (tctx.tree / "pkg" / "bad.py").write_text("try:\n    x = 1\nexcept:\n    pass\n")
    out, ok = run(tctx, "run_linter", paths=["pkg/bad.py"])
    assert ok
    assert "E722" in out


def test_check_package_exists_missing(tctx: ToolContext) -> None:
    out, ok = run(tctx, "check_package_exists", name="nope-pkg")
    assert ok
    assert "does NOT exist" in out
    out, ok = run(tctx, "check_package_exists", name="x", ecosystem="npm")
    assert not ok


def test_read_conventions(tctx: ToolContext) -> None:
    assert run(tctx, "read_conventions") == ("Use snake_case.", True)


def test_unknown_tool_and_invalid_json(tctx: ToolContext) -> None:
    out, ok = run(tctx, "rm_rf")
    assert not ok and "unknown tool" in out
    out, ok = execute(tctx, "read_file", {"__invalid_json__": "{"})
    assert not ok and "not valid JSON" in out


def test_tools_without_sandbox(tctx: ToolContext) -> None:
    tctx.sandbox = None
    out, ok = run(tctx, "run_tests", selectors=["tests/test_text.py"])
    assert not ok and "no sandbox" in out
