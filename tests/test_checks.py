from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.conftest import commit_files, git
from watchman_review.checks import REGISTRY, run_checks
from watchman_review.checks.base import Check, CheckContext, run_check
from watchman_review.checks.hygiene import HygieneCheck
from watchman_review.checks.imports import ImportsCheck, added_imports
from watchman_review.checks.mypy_check import MypyCheck
from watchman_review.checks.ruff_check import RuffCheck
from watchman_review.checks.scope import ScopeCheck
from watchman_review.checks.secrets import SecretsCheck, redact, scan_line
from watchman_review.checks.tests_check import TestsCheck
from watchman_review.config import Config
from watchman_review.gitutil import get_diff
from watchman_review.pypi import PackageIndex
from watchman_review.sandbox import Sandbox, SubprocessSandbox
from watchman_review.schema import Category, CheckResult, CheckStatus, Severity


def make_ctx(
    repo: Path,
    base: str = "main",
    head: str = "HEAD",
    *,
    config: Config | None = None,
    sandbox: Sandbox | None = None,
) -> CheckContext:
    return CheckContext(
        repo_root=repo,
        tree=repo,
        diff=get_diff(repo, base, head),
        config=config or Config(),
        sandbox=sandbox,
    )


def branch_change(repo: Path, base_files: dict[str, str], head_files: dict[str, str]) -> None:
    commit_files(repo, base_files, "base")
    git(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, head_files, "change")


def rules(res: CheckResult) -> list[str]:
    return sorted(f.rule or "" for f in res.findings)


# --- secrets ---------------------------------------------------------------

# Built at runtime so this repo never contains a literal that looks like a real key.
FAKE_AWS = "AKIA" + "Q" * 16
FAKE_GH = "ghp_" + "a1B2" * 9


@pytest.mark.parametrize(
    ("line", "rule_id"),
    [
        (f'key = "{FAKE_AWS}"', "aws-access-key"),
        (f"TOKEN={FAKE_GH}", "github-token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private-key"),
        ('db_password = "Zx9!qL2#vR7$mK4@"', "generic-secret"),
        ("url = 'postgres://admin:hunter2pass@db:5432/x'", "url-credentials"),
    ],
)
def test_secret_rules_match(line: str, rule_id: str) -> None:
    assert [r.id for r, _ in scan_line(line)] == [rule_id]


@pytest.mark.parametrize(
    "line",
    [
        'password = "changeme-placeholder"',
        'api_key = os.environ["API_KEY"]',
        'token = "aaaaaaaaaaaaaaaa"',  # low entropy
        "password = get_password()",
    ],
)
def test_secret_rules_ignore_benign(line: str) -> None:
    assert scan_line(line) == []


def test_secrets_check_redacts_and_only_scans_added_lines(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"old.py": f'OLD = "{FAKE_AWS}"\n'},
        {"cfg.py": f'GH = "{FAKE_GH}"\n'},
    )
    res = run_check(SecretsCheck(), make_ctx(git_repo))
    assert res.status is CheckStatus.FAILED
    assert [(f.file, f.line_start) for f in res.findings] == [("cfg.py", 1)]
    assert FAKE_GH not in res.findings[0].evidence
    assert res.findings[0].severity is Severity.BLOCKER
    assert res.findings[0].evidence_refs == ["check:secrets"]


def test_redact_keeps_prefix_only() -> None:
    assert redact("abcdefghijkl") == "abcd…[12 chars redacted]"


# --- imports ---------------------------------------------------------------


def fake_pypi(existing: dict[str, dict[str, object]]) -> PackageIndex:
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.split("/")[2]
        if name in existing:
            return httpx.Response(200, json=existing[name])
        return httpx.Response(404)

    return PackageIndex(http=httpx.Client(transport=httpx.MockTransport(handler)))


MATURE = {
    "info": {"summary": "x"},
    "releases": {
        "1.0": [{"upload_time_iso_8601": "2015-01-01T00:00:00Z"}],
        "2.0": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z"}],
    },
}
FRESH = {"info": {}, "releases": {"0.0.1": [{"upload_time_iso_8601": "2099-01-01T00:00:00Z"}]}}


