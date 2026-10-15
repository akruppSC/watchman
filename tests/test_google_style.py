from __future__ import annotations

from pathlib import Path

from tests.test_checks import branch_change, make_ctx, rules
from watchman_review.checks.google_style import GoogleStyleCheck, bad_snake, bad_variable
from watchman_review.checks.quality import QualityCheck
from watchman_review.config import Config, StyleGuide, load_config
from watchman_review.review import with_style_preset
from watchman_review.schema import CheckStatus

BASE = {"app/other.py": "X = 1\n"}


def run(repo: Path):
    return GoogleStyleCheck().run(make_ctx(repo))


def head(repo: Path, body: str, name: str = "app/m.py"):
    branch_change(repo, BASE, {name: body})
    return run(repo)


def test_naming_flags_camel_case_but_not_snake_constants_or_dunders(git_repo: Path) -> None:
    body = (
        '"""Doc."""\n\nMAX_SIZE = 3\n\n\n'
        "class my_class:\n    def __init__(self):\n        self.x = 1\n\n\n"
        "def goodName(argOne, arg_two):\n"
        "    camelVar = 1\n    ok_var = 2\n    return camelVar + ok_var\n"
    )
    res = head(git_repo, body)
    assert rules(res) == [
        "google:2.21-annotations",  # `goodName` is a new public function without annotations
        "google:3.16-argument-name",
        "google:3.16-class-name",
        "google:3.16-function-name",
        "google:3.16-variable-name",
    ]
    assert all("§" in f.summary for f in res.findings)  # every finding cites a section


def test_unittest_and_test_names_are_exempt(git_repo: Path) -> None:
    body = (
        "class T:\n    def setUp(self):\n        pass\n\n    def testThing(self):\n        pass\n"
    )
    assert head(git_repo, body, "tests/test_x.py").findings == []


def test_new_public_function_needs_annotations_and_sections(git_repo: Path) -> None:
    body = (
        '"""Mod."""\n\n\n'
        'def load(path, strict):\n    """Load a thing.\n\n    More detail.\n    """\n'
        "    return path\n"
    )
    got = rules(head(git_repo, body))
    assert got == ["google:2.21-annotations", "google:3.8.3-args", "google:3.8.3-returns"]


def test_annotated_documented_function_passes(git_repo: Path) -> None:
    body = (
        '"""Mod."""\n\n\ndef load(path: str) -> str:\n    """Loads a thing.\n\n    Args:\n'
        '        path: Where it is.\n\n    Returns:\n        The path.\n    """\n    return path\n'
    )
    assert head(git_repo, body).findings == []


def test_one_line_docstring_may_omit_sections_but_not_the_period(git_repo: Path) -> None:
    ok = '"""Mod."""\n\n\ndef f(a: int) -> int:\n    """Doubles a."""\n    return a * 2\n'
    assert head(git_repo, ok).findings == []


def test_existing_functions_are_not_blamed(git_repo: Path) -> None:
    old = '"""Mod."""\n\n\ndef load(path):\n    return path\n'
    branch_change(
        git_repo, {"app/m.py": old}, {"app/m.py": old.replace("return path", "return path or 1")}
    )
    assert run(git_repo).findings == []


def test_private_functions_are_exempt_from_annotation_rules(git_repo: Path) -> None:
    assert head(git_repo, '"""Mod."""\n\n\ndef _helper(a):\n    return a\n').findings == []


def test_module_docstring_for_new_modules(git_repo: Path) -> None:
    res = head(git_repo, "def f() -> int:\n    return 1\n")
    assert "google:3.8.2-module-docstring" in rules(res)


def test_relative_import_comprehension_staticmethod_global(git_repo: Path) -> None:
    body = (
        '"""Mod."""\n\nfrom . import sibling\n\n\n'
        "class K:\n    @staticmethod\n    def f() -> int:\n"
        '        """One."""\n        return 1\n\n\ndef g(xs: list[int]) -> list[int]:\n'
        '    """Two."""\n    global counter\n    return [a * b for a in xs for b in xs]\n'
    )
    got = rules(head(git_repo, body))
    assert {"google:2.2-relative-import", "google:2.17-staticmethod", "google:2.5-global",
            "google:2.7-comprehension"} <= set(got)  # fmt: skip


