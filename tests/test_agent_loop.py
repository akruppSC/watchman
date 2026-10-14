from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import commit_files, git
from watchman_review.agent.grounding import Grade, grade_finding
from watchman_review.config import Config
from watchman_review.llm.fake import FakeClient
from watchman_review.llm.types import CompletionRequest, LLMResponse, Role, ToolCall
from watchman_review.review import ReviewOptions, run_review
from watchman_review.schema import (
    Category,
    Finding,
    ReviewMode,
    Severity,
    Source,
    ToolTraceStep,
    Verdict,
)


@pytest.fixture
def dup_repo(git_repo: Path) -> Path:
    commit_files(
        git_repo,
        {
            "pyproject.toml": '[project]\nname = "pkg"\nversion = "0"\ndependencies = []\n',
            "pkg/__init__.py": "",
            "pkg/text.py": "def slugify(s):\n    return s.lower().replace(' ', '-')\n",
        },
        "base",
    )
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit_files(
        git_repo,
        {
            "pkg/util.py": (
                "# Convert text to a slug.\n"
                "def to_slug(text):\n    return text.lower().replace(' ', '-')\n"
            )
        },
        "add to_slug",
    )
    return git_repo


def call(name: str, cid: str, **args: Any) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=cid, name=name, arguments=args)], input_tokens=10)


def finding(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "category": "duplication", "severity": "major", "confidence": 0.9,
        "file": "pkg/util.py", "line_start": 2, "line_end": 3,
        "summary": "to_slug duplicates pkg.text.slugify",
        "evidence": "search_repo found identical expression in pkg/text.py:2",
        "evidence_refs": ["t1"],
    }  # fmt: skip
    base.update(kw)
    return base


def submit(*findings: dict[str, Any]) -> LLMResponse:
    return call("submit_findings", "s1", findings=list(findings))


def agent_opts(client: FakeClient, mode: ReviewMode = ReviewMode.AGENT) -> ReviewOptions:
    return ReviewOptions(
        base="main", head="feature", mode=mode, task="Add a slug helper", offline=True,
        ledger=None, llm_client=client, checks=["secrets", "hygiene", "ruff"],
    )  # fmt: skip


def test_agent_verifies_with_tool_and_cites_evidence(dup_repo: Path) -> None:
    """Milestone 2 acceptance (scripted): a suspicion is verified by a tool call and cited."""
    fake = FakeClient(
        [
            call("search_repo", "c1", pattern=".lower().replace(' ', '-')"),
            submit(
                finding(),
                finding(category="comment_noise", severity="nit", line_start=1, line_end=1,
                        summary="Comment restates the function name", evidence_refs=["diff"]),
                finding(category="correctness", line_start=3, line_end=3,
                        summary="Invented problem", evidence_refs=["t99"]),
            ),
        ]
    )  # fmt: skip
    res = run_review(dup_repo, agent_opts(fake))

    by_cat = {f.category: f for f in res.findings}
    dup = by_cat[Category.DUPLICATION]
    assert dup.evidence_refs == ["t1"]
    assert dup.confidence == 0.9
    assert dup.source is Source.AGENT
    # A diff-only finding is kept but capped; it sits on a different line than the
    # duplication finding, so it is a separate defect rather than a restatement.
    assert by_cat[Category.COMMENT_NOISE].confidence == 0.6
    reasons = {d.finding.category: d.reason for d in res.dropped}
    assert reasons[Category.CORRECTNESS].startswith("confidence 0.30")
    assert res.dropped_findings == 1
    assert [s.ref for s in res.tool_trace] == ["t1"]
    assert res.tool_trace[0].tool == "search_repo"
    assert "pkg/text.py:2" in res.tool_trace[0].output_excerpt
    assert res.usage.tool_calls == 1
    assert res.usage.llm_calls == 2
    assert res.verdict is Verdict.WARN

    first, second = fake.requests
    user = first.messages[1].content
    assert "<untrusted_task>" in user and "<untrusted_diff>" in user
    assert "<untrusted_layer1>" in user and "[ref:check:ruff]" in user
    tool_msg = second.messages[-1]
    assert tool_msg.role is Role.TOOL
    assert tool_msg.content.startswith("[ref:t1]\n<untrusted_tool_output>")


def test_agent_no_l1_mode_has_no_layer1(dup_repo: Path) -> None:
    fake = FakeClient([submit()])
    res = run_review(dup_repo, agent_opts(fake, ReviewMode.AGENT_NO_L1))
    assert res.checks == []
    assert res.findings == []
    assert "<untrusted_layer1>" not in fake.requests[0].messages[1].content


def test_tool_budget_forces_submission(dup_repo: Path) -> None:
    seen: list[CompletionRequest] = []
    script = [
        call("read_file", "a", path="pkg/util.py"),
        call("read_file", "b", path="pkg/text.py"),  # over budget -> refused
        submit(),
    ]

    def responder(req: CompletionRequest) -> LLMResponse:
        seen.append(req)
        return script[len(seen) - 1]

    cfg = Config()
    cfg.budget.max_tool_calls = 1
    cfg.review.checks = ["secrets"]
    res = run_review(dup_repo, agent_opts(FakeClient(responder=responder)), cfg)
    assert [s.ref for s in res.tool_trace] == ["t1"]
    assert res.usage.tool_calls == 1
    texts = [m.content for m in seen[-1].messages]
    assert any("Budget nearly exhausted" in t for t in texts)
    assert any("tool budget exhausted" in t for t in texts)


def test_text_json_answer_accepted(dup_repo: Path) -> None:
    payload = {"findings": [finding(evidence_refs=["diff"])]}
    fake = FakeClient([LLMResponse(content=json.dumps(payload))])
    res = run_review(dup_repo, agent_opts(fake))
    [f] = [f for f in res.findings if f.source is Source.AGENT]
    assert f.confidence == 0.6