def test_added_imports_only_on_changed_lines_and_detects_guard() -> None:
    src = "import os\ntry:\n    import ujson\nexcept ImportError:\n    ujson = None\nimport a.b\n"
    found = added_imports(src, "m.py", {3, 6})
    assert [(i.module, i.line, i.guarded) for i in found] == [("ujson", 3, True), ("a", 6, False)]


def test_imports_check_classifies_each_import(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {
            "pyproject.toml": '[project]\nname="x"\ndependencies=["httpx>=0.27", "PyYAML"]\n',
            "localpkg/__init__.py": "",
            "app.py": "import os\n",
        },
        {
            "app.py": (
                "import os\nimport json\nimport httpx\nimport yaml\nimport localpkg\n"
                "import requests\nimport fastjsonx_utils\nimport shinynewpkg\n"
            )
        },
    )
    index = fake_pypi({"requests": MATURE, "shinynewpkg": FRESH})
    res = run_check(ImportsCheck(index), make_ctx(git_repo))
    by_rule = {(f.rule, f.line_start) for f in res.findings}
    assert by_rule == {
        ("imports:undeclared", 6),  # requests: real but undeclared
        ("imports:missing", 7),  # hallucinated
        ("imports:suspicious-package", 8),
        ("imports:undeclared", 8),
    }
    missing = next(f for f in res.findings if f.rule == "imports:missing")
    assert missing.category is Category.HALLUCINATED_DEPENDENCY
    assert missing.severity is Severity.BLOCKER


def test_undeclared_test_tooling_is_not_reported_but_hallucinations_are(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"pyproject.toml": '[project]\nname="x"\ndependencies=[]\n', "tests/__init__.py": ""},
        {"tests/test_a.py": "import pytest\nimport pytest_magicfixtures_xyz\n"},
    )
    res = run_check(ImportsCheck(fake_pypi({"pytest": MATURE})), make_ctx(git_repo))
    assert [(f.rule, f.line_start) for f in res.findings] == [("imports:missing", 2)]


def test_imports_check_new_manifest_entry_missing_on_pypi(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"requirements.txt": "requests==2.0\n"},
        {"requirements.txt": "requests==2.0\nreqeusts-helper==1.0\n"},
    )
    res = run_check(ImportsCheck(fake_pypi({"requests": MATURE})), make_ctx(git_repo))
    assert [(f.rule, f.file, f.line_start) for f in res.findings] == [
        ("imports:manifest-missing", "requirements.txt", 2)
    ]


def test_imports_check_unknown_network_produces_no_findings(git_repo: Path) -> None:
    branch_change(git_repo, {"a.py": ""}, {"a.py": "import mystery\n"})
    res = run_check(ImportsCheck(PackageIndex(offline=True)), make_ctx(git_repo))
    assert res.findings == []
    assert "unreachable" in res.summary


# --- ruff ------------------------------------------------------------------


def test_ruff_reports_only_new_pitfalls(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"m.py": "import os\n\n\ndef f():\n    return 1\n"},  # pre-existing unused import
        {
            "m.py": (
                "import os\n\n\ndef f():\n    try:\n        return 1\n    except:\n        pass\n"
            )
        },
    )
    res = run_check(RuffCheck(), make_ctx(git_repo))
    assert res.status is CheckStatus.FAILED
    assert {(f.rule, f.line_start) for f in res.findings} == {
        ("ruff:E722", 7),
        ("ruff:S110", 7),
    }
    assert all(f.category is Category.SWALLOWED_ERROR for f in res.findings)


# --- mypy ------------------------------------------------------------------


def test_mypy_flags_hallucinated_stdlib_api(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"m.py": "import json\n"},
        {"m.py": "import json\n\n\ndef load(s):\n    return json.parse_string(s)\n"},
    )
    res = run_check(MypyCheck(), make_ctx(git_repo))
    assert res.status is CheckStatus.FAILED, res.summary
    [f] = res.findings
    assert f.category is Category.HALLUCINATED_DEPENDENCY
    assert (f.file, f.line_start, f.rule) == ("m.py", 5, "mypy:attr-defined")