def test_implicit_false_logging_todo_main_and_long_try(git_repo: Path) -> None:
    long_try = "".join(f"        value_{i} = step_{i}()\n" for i in range(32))
    body = (
        '"""Mod."""\n\nimport logging\n\nlogger = logging.getLogger(__name__)\n\n\n'
        "def f(items: list[int], x: object) -> None:\n"
        '    """Does it."""\n'
        "    if len(items) == 0:\n        return\n    if x == None:\n        return\n"
        '    logger.info(f"n={len(items)}")\n'
        "    # TODO fix this\n    # TODO(b/123): later\n"
        f"    try:\n{long_try}    except ValueError:\n        pass\n\n\nmain()\n"
    )
    got = rules(head(git_repo, body))
    assert {
        "google:2.14-len-compare", "google:2.14-none-compare", "google:3.10.1-logging-fstring",
        "google:3.12-todo", "google:2.4-try-body", "google:3.17-main-guard",
    } <= set(got)  # fmt: skip
    assert got.count("google:3.12-todo") == 1  # the linked TODO is fine


def test_a_clean_module_passes_and_repeats_are_capped(git_repo: Path) -> None:
    clean = '"""Mod."""\n\n\ndef ok(a: int) -> int:\n    """Returns a."""\n    return a\n'
    assert head(git_repo, clean).status is CheckStatus.PASSED


def test_repeats_of_one_rule_in_a_file_are_capped(git_repo: Path) -> None:
    many = '"""Mod."""\n' + "".join(f"badName{i} = {i}\n" for i in range(8))
    assert rules(head(git_repo, many)).count("google:3.16-variable-name") == 3


def test_added_file_with_dashes_is_flagged(git_repo: Path) -> None:
    res = head(git_repo, '"""Mod."""\n', "app/my-module.py")
    assert "google:3.16.3-filename" in rules(res)


def test_bad_snake_helper() -> None:
    assert not bad_snake("good_name") and not bad_snake("__init__") and not bad_snake("_private")
    assert bad_snake("badName") and bad_snake("BadName")
    assert bad_variable("camelVar") and not bad_variable("CmdType") and not bad_variable("ok_var")


def test_length_limit_is_40_under_google_and_60_otherwise(git_repo: Path) -> None:
    body = "def f(a):\n" + "".join(f"    a += {i}\n" for i in range(44)) + "    return a\n"
    branch_change(git_repo, {"app/m.py": "X = 1\n"}, {"app/m.py": "X = 1\n\n\n" + body})
    assert QualityCheck().run(make_ctx(git_repo)).findings == []
    cfg = Config()
    cfg.review.style = StyleGuide.GOOGLE
    res = QualityCheck().run(make_ctx(git_repo, config=cfg))
    assert rules(res) == ["quality:length"] and "limit 40" in res.findings[0].summary


def test_style_is_user_config_only_and_adds_the_check_and_preset(tmp_path: Path) -> None:
    cfg, _ = load_config(None, repo_config_text='[review]\nstyle = "google"\n')
    assert cfg.review.style is StyleGuide.NONE  # a reviewed repo can't opt in or out
    text = with_style_preset("Use tabs.")
    assert "Google Python Style Guide" in text and text.index("Use tabs.") > text.index("§3.18")
    assert with_style_preset(None).startswith("# Style conventions")


def test_pascal_case_aliases_and_stub_annotated_modules_are_left_alone(git_repo: Path) -> None:
    body = (
        '"""Mod."""\n\nimport typing\n\nCmdType = typing.TypeVar("CmdType")\n\n\n'
        'def load(path):\n    """Loads."""\n    return path\n'
    )
    branch_change(
        git_repo, BASE, {"app/m.py": body, "app/m.pyi": "def load(path: str) -> str: ...\n"}
    )
    assert run(git_repo).findings == []


def test_dunder_module_names_are_not_misnamed(git_repo: Path) -> None:
    assert head(git_repo, "from app import cli\n\ncli.main()\n", "app/__main__.py").findings == []


def test_long_lines_are_flagged_except_imports_urls_and_single_tokens(git_repo: Path) -> None:
    long = "x = " + " + ".join(["value"] * 20)
    body = (
        '"""Mod."""\n\n'
        f"{long}\n"
        f"# see https://example.com/{'a' * 90}\n"
        f"from some.very.long.package.path.{'m' * 70} import thing  # {'c' * 10}\n"
        f"PATH = '/{'p' * 90}'\n"
    )
    res = head(git_repo, body)
    assert rules(res) == ["google:3.2-line-length"]
    assert res.findings[0].line_start == 3 and "1 added line(s)" in res.findings[0].summary


def test_many_long_lines_make_one_finding_per_file(git_repo: Path) -> None:
    body = '"""Mod."""\n' + "".join(f"x{i} = " + " + ".join(["v"] * 40) + "\n" for i in range(7))
    res = head(git_repo, body)
    assert rules(res) == ["google:3.2-line-length"] and "7 added line(s)" in res.findings[0].summary
