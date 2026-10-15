from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.conftest import commit_files, git
from watchman_review.ci import (
    CiError,
    CiOptions,
    PullRequest,
    gate,
    load_pull_request,
    run_ci,
    upsert_comment,
)
from watchman_review.report_md import MARKER, md_safe, render_markdown
from watchman_review.schema import (
    Category,
    CheckResult,
    CheckStatus,
    Finding,
    ReviewMode,
    ReviewResult,
    Severity,
    Source,
    StageSummary,
    Usage,
    Verdict,
)

ROOT = Path(__file__).resolve().parents[1]


def finding(**kw: Any) -> Finding:
    base: dict[str, Any] = {
        "category": Category.SWALLOWED_ERROR, "severity": Severity.MAJOR, "confidence": 0.9,
        "file": "src/app.py", "line_start": 10, "line_end": 12,
        "summary": "Bare except hides errors",
        "evidence": "read_file shows `except: pass`", "source": Source.AGENT,
        "suggested_fix": "Catch the specific exception.",
    }  # fmt: skip
    base.update(kw)
    return Finding.model_validate(base)


def result(findings: list[Finding] | None = None, **kw: Any) -> ReviewResult:
    fs = findings if findings is not None else [finding()]
    base: dict[str, Any] = {
        "mode": ReviewMode.AGENT, "base": "b", "head": "h", "verdict": Verdict.WARN,
        "findings": fs, "usage": Usage(model="m/x", llm_calls=3, cost_usd=0.0421),
        "latency_s": 41.6,
    }  # fmt: skip
    base.update(kw)
    return ReviewResult.model_validate(base)


# --- markdown ---------------------------------------------------------------


def test_markdown_has_marker_verdict_and_linked_finding() -> None:
    md = render_markdown(result(), blob_base="https://github.com/o/r/blob/" + "a" * 40)
    assert md.startswith(MARKER)
    assert "WARN: findings worth a look" in md
    assert "1 finding(s): 1 major" in md
    assert "[`src/app.py:10-12`](https://github.com/o/r/blob/" in md
    assert md.count("#L10-L12") == 1
    assert "Fix: Catch the specific exception." in md
    assert "model `m/x`, $0.042" in md


def test_markdown_orders_findings_by_severity_and_caps_the_list() -> None:
    many = [finding(line_start=i, line_end=i, summary=f"s{i}", severity=Severity.NIT)
            for i in range(1, 40)]  # fmt: skip
    many.append(
        finding(line_start=99, line_end=99, summary="the blocker", severity=Severity.BLOCKER)
    )
    md = render_markdown(result(many, verdict=Verdict.BLOCK))
    assert md.index("the blocker") < md.index("s1\n")
    assert "…and 15 more" in md


def test_markdown_incomplete_banner_and_clean_review() -> None:
    md = render_markdown(
        result([], verdict=Verdict.WARN, incomplete=True, incomplete_reasons=["agent timed out"])
    )
    assert "Incomplete review" in md and "not a clean bill of health" in md
    clean = render_markdown(result([], verdict=Verdict.PASS))
    assert "PASS" in clean and "0 finding(s): no findings" in clean


def test_markdown_sections_for_checks_stages_and_warnings() -> None:
    r = result(
        checks=[
            CheckResult(id="check:ruff", name="ruff", status=CheckStatus.FAILED, summary="1 hit")
        ],
        stages=[
            StageSummary(name="primary", model="a/b"),
            StageSummary(name="overseer", model="c/d", ran=False),
        ],
        warnings=["something odd", "Running target-repo tests in a local subprocess"],
    )
    md = render_markdown(r)
    assert "Layer 1 checks (1 found problems)" in md
    assert "| overseer | c/d | skipped |" in md
    assert "something odd" in md and "local subprocess" not in md


@pytest.mark.parametrize(
    ("hostile", "must_not_contain"),
    [
        ("ping @octocat and @org/team now", "@octocat"),
        ("see [click me](https://evil.example/x)", "](https://"),
        ("visit https://evil.example/phish", "https://evil"),
        ("<img src=x onerror=alert(1)> <script>x</script>", "<img"),
        ("closes #123 and fixes #9", "#123"),
        ("```\nescape the fence\n```", "```"),
        ("a | b | c", "a | b"),
    ],
)
def test_untrusted_text_is_made_inert(hostile: str, must_not_contain: str) -> None:
    assert must_not_contain not in md_safe(hostile)
    md = render_markdown(result([finding(summary=hostile, evidence=hostile)]))
    assert must_not_contain not in md