# --- pytest ----------------------------------------------------------------


def test_pytest_failure_becomes_blocker_on_changed_test(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"calc.py": "def add(a, b):\n    return a + b\n", "test_calc.py": ""},
        {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": (
                "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
            ),
        },
    )
    ctx = make_ctx(git_repo, sandbox=SubprocessSandbox())
    res = run_check(TestsCheck(), ctx)
    assert res.status is CheckStatus.FAILED, res.output_excerpt
    [f] = res.findings
    assert f.file == "test_calc.py"
    assert f.severity is Severity.BLOCKER


def test_pytest_passing(git_repo: Path) -> None:
    branch_change(
        git_repo,
        {"test_ok.py": "def test_ok():\n    assert True\n"},
        {"test_ok.py": "def test_ok():\n    assert 1 + 1 == 2\n"},
    )
    res = run_check(TestsCheck(), make_ctx(git_repo, sandbox=SubprocessSandbox()))
    assert res.status is CheckStatus.PASSED, res.output_excerpt


def test_pytest_preexisting_failure_not_attributed(git_repo: Path) -> None:
    broken = "def test_old():\n    assert False\n"
    branch_change(
        git_repo,
        {"test_old.py": broken, "a.py": ""},
        {"a.py": "x = 1\n", "test_new.py": "def test_new():\n    assert 1 == 2\n"},
    )
    res = run_check(TestsCheck(), make_ctx(git_repo, sandbox=SubprocessSandbox()))
    assert [f.summary for f in res.findings] == ["Test fails: test_new.py::test_new"]
    assert "1 also fail at merge-base" in res.summary


def test_pytest_custom_command_python_placeholder(git_repo: Path) -> None:
    cfg = Config()
    cfg.review.test_command = ["{python}", "-m", "pytest", "-q", "-k", "not slow"]
    branch_change(
        git_repo,
        {"test_x.py": "def test_fast():\n    pass\n"},
        {"test_x.py": "def test_fast():\n    pass\n\n\ndef test_slow():\n    assert 0\n"},
    )
    res = run_check(TestsCheck(), make_ctx(git_repo, sandbox=SubprocessSandbox(), config=cfg))
    assert res.status is CheckStatus.PASSED, res.output_excerpt


def test_pytest_skipped_without_sandbox(git_repo: Path) -> None:
    branch_change(git_repo, {"a.py": ""}, {"a.py": "x = 1\n"})
    assert run_check(TestsCheck(), make_ctx(git_repo)).status is CheckStatus.SKIPPED


# --- hygiene ---------------------------------------------------------------


def test_hygiene_detects_tampering(git_repo: Path) -> None:
    old_test = "def test_a():\n    assert f(1) == 1\n    assert f(2) == 2\n    assert f(3) == 3\n"
    new_test = "import pytest\n\n\n@pytest.mark.skip\ndef test_a():\n    f(1)\n"
    branch_change(
        git_repo,
        {
            "tests/test_f.py": old_test,
            "tests/test_gone.py": "def test_x():\n    pass\n",
            "m.py": "",
        },
        {"tests/test_f.py": new_test, "m.py": "x = eval('1')  # noqa: S307\n"},
    )
    git(git_repo, "rm", "-q", "tests/test_gone.py")
    git(git_repo, "commit", "-q", "-m", "rm")
    res = run_check(HygieneCheck(), make_ctx(git_repo))
    assert rules(res) == [
        "hygiene:assertions-removed",
        "hygiene:suppression",
        "hygiene:test-deleted",
        "hygiene:test-skipped",
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("x = 1  # noqa", ("noqa", Severity.MINOR)),
        ("x = 1  # type: ignore", ("type: ignore", Severity.MINOR)),
        ("x = 1  # noqa: E501", ("noqa", Severity.NIT)),
        ("x = f()  # type: ignore[attr-defined]", ("type: ignore", Severity.NIT)),
        ("except Exception:  # noqa: BLE001 - must not abort the review", None),
        ("x = f()  # type: ignore[arg-type]  # stub is wrong upstream", None),
        ("x = 1  # ordinary comment", None),
    ],
)
def test_classify_suppression(line: str, expected: tuple[str, Severity] | None) -> None:
    from watchman_review.checks.hygiene import classify_suppression

    assert classify_suppression(line) == expected


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("@pytest.mark.skip", True),
        ('@pytest.mark.skip(reason="flaky")', True),
        ("@pytest.mark.xfail", True),
        ('@pytest.mark.xfail(reason="todo")', True),
        ('@unittest.skip("later")', True),
        ('@pytest.mark.skipif(sys.platform == "win32", reason="posix")', False),
        ("@pytest.mark.xfail(sys.version_info < (3, 13), reason='x')", False),
        ('        pytest.skip("docker unavailable")', False),
        ('src = "@pytest.mark.skip\\ndef test_a(): ..."', False),
    ],
)
def test_skip_detection_is_unconditional_decorators_only(line: str, flagged: bool) -> None:
    from watchman_review.checks.hygiene import _SKIP

    assert bool(_SKIP.search(line)) is flagged