def test_agent_that_never_submits_warns(dup_repo: Path) -> None:
    fake = FakeClient([LLMResponse(content="Looks fine."), LLMResponse(content="Still fine.")])
    res = run_review(dup_repo, agent_opts(fake))
    assert any("without submitting" in w for w in res.warnings)
    assert len(fake.requests) == 2
    assert "submit_findings" in fake.requests[1].messages[-1].content


def test_tool_errors_are_returned_to_model_not_raised(dup_repo: Path) -> None:
    fake = FakeClient([call("read_file", "a", path="../../etc/passwd"), submit()])
    res = run_review(dup_repo, agent_opts(fake))
    assert res.tool_trace[0].ok is False
    assert "escapes" in res.tool_trace[0].output_excerpt


def test_failed_tool_call_cannot_be_cited(dup_repo: Path) -> None:
    fake = FakeClient(
        [call("read_file", "a", path="missing.py"), submit(finding(evidence_refs=["t1"]))]
    )
    res = run_review(dup_repo, agent_opts(fake))
    assert [f for f in res.findings if f.source is Source.AGENT] == []


def _step(ref: str, ok: bool = True, output: str = "", **args: Any) -> ToolTraceStep:
    return ToolTraceStep(ref=ref, tool="read_file", arguments=args, ok=ok, output_excerpt=output)


@pytest.mark.parametrize(
    ("refs", "steps", "grade"),
    [
        (["t1"], {"t1": _step("t1", path="a.py")}, Grade.VERIFIED),
        (["t1"], {"t1": _step("t1", output="a.py:3: x")}, Grade.VERIFIED),
        (["t1"], {"t1": _step("t1", path="other.py")}, Grade.DIFF_ONLY),  # irrelevant tool
        (["check:ruff"], {}, Grade.VERIFIED),
        (["diff"], {}, Grade.DIFF_ONLY),
        (["t1"], {"t1": _step("t1", ok=False, path="a.py")}, Grade.UNVERIFIED),
        ([], {}, Grade.UNVERIFIED),
    ],
)
def test_grade_finding(refs: list[str], steps: dict[str, ToolTraceStep], grade: Grade) -> None:
    f = Finding(
        category=Category.CORRECTNESS, severity=Severity.MAJOR, confidence=0.9, file="a.py",
        line_start=3, line_end=3, summary="s", evidence="e", source=Source.AGENT,
        evidence_refs=refs,
    )  # fmt: skip
    got, _, _ = grade_finding(f, steps, {"check:ruff"})
    assert got is grade


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("t3", "t3"),
        ("[ref:t3]", "t3"),
        ("ref:T3", "t3"),
        (" t3 (read_file a.py)", "t3"),
        ("DIFF", "diff"),
        ("check:ruff", "check:ruff"),
        ("[ref:check:pytest]", "check:pytest"),
        ("made-up", "made-up"),
    ],
)
def test_normalize_ref(raw: str, norm: str) -> None:
    from watchman_review.agent.grounding import normalize_ref

    assert normalize_ref(raw) == norm


def test_dropped_findings_are_reported_with_reason(dup_repo: Path) -> None:
    fake = FakeClient([submit(finding(evidence_refs=["t42"]))])
    res = run_review(dup_repo, agent_opts(fake))
    [d] = [d for d in res.dropped if d.finding.category is Category.DUPLICATION]
    assert "grounding=unverified" in d.reason
    assert "t42" in d.reason


def test_model_cannot_spoof_source_or_rule(dup_repo: Path) -> None:
    spoof = finding(evidence_refs=["diff"], source="deterministic", rule="ruff:E722")
    res = run_review(dup_repo, agent_opts(FakeClient([submit(spoof)])))
    [f] = [f for f in res.findings if f.category is Category.DUPLICATION]
    assert f.source is Source.AGENT
    assert f.rule is None
    assert f.confidence == 0.6


def test_llm_failure_marks_review_incomplete_not_clean(dup_repo: Path) -> None:
    from watchman_review.llm.types import LLMError

    def responder(req: CompletionRequest) -> LLMResponse:
        raise LLMError("A Timeout Occurred")

    res = run_review(dup_repo, agent_opts(FakeClient(responder=responder)))
    assert res.incomplete
    assert res.verdict is not Verdict.PASS
    assert "did not finish" in res.incomplete_reasons[0]
    assert any("LLM error" in w for w in res.warnings)


def test_auth_failure_stops_agent_with_clear_warning(dup_repo: Path) -> None:
    from watchman_review.llm.types import LLMAuthError

    def responder(req: CompletionRequest) -> LLMResponse:
        raise LLMAuthError("out of credits (402)")

    res = run_review(dup_repo, agent_opts(FakeClient(responder=responder)))
    assert res.incomplete
    assert any("out of credits" in w for w in res.warnings)
    assert len(res.tool_trace) == 0


def test_cli_exits_2_on_incomplete(dup_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from watchman_review import cli
    from watchman_review.schema import ReviewResult

    def fake_run(root: Path, opts: ReviewOptions) -> ReviewResult:
        return ReviewResult(
            mode=ReviewMode.AGENT, base="b", head="h", verdict=Verdict.WARN, findings=[],
            incomplete=True, incomplete_reasons=["agent model stage did not finish"],
        )  # fmt: skip

    monkeypatch.setattr(cli, "run_review", fake_run)
    result = CliRunner().invoke(cli.app, ["review", "--repo", str(dup_repo), "--fail-on", "never"])
    assert result.exit_code == 2
    assert "incomplete" in result.output