def test_model_text_cannot_forge_the_marker() -> None:
    md = render_markdown(result([finding(summary=f"x {MARKER} y", evidence=f"{MARKER}")]))
    assert md.count(MARKER) == 1  # only ours, at the top


def test_oversized_comment_is_truncated() -> None:
    big = [
        finding(line_start=i, line_end=i, evidence="e" * 390, summary="s" * 290) for i in range(25)
    ]
    r = result(big, warnings=["w" * 290] * 10)
    assert len(render_markdown(r)) <= 60_000


# --- comment upsert ---------------------------------------------------------


class FakeGitHub:
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self.comments = comments
        self.requests: list[tuple[str, str]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append((req.method, req.url.path))
        assert req.headers["authorization"] == "Bearer tok"
        path = req.url.path
        if req.method == "GET":
            page = int(req.url.params.get("page", "1"))
            return httpx.Response(200, json=self.comments[(page - 1) * 100 : page * 100])
        if req.method == "PATCH":
            cid = int(path.rsplit("/", 1)[1])
            if cid == 666:
                return httpx.Response(403, json={"message": "nope"})
            return httpx.Response(200, json={"html_url": f"https://x/c/{cid}"})
        return httpx.Response(201, json={"html_url": "https://x/c/new"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def bot(cid: int, body: str) -> dict[str, Any]:
    return {"id": cid, "body": body, "user": {"type": "Bot", "login": "github-actions[bot]"}}


def human(cid: int, body: str) -> dict[str, Any]:
    return {"id": cid, "body": body, "user": {"type": "User", "login": "mallory"}}


def test_upsert_creates_when_absent_and_updates_in_place() -> None:
    gh = FakeGitHub([bot(1, "unrelated"), human(2, "hi")])
    assert (
        upsert_comment(gh.client(), "https://api", "o/r", 7, MARKER + "v1", "tok")[0] == "created"
    )
    gh2 = FakeGitHub([bot(1, "unrelated"), bot(5, MARKER + "old")])
    action, url = upsert_comment(gh2.client(), "https://api", "o/r", 7, MARKER + "v2", "tok")
    assert (action, url) == ("updated", "https://x/c/5")
    assert ("PATCH", "/repos/o/r/issues/comments/5") in gh2.requests


def test_upsert_never_touches_a_humans_comment_that_copies_the_marker() -> None:
    gh = FakeGitHub([human(9, MARKER + " pretend")])
    action, _ = upsert_comment(gh.client(), "https://api", "o/r", 7, MARKER + "mine", "tok")
    assert action == "created"
    assert not any(m == "PATCH" for m, _ in gh.requests)


def test_upsert_falls_back_to_posting_when_the_edit_is_refused() -> None:
    gh = FakeGitHub([bot(666, MARKER + "owned by another app")])
    assert upsert_comment(gh.client(), "https://api", "o/r", 7, MARKER + "x", "tok")[0] == "created"


def test_upsert_finds_our_comment_on_a_later_page() -> None:
    comments = [bot(i, "noise") for i in range(1, 101)] + [bot(500, MARKER + "old")]
    gh = FakeGitHub(comments)
    assert upsert_comment(gh.client(), "https://api", "o/r", 7, MARKER + "x", "tok")[0] == "updated"


# --- run_ci -----------------------------------------------------------------


def write_event(path: Path, base: str, head: str, *, fork: bool = False) -> Path:
    head_repo = "mallory/r" if fork else "o/r"
    path.write_text(json.dumps({"pull_request": {
        "number": 7, "title": "Add loader", "body": "Please review",
        "base": {"sha": base, "repo": {"full_name": "o/r"}},
        "head": {"sha": head, "repo": {"full_name": head_repo}},
    }}))  # fmt: skip
    return path


@pytest.fixture
def pr_repo(git_repo: Path) -> tuple[Path, str, str]:
    base = commit_files(git_repo, {"pyproject.toml": '[project]\nname="x"\ndependencies=[]\n',
                                   "a.py": "x = 1\n"}, "base")  # fmt: skip
    git(git_repo, "checkout", "-q", "-b", "feature")
    head = commit_files(git_repo, {"a.py": "try:\n    x = 1\nexcept:\n    pass\n"}, "change")
    return git_repo, base, head


def ci_env(tmp_path: Path, event: Path, **extra: str) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "tok", "RUNNER_TEMP": str(tmp_path / "tmp"),
        "GITHUB_OUTPUT": str(tmp_path / "out.txt"), "GITHUB_STEP_SUMMARY": str(tmp_path / "sum.md"),
        "GITHUB_API_URL": "https://api.github.test", **extra,
    }  # fmt: skip


def test_run_ci_reviews_the_pr_posts_a_comment_and_writes_outputs(
    pr_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, base, head = pr_repo
    gh = FakeGitHub([])
    out = run_ci(
        CiOptions(checks=["ruff", "secrets"]),
        ci_env(tmp_path, write_event(tmp_path / "ev.json", base, head)),
        cwd=repo, http=gh.client(),
    )  # fmt: skip
    assert out.comment_action == "created"
    assert ("POST", "/repos/o/r/issues/7/comments") in gh.requests
    assert out.exit_code == 0  # ruff findings are WARN, and the default gate is block
    assert {f.rule for f in out.result.findings} >= {"ruff:E722"}
    assert out.markdown.startswith(MARKER) and f"/blob/{head}/a.py" in out.markdown
    outputs = dict(line.split("=", 1) for line in (tmp_path / "out.txt").read_text().splitlines())
    assert outputs["verdict"] == "warn" and outputs["incomplete"] == "false"
    assert "Watchman review" in (tmp_path / "sum.md").read_text()
    assert json.loads(out.json_path.read_text())["mode"] == "deterministic"


def test_run_ci_gate_and_no_comment(pr_repo: tuple[Path, str, str], tmp_path: Path) -> None:
    repo, base, head = pr_repo
    env = ci_env(tmp_path, write_event(tmp_path / "ev.json", base, head))
    gh = FakeGitHub([])
    out = run_ci(CiOptions(checks=["ruff"], fail_on="warn", comment=False), env, cwd=repo,
                 http=gh.client())  # fmt: skip
    assert out.exit_code == 1
    assert gh.requests == []  # --no-comment: nothing was posted


def test_run_ci_degrades_gracefully_when_the_token_cannot_comment(
    pr_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, base, head = pr_repo

    def deny(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    env = ci_env(tmp_path, write_event(tmp_path / "ev.json", base, head, fork=True))
    out = run_ci(CiOptions(checks=["ruff"]), env, cwd=repo,
                 http=httpx.Client(transport=httpx.MockTransport(deny)))  # fmt: skip
    assert out.comment_action is None
    assert any("could not post the PR comment" in n for n in out.notes)
    assert "Watchman review" in (tmp_path / "sum.md").read_text()  # still in the job summary


def test_fork_pr_without_a_key_falls_back_to_layer1(
    pr_repo: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = pr_repo
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env = ci_env(tmp_path, write_event(tmp_path / "ev.json", base, head, fork=True))
    out = run_ci(CiOptions(mode=ReviewMode.AGENT, checks=["ruff"], comment=False), env, cwd=repo)
    assert out.result.mode is ReviewMode.DETERMINISTIC
    assert any("fork PR" in w for w in out.result.warnings)


def test_strict_fails_on_incomplete_only_when_asked(
    pr_repo: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = pr_repo
    env = ci_env(tmp_path, write_event(tmp_path / "ev.json", base, head))
    from watchman_review.llm.fake import FakeClient
    from watchman_review.llm.types import LLMError
    from watchman_review.review import ReviewOptions

    def boom(req: object) -> Any:
        raise LLMError("A Timeout Occurred")

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    for strict, code in ((False, 0), (True, 1)):
        out = run_ci(
            CiOptions(mode=ReviewMode.AGENT, checks=["secrets"], strict=strict, comment=False),
            env, cwd=repo,
            base_options=ReviewOptions(llm_client=FakeClient(responder=boom), ledger=None),
        )  # fmt: skip
        assert out.result.incomplete and out.exit_code == code


def test_run_ci_errors_clearly_without_a_base_or_history(tmp_path: Path, git_repo: Path) -> None:
    commit_files(git_repo, {"a.py": ""}, "base")
    with pytest.raises(CiError, match="no base"):
        run_ci(CiOptions(), {}, cwd=git_repo)
    with pytest.raises(CiError, match="fetch-depth"):
        run_ci(CiOptions(base="main", head="deadbeef" * 5), {}, cwd=git_repo)


def test_pull_request_event_parsing(tmp_path: Path) -> None:
    pr = load_pull_request(write_event(tmp_path / "e.json", "b" * 40, "h" * 40, fork=True))
    assert (pr.number, pr.is_fork) == (7, True)
    assert pr.task() == "Add loader\n\nPlease review"
    long = PullRequest(1, "t", "x" * 9000, "b", "h", "o/r", "o/r")
    assert len(long.task()) == 4000 and not long.is_fork
    with pytest.raises(CiError):
        load_pull_request(tmp_path / "missing.json")


@pytest.mark.parametrize(
    ("verdict", "fail_on", "expected"),
    [(Verdict.BLOCK, "block", True), (Verdict.WARN, "block", False), (Verdict.WARN, "warn", True),
     (Verdict.BLOCK, "never", False), (Verdict.PASS, "warn", False)],
)  # fmt: skip
def test_gate(verdict: Verdict, fail_on: str, expected: bool) -> None:
    assert gate(verdict, fail_on) is expected


# --- the action definition --------------------------------------------------


def load_action() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "action.yml").read_text())  # type: ignore[no-any-return]


def test_action_outputs_match_what_the_ci_command_writes() -> None:
    action = load_action()
    for name, spec in action["outputs"].items():
        assert f"steps.review.outputs.{name}" in spec["value"]
    # every output the action declares is written by run_ci, and vice versa
    import re

    src = (ROOT / "src/watchman_review/ci.py").read_text()
    written = set(re.findall(r'^\s+"([a-z-]+)": ', src[src.index("write_outputs(\n") :], re.M))
    assert written == set(action["outputs"])


def test_action_never_interpolates_inputs_into_shell_scripts() -> None:
    steps = load_action()["runs"]["steps"]
    for step in steps:
        script = step.get("run", "")
        assert "${{" not in script, "inputs must reach scripts through env, not interpolation"
    run_step = next(s for s in steps if s.get("id") == "review")
    assert run_step["env"]["WM_MODE"] == "${{ inputs.mode }}"
    assert run_step["shell"] == "bash"


def test_action_inputs_cover_every_env_the_script_reads() -> None:
    action = load_action()
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "review")
    for var, expr in step["env"].items():
        if expr.startswith("${{ inputs."):
            name = expr.removeprefix("${{ inputs.").removesuffix(" }}")
            assert name in action["inputs"], f"{var} reads undeclared input {name}"
        assert var in step["run"] or var == "GITHUB_TOKEN"


def test_action_cli_flags_exist() -> None:
    from typer.testing import CliRunner

    from watchman_review.cli import app

    help_text = CliRunner().invoke(app, ["ci", "--help"]).output
    script = next(s for s in load_action()["runs"]["steps"] if s.get("id") == "review")["run"]
    import re

    for flag in set(re.findall(r"--[a-z][a-z-]+", script)) - {"--project", "--frozen"}:
        assert flag in help_text, f"{flag} is used by the action but missing from `watchman ci`"


def test_workflows_are_valid_and_use_the_safe_trigger() -> None:
    for path in [ROOT / ".github/workflows/watchman.yml", ROOT / "examples/workflows/watchman.yml"]:
        wf = yaml.safe_load(path.read_text())
        triggers = wf.get("on", wf.get(True))
        assert "pull_request_target" not in (triggers if isinstance(triggers, dict) else [triggers])
        assert wf["permissions"]["pull-requests"] == "write"
        checkout = next(s for s in wf["jobs"]["review"]["steps"] if "checkout" in s.get("uses", ""))
        assert checkout["with"]["fetch-depth"] == 0
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert ci["permissions"] == {"contents": "read"}