def test_suppression_inside_string_literal_ignored(git_repo: Path) -> None:
    src = 'CASES = ["x  # noqa", """\n# type: ignore\n"""]\ny = 1  # noqa\n'
    branch_change(git_repo, {"a.py": ""}, {"a.py": src})
    res = run_check(HygieneCheck(), make_ctx(git_repo))
    assert [(f.rule, f.line_start) for f in res.findings] == [("hygiene:suppression", 4)]


def test_private_key_header_without_body_is_ignored(git_repo: Path) -> None:
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    body = "MIIEow" + "A" * 58
    branch_change(
        git_repo,
        {"a.py": ""},
        {"a.py": f'MARKER = "{header}"\n', "key.pem": f"{header}\n{body}\n"},
    )
    res = run_check(SecretsCheck(), make_ctx(git_repo))
    assert [(f.file, f.rule) for f in res.findings] == [("key.pem", "secrets:private-key")]


def test_generic_secret_in_tests_downgraded(git_repo: Path) -> None:
    line = 'db_password = "Zx9!qL2#vR7$mK4@"\n'
    branch_change(git_repo, {"a.py": ""}, {"tests/test_db.py": line, "app.py": line})
    res = run_check(SecretsCheck(), make_ctx(git_repo))
    sev = {f.file: f.severity for f in res.findings}
    assert sev == {"app.py": Severity.MAJOR, "tests/test_db.py": Severity.MINOR}


# --- scope -----------------------------------------------------------------


def test_scope_flags_large_diff_and_ci(git_repo: Path) -> None:
    cfg = Config()
    cfg.review.max_diff_files_warn = 2
    branch_change(
        git_repo,
        {"a.py": ""},
        {"a.py": "x=1\n", "b.py": "y=1\n", ".github/workflows/ci.yml": "on: push\n"},
    )
    res = run_check(ScopeCheck(), make_ctx(git_repo, config=cfg))
    assert rules(res) == ["scope:ci-config", "scope:large-diff"]
    assert "3 file(s)" in res.summary


# --- runner ----------------------------------------------------------------


class Boom(Check):
    name = "boom"

    def run(self, ctx: CheckContext) -> CheckResult:
        raise RuntimeError("kaboom")


def test_crashing_check_becomes_error_not_exception(git_repo: Path) -> None:
    branch_change(git_repo, {"a.py": ""}, {"a.py": "x = 1\n"})
    res = run_check(Boom(), make_ctx(git_repo))
    assert res.status is CheckStatus.ERROR
    assert "kaboom" in res.summary


def test_run_checks_rejects_unknown(git_repo: Path) -> None:
    branch_change(git_repo, {"a.py": ""}, {"a.py": "x = 1\n"})
    with pytest.raises(ValueError, match="unknown check"):
        run_checks(make_ctx(git_repo), ["nope"])


def test_default_checks_are_registered() -> None:
    assert set(Config().review.checks) <= set(REGISTRY)
