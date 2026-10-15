from __future__ import annotations

from pathlib import Path

from tests.test_checks import branch_change, make_ctx, rules
from watchman_review.checks.quality import QualityCheck
from watchman_review.schema import Category, CheckStatus, Severity

TANGLED = "def route(cmd, a):\n" + "".join(
    f"    {'if' if i == 0 else 'elif'} cmd == {i}:\n        return a + {i}\n" for i in range(12)
)
CALM = "def route(cmd, a):\n    return a\n"


def run(repo: Path):
    return QualityCheck().run(make_ctx(repo))


def test_new_function_over_complexity_limit_is_flagged(git_repo: Path) -> None:
    branch_change(git_repo, {"app/m.py": "X = 1\n"}, {"app/m.py": "X = 1\n\n\n" + TANGLED})
    res = run(git_repo)
    assert res.status is CheckStatus.FAILED
    assert rules(res) == ["quality:complexity"]
    f = res.findings[0]
    assert f.category is Category.MAINTAINABILITY and f.severity is Severity.MINOR
    assert "is new" in f.evidence and f.suggested_fix and "lookup table" in f.suggested_fix


def test_existing_debt_is_not_blamed_on_a_change_that_doesnt_worsen_it(git_repo: Path) -> None:
    before = TANGLED
    after = TANGLED.replace("return a + 3", "return a + 30")  # touched, same complexity
    branch_change(git_repo, {"app/m.py": before}, {"app/m.py": after})
    assert run(git_repo).findings == []


def test_pushing_a_function_across_the_limit_is_flagged(git_repo: Path) -> None:
    base = "def route(cmd, a):\n" + "".join(
        f"    {'if' if i == 0 else 'elif'} cmd == {i}:\n        return a + {i}\n" for i in range(9)
    )
    after = base + "    elif cmd == 99:\n        return a\n"
    branch_change(git_repo, {"app/m.py": base}, {"app/m.py": after})
    f = run(git_repo).findings
    assert [x.rule for x in f] == ["quality:complexity"] and "complexity 11" in f[0].summary


def test_adding_to_an_already_too_complex_function_is_not_flagged(git_repo: Path) -> None:
    after = TANGLED + "    elif cmd == 99:\n        return a\n"
    branch_change(git_repo, {"app/m.py": TANGLED}, {"app/m.py": after})
    assert run(git_repo).findings == []


def test_stub_files_are_ignored(git_repo: Path) -> None:
    stub = "def wide(a, b, c, d, e, f, g): ...\n"
    branch_change(git_repo, {"app/m.pyi": "X: int\n"}, {"app/m.pyi": stub})
    assert run(git_repo).findings == []


def test_calm_code_and_test_files_pass(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"app/m.py": "X = 1\n"},
        {"app/m.py": "X = 1\n\n\n" + CALM, "tests/test_m.py": TANGLED},  # tests are exempt
    )
    res = run(git_repo)
    assert res.status is CheckStatus.PASSED and not res.findings


def test_too_many_parameters_and_deep_nesting(git_repo: Path) -> None:
    nested = (
        "def deep(a):\n    if a:\n        for i in a:\n            while i:\n"
        "                if i > 1:\n                    with a:\n                        return i\n"
    )
    wide = "def wide(a, b, c, d, e, f):\n    return a\n"
    branch_change(git_repo, {"app/m.py": "X = 1\n"}, {"app/m.py": f"X = 1\n\n\n{nested}\n\n{wide}"})
    assert rules(run(git_repo)) == ["quality:nesting", "quality:params"]


def test_docstring_is_required_only_where_the_module_documents_its_api(git_repo: Path) -> None:
    documented = "".join(f'def f{i}():\n    """Doc."""\n    return {i}\n\n\n' for i in range(4))
    branch_change(
        git_repo,
        {
            "app/m.py": documented,
            "app/n.py": "".join(f"def g{i}():\n    return {i}\n\n\n" for i in range(4)),
        },
        {
            "app/m.py": documented + "def new_one():\n    return 1\n",
            "app/n.py": "".join(f"def g{i}():\n    return {i}\n\n\n" for i in range(4))
            + "def new_two():\n    return 2\n",
        },
    )
    res = run(git_repo)
    assert rules(res) == ["quality:docstring"]
    assert res.findings[0].file == "app/m.py"  # n.py never documented its API: no convention


def test_copied_block_is_flagged_as_duplication(git_repo: Path) -> None:
    block = "".join(f"    value_{i} = compute({i}) + offset_{i}\n" for i in range(7))
    branch_change(
        git_repo,
        {"app/a.py": f"def a():\n{block}    return 1\n"},
        {"app/b.py": f"def b():\n{block}    return 2\n"},
    )
    res = run(git_repo)
    assert rules(res) == ["quality:duplicate"]
    assert res.findings[0].category is Category.DUPLICATION
    assert "app/a.py" in res.findings[0].summary


def test_boilerplate_windows_are_not_duplicates(git_repo: Path) -> None:
    imports = "".join(f"import mod_{i}\n" for i in range(8))
    branch_change(git_repo, {"app/a.py": imports}, {"app/b.py": imports})
    assert run(git_repo).findings == []


def test_a_new_undocumented_module_gets_one_api_docs_finding(git_repo: Path) -> None:
    body = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(4))
    branch_change(git_repo, {"app/other.py": "X = 1\n"}, {"app/new.py": body})
    res = run(git_repo)
    assert rules(res) == ["quality:api-docs"]
    assert "4 of 4" in res.findings[0].summary and "docstring" in res.findings[0].suggested_fix


def test_a_documented_or_tiny_new_module_is_left_alone(git_repo: Path) -> None:
    doc = "".join(f'def f{i}():\n    """Doc."""\n    return {i}\n\n\n' for i in range(4))
    tiny = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    branch_change(git_repo, {"app/other.py": "X = 1\n"}, {"app/doc.py": doc, "app/tiny.py": tiny})
    assert run(git_repo).findings == []


def test_public_methods_count_toward_the_api_docs_rule(git_repo: Path) -> None:
    body = (
        "class Store:\n    def a(self):\n        return 1\n\n    def b(self):\n        return 2\n\n"
    )
    branch_change(git_repo, {"app/other.py": "X = 1\n"}, {"app/store.py": body})
    res = run(git_repo)
    assert rules(res) == ["quality:api-docs"]  # Store, Store.a, Store.b are all undocumented


def test_private_and_local_helpers_are_not_public_api(git_repo: Path) -> None:
    body = (
        "def _a():\n    return 1\n\n\ndef _b():\n    return 2\n\n\n"
        "def only_public():\n    def inner():\n        return 1\n\n    return inner()\n"
    )
    branch_change(git_repo, {"app/other.py": "X = 1\n"}, {"app/p.py": body})
    assert run(git_repo).findings == []
