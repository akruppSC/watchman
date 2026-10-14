from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import commit_files, git
from watchman_review.agent.loop import AgentOutcome
from watchman_review.config import Config, OverseerConfig, OverseerTrigger, load_config
from watchman_review.gitutil import parse_diff
from watchman_review.llm.fake import FakeClient
from watchman_review.llm.types import CompletionRequest, LLMError, LLMResponse, ToolCall
from watchman_review.overseer import (
    apply_verdicts,
    build_notes,
    feedback_needed,
    matches_rejected,
    risk_reasons,
    should_run,
)
from watchman_review.review import ReviewOptions, effective_trigger, run_review
from watchman_review.schema import (
    Category,
    CheckResult,
    CheckStatus,
    Finding,
    ReviewMode,
    Severity,
    Source,
    ToolTraceStep,
)

BIG = "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(34))  # 102 lines


@pytest.fixture
def repo(git_repo: Path) -> Path:
    commit_files(
        git_repo,
        {"pyproject.toml": '[project]\nname = "pkg"\nversion = "0"\ndependencies = []\n',
         "pkg/__init__.py": ""},
        "base",
    )  # fmt: skip
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit_files(git_repo, {"pkg/big.py": BIG}, "add big")
    return git_repo


def finding(
    line: int, sev: Severity = Severity.MAJOR, conf: float = 0.9, **kw: Any
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "category": "correctness", "severity": sev.value, "confidence": conf,
        "file": "pkg/big.py", "line_start": line, "line_end": line,
        "summary": f"problem at {line}", "evidence": f"read_file shows line {line}",
        "evidence_refs": ["t1"],
    }  # fmt: skip
    d.update(kw)
    return d


def call(name: str, cid: str, **args: Any) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=cid, name=name, arguments=args)], input_tokens=10)


def opts(primary: FakeClient, overseer: FakeClient | None, trigger: OverseerTrigger, **kw: Any):
    return ReviewOptions(
        base="main", head="feature", mode=ReviewMode.AGENT, task="Add helpers", offline=True,
        ledger=None, llm_client=primary, overseer_client=overseer, overseer=trigger,
        checks=["secrets", "hygiene"], **kw,
    )  # fmt: skip


def make_finding(line: int = 5, sev: Severity = Severity.MAJOR, **kw: Any) -> Finding:
    base: dict[str, Any] = {
        "category": Category.CORRECTNESS, "severity": sev, "confidence": 0.9,
        "file": "pkg/big.py", "line_start": line, "line_end": line, "summary": f"s{line}",
        "evidence": "e", "source": Source.AGENT,
    }  # fmt: skip
    base.update(kw)
    return Finding.model_validate(base)


def outcome(findings: list[Finding], **kw: Any) -> AgentOutcome:
    return AgentOutcome(findings=findings, trace=[], submitted=kw.pop("submitted", True), **kw)


# --- triggers ---------------------------------------------------------------


def diff_of(added: list[str], path: str = "pkg/m.py", extra_files: int = 0):
    body = "".join(f"+{ln}\n" for ln in added)
    raw = (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added)} @@\n{body}"
    )
    for i in range(extra_files):
        raw += (
            f"diff --git a/x{i}.py b/x{i}.py\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/x{i}.py\n@@ -0,0 +1 @@\n+y = 1\n"
        )
    return parse_diff(raw)


OC = OverseerConfig()


def reasons(findings: list[Finding] | None = None, **kw: Any) -> list[str]:
    fs = findings or []
    args: dict[str, Any] = {
        "primary": outcome(fs), "reportable": fs, "checks": None,
        "diff": diff_of(["x = 1"]), "oc": OC,
    }  # fmt: skip
    args.update(kw)
    return risk_reasons(**args)


def test_clean_small_review_is_not_risky() -> None:
    assert reasons([make_finding(sev=Severity.MINOR)]) == []


def test_major_finding_triggers() -> None:
    assert "1 major/blocker" in reasons([make_finding(sev=Severity.MAJOR)])[0]


def test_incomplete_primary_triggers() -> None:
    r = reasons(primary=outcome([], submitted=False))
    assert r == ["primary review did not finish"]


def test_escalation_triggers() -> None:
    from watchman_review.schema import Escalation

    out = outcome([], escalations=[Escalation(question="who calls this?")])
    assert "escalated 1 question" in reasons(primary=out)[0]


def test_failed_layer1_check_triggers_but_a_check_that_could_not_run_does_not() -> None:
    bad = CheckResult(id="check:ruff", name="ruff", status=CheckStatus.FAILED)
    ok = CheckResult(id="check:mypy", name="mypy", status=CheckStatus.PASSED)
    broken = CheckResult(id="check:pytest", name="pytest", status=CheckStatus.ERROR)
    assert reasons(checks=[ok, bad]) == ["Layer 1 check(s) found problems: ruff"]
    assert reasons(checks=[ok, broken]) == []


def test_large_diff_triggers_by_lines_and_files() -> None:
    assert "large diff" in reasons(diff=diff_of(["x = 1"] * 400))[0]
    assert "large diff" in reasons(diff=diff_of(["x = 1"], extra_files=9))[0]


@pytest.mark.parametrize(
    "line", ["subprocess.run(cmd, shell=True)", "data = pickle.loads(blob)",
             "cur.execute(query)", "requests.get(u, verify=False)"],
)  # fmt: skip
def test_sensitive_code_triggers(line: str) -> None:
    assert "security-sensitive" in reasons(diff=diff_of([line]))[0]


def test_sensitive_path_triggers_and_plain_code_does_not() -> None:
    assert "security-sensitive" in reasons(diff=diff_of(["x = 1"], path="app/auth/login.py"))[0]
    assert reasons(diff=diff_of(["def add(a, b):", "    return a + b"])) == []


@pytest.mark.parametrize(
    ("trigger", "why", "expected"),
    [
        (OverseerTrigger.NEVER, ["r"], False),
        (OverseerTrigger.ALWAYS, [], True),
        (OverseerTrigger.RISKY, [], False),
        (OverseerTrigger.RISKY, ["r"], True),
    ],
)
def test_should_run(trigger: OverseerTrigger, why: list[str], expected: bool) -> None:
    assert should_run(trigger, why) is expected


# --- verdicts and feedback --------------------------------------------------


def test_apply_verdicts_each_kind() -> None:
    f1, f2, f3, f4 = (make_finding(i) for i in (1, 2, 3, 4))
    packets = {"F1": f1, "F2": f2, "F3": f3, "F4": f4}
    raw = [
        {"id": "F1", "verdict": "confirm", "reason": "ok"},
        {"id": "f2", "verdict": "reject", "reason": "pre-existing"},
        {"id": "F3", "verdict": "downgrade", "reason": "overstated"},
        {"id": "F9", "verdict": "reject", "reason": "?"},
        {"id": "F1", "verdict": "reject", "reason": "second verdict is ignored"},
    ]
    out = apply_verdicts(raw, packets)
    by_line = {f.line_start: f for f in out.kept}
    assert by_line[1].verified_by == "overseer:confirmed"
    assert by_line[3].severity is Severity.MINOR and by_line[3].confidence < f3.confidence
    assert by_line[3].verified_by == "overseer:downgraded"
    assert by_line[4].verified_by is None  # unreviewed findings are left exactly as reported
    assert [(f.line_start, why) for f, why in out.rejected] == [(2, "pre-existing")]
    assert (out.confirmed, out.downgraded) == (1, 1)
    assert any("unknown finding 'F9'" in w for w in out.warnings)


def test_downgrade_never_raises_severity() -> None:
    f = make_finding(sev=Severity.MINOR)
    v = [{"id": "F1", "verdict": "downgrade", "reason": "r", "new_severity": "blocker",
          "new_confidence": 0.99}]  # fmt: skip
    [kept] = apply_verdicts(v, {"F1": f}).kept
    assert kept.severity is Severity.NIT and kept.confidence <= f.confidence


@pytest.mark.parametrize(
    ("rejected", "added", "checked", "expected"),
    [(0, 0, 8, False), (2, 0, 8, False), (2, 1, 8, True), (0, 3, 8, True),
     (1, 0, 2, True), (1, 0, 3, False), (1, 0, 1, False)],
)  # fmt: skip
def test_feedback_thresholds(rejected: int, added: int, checked: int, expected: bool) -> None:
    assert feedback_needed(OC, rejected=rejected, added=added, spot_checked=checked) is expected


def test_feedback_can_be_disabled() -> None:
    oc = OverseerConfig(feedback_max_rounds=0)
    assert not feedback_needed(oc, rejected=5, added=5, spot_checked=5)


def test_rejected_findings_match_nearby_same_category_only() -> None:
    rejected = [make_finding(10)]
    assert matches_rejected(make_finding(11), rejected)
    assert not matches_rejected(make_finding(40), rejected)
    assert not matches_rejected(make_finding(11, category=Category.SECURITY), rejected)


def test_notes_carry_rejections_and_new_findings() -> None:
    applied = apply_verdicts([{"id": "F1", "verdict": "reject", "reason": "dead code"}],
                             {"F1": make_finding(7)})  # fmt: skip
    notes = build_notes(applied, [make_finding(30, summary="missing check")], "look at callers")
    assert "look at callers" in notes
    assert "REJECTED your finding at pkg/big.py:7" in notes and "dead code" in notes
    assert "OVERSEER FOUND" in notes and "missing check" in notes


# --- end to end with scripted models ---------------------------------------


def three_findings_primary(escalate: bool = False) -> list[LLMResponse]:
    args: dict[str, Any] = {
        "findings": [
            finding(5, Severity.BLOCKER, 0.95),
            finding(45, Severity.MAJOR, 0.9),
            finding(85, Severity.MINOR, 0.8),
        ]
    }
    if escalate:
        args["escalations"] = [{"file": "pkg/big.py", "line_start": 60, "line_end": 62,
                                "question": "Is f15 used elsewhere?"}]  # fmt: skip
    return [call("read_file", "c1", path="pkg/big.py", start=1, end=100),
            call("submit_findings", "s1", **args)]  # fmt: skip


def test_overseer_rejects_and_primary_gets_a_second_pass(repo: Path) -> None:
    primary_calls: list[CompletionRequest] = []
    script = three_findings_primary(escalate=True)
    second_pass = [call("submit_findings", "s2", findings=[
        finding(5, Severity.BLOCKER, 0.95, evidence_refs=["r1"], summary="re-raised"),
        finding(65, Severity.MAJOR, 0.9, evidence_refs=["r1"], summary="new problem"),
    ])]  # fmt: skip

    def primary(req: CompletionRequest) -> LLMResponse:
        primary_calls.append(req)
        if "SECOND PASS" in req.messages[1].content:
            if not any(m.tool_call_id for m in req.messages):
                return call("read_file", "r-c1", path="pkg/big.py", start=60, end=70)
            return second_pass[0]
        return script.pop(0)

    overseer = FakeClient(
        [
            call("read_file", "o-c1", path="pkg/big.py", start=1, end=20),
            call("submit_oversight", "o-s", verdicts=[
                {"id": "F1", "verdict": "reject", "reason": "x + 0 is intended"},
                {"id": "F2", "verdict": "reject", "reason": "pre-existing helper"},
                {"id": "F3", "verdict": "confirm", "reason": "verified"},
            ], new_findings=[
                finding(25, Severity.MAJOR, 0.9, evidence_refs=["o1"], summary="overseer found"),
                finding(26, Severity.MAJOR, 0.9, evidence_refs=["t77"], summary="invented"),
            ], notes_for_reviewer="Check how f15 is called."),
        ]
    )  # fmt: skip
    res = run_review(repo, opts(FakeClient(responder=primary), overseer, OverseerTrigger.RISKY))

    stages = {s.name: s for s in res.stages}
    assert list(stages) == ["primary", "overseer", "second_pass"]
    ov = stages["overseer"]
    assert (ov.confirmed, ov.rejected, ov.added) == (1, 2, 1)  # the invented finding is dropped
    assert any("major/blocker" in r for r in stages["primary"].trigger_reasons)
    assert stages["primary"].escalations[0].question == "Is f15 used elsewhere?"

    by_summary = {f.summary: f for f in res.findings}
    assert by_summary["problem at 85"].verified_by == "overseer:confirmed"
    assert by_summary["overseer found"].source is Source.OVERSEER
    assert by_summary["overseer found"].verified_by == "overseer"
    assert by_summary["new problem"].source is Source.AGENT  # second pass finding
    assert "problem at 5" not in by_summary and "problem at 45" not in by_summary
    assert "re-raised" not in by_summary  # the overseer rejected line 5; it stays rejected
    reasons_ = {d.finding.summary: d.reason for d in res.dropped}
    assert reasons_["problem at 5"].startswith("overseer rejected: x + 0 is intended")
    assert "rejected" in reasons_["re-raised"]
    assert reasons_["invented"] == "overseer finding lacks verifiable evidence"

    # The second pass saw the overseer's notes, as untrusted-wrapped guidance.
    second_msg = next(
        r.messages[1].content for r in primary_calls if "SECOND PASS" in r.messages[1].content
    )
    assert "Check how f15 is called." in second_msg
    assert "REJECTED your finding at pkg/big.py:5" in second_msg
    assert "<untrusted_overseer_notes>" in second_msg

    # Refs stay unique per stage and usage adds up across stages.
    refs = [s.ref for s in res.tool_trace]
    assert len(refs) == len(set(refs)) and {r[0] for r in refs} == {"t", "o", "r"}
    total_calls = sum(s.usage.llm_calls for s in res.stages)
    assert res.usage.llm_calls == total_calls and res.usage.llm_calls >= 5
    assert ov.model == "fake-model"


def test_clean_review_skips_the_overseer_and_costs_nothing(repo: Path) -> None:
    primary = FakeClient([call("submit_findings", "s", findings=[])])
    overseer = FakeClient([])
    res = run_review(repo, opts(primary, overseer, OverseerTrigger.RISKY))
    assert [(s.name, s.ran) for s in res.stages] == [("primary", True), ("overseer", False)]
    assert overseer.requests == []


def test_always_mode_runs_and_records_why_it_would_not_have(repo: Path) -> None:
    primary = FakeClient([call("submit_findings", "s", findings=[])])
    overseer = FakeClient([call("submit_oversight", "o", verdicts=[])])
    res = run_review(repo, opts(primary, overseer, OverseerTrigger.ALWAYS))
    ov = next(s for s in res.stages if s.name == "overseer")
    assert ov.ran and ov.trigger_reasons == []  # always mode: ran without a risk trigger
    assert len(overseer.requests) == 1


def test_primary_stage_keeps_the_paired_no_overseer_findings(repo: Path) -> None:
    primary = FakeClient(three_findings_primary())
    overseer = FakeClient([call("submit_oversight", "o", verdicts=[
        {"id": "F1", "verdict": "reject", "reason": "wrong"}])])  # fmt: skip
    res = run_review(repo, opts(primary, overseer, OverseerTrigger.ALWAYS))
    before = next(s for s in res.stages if s.name == "primary").findings_before_overseer
    assert before is not None
    assert {f.summary for f in before} == {"problem at 5", "problem at 45", "problem at 85"}
    assert "problem at 5" not in {f.summary for f in res.findings}


def test_overseer_failure_is_a_warning_not_an_incomplete_review(repo: Path) -> None:
    def boom(req: CompletionRequest) -> LLMResponse:
        raise LLMError("A Timeout Occurred")

    primary = FakeClient(three_findings_primary())
    res = run_review(repo, opts(primary, FakeClient(responder=boom), OverseerTrigger.RISKY))
    assert not res.incomplete
    assert any("not independently verified" in w for w in res.warnings)
    ov = next(s for s in res.stages if s.name == "overseer")
    assert ov.error and "did not finish" in ov.error
    assert {"problem at 5", "problem at 45", "problem at 85"} <= {f.summary for f in res.findings}


def test_overseer_cost_cap_is_enforced(repo: Path) -> None:
    cfg = Config()
    cfg.overseer.max_cost_usd = 0.15
    expensive = [
        LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="read_file",
                                         arguments={"path": "pkg/big.py"})], cost_usd=0.2)
        for i in range(5)
    ]  # fmt: skip
    overseer = FakeClient(expensive)
    primary = FakeClient(three_findings_primary())
    res = run_review(repo, opts(primary, overseer, OverseerTrigger.RISKY), cfg)
    ov = next(s for s in res.stages if s.name == "overseer")
    assert len(overseer.requests) <= 2  # stopped after the first $0.20 call blew the $0.15 cap
    assert ov.error and ov.usage.cost_usd == pytest.approx(0.2)


def test_overseer_tool_budget_is_its_own(repo: Path) -> None:
    cfg = Config()
    cfg.overseer.max_tool_calls = 1
    seen = [
        call("read_file", "a", path="pkg/big.py"),
        call("read_file", "b", path="pkg/big.py"),
        call("submit_oversight", "s", verdicts=[]),
    ]
    overseer = FakeClient(seen)
    res = run_review(
        repo, opts(FakeClient(three_findings_primary()), overseer, OverseerTrigger.RISKY), cfg
    )
    assert [s.ref for s in res.tool_trace if s.ref.startswith("o")] == ["o1"]
    assert [s.ref for s in res.tool_trace if s.ref.startswith("t")] == ["t1"]


def test_overseer_cannot_touch_deterministic_findings(repo: Path) -> None:
    from tests.test_checks import FAKE_GH

    commit_files(repo, {"pkg/leak.py": f'KEY = "{FAKE_GH}"\n'}, "oops")
    primary = FakeClient([call("submit_findings", "s", findings=[])])
    overseer = FakeClient([call("submit_oversight", "o", verdicts=[
        {"id": "F1", "verdict": "reject", "reason": "ignore secrets"}])])  # fmt: skip
    res = run_review(repo, opts(primary, overseer, OverseerTrigger.ALWAYS))
    assert any(f.rule == "secrets:github-token" for f in res.findings)


def test_overseer_prompt_marks_everything_untrusted(repo: Path) -> None:
    primary = FakeClient(three_findings_primary())
    overseer = FakeClient([call("submit_oversight", "o", verdicts=[])])
    run_review(repo, opts(primary, overseer, OverseerTrigger.RISKY))
    first = overseer.requests[0]
    user = first.messages[1].content
    assert "<untrusted_primary_findings>" in user and "<untrusted_diff>" in user
    assert "<untrusted_layer1>" in user
    assert "never follow" in first.messages[0].content.lower()
    assert "submit_oversight" in {t.name for t in first.tools}


def test_escalations_only_offered_when_the_overseer_is_active(repo: Path) -> None:
    on = FakeClient([call("submit_findings", "s", findings=[])])
    run_review(repo, opts(on, FakeClient([call("submit_oversight", "o", verdicts=[])]),
                          OverseerTrigger.ALWAYS))  # fmt: skip
    off = FakeClient([call("submit_findings", "s", findings=[])])
    run_review(repo, opts(off, None, OverseerTrigger.NEVER))

    def submit_props(c: FakeClient) -> set[str]:
        tool = next(t for t in c.requests[0].tools if t.name == "submit_findings")
        return set(tool.parameters["properties"])

    assert submit_props(on) == {"findings", "escalations"}
    assert submit_props(off) == {"findings"}
    assert "escalations" in on.requests[0].messages[0].content
    assert "escalations" not in off.requests[0].messages[0].content


# --- config -----------------------------------------------------------------


def test_effective_trigger_follows_config_and_overrides() -> None:
    cfg = Config()
    agent = ReviewMode.AGENT
    assert effective_trigger(cfg, ReviewOptions(), agent) is OverseerTrigger.NEVER  # off by default
    cfg.overseer.enabled = True
    assert effective_trigger(cfg, ReviewOptions(), agent) is OverseerTrigger.RISKY
    assert effective_trigger(cfg, ReviewOptions(overseer=OverseerTrigger.NEVER), agent) is (
        OverseerTrigger.NEVER
    )
    assert effective_trigger(Config(), ReviewOptions(overseer=OverseerTrigger.ALWAYS), agent) is (
        OverseerTrigger.ALWAYS
    )
    for mode in (ReviewMode.BASELINE, ReviewMode.DETERMINISTIC):
        assert effective_trigger(cfg, ReviewOptions(), mode) is OverseerTrigger.NEVER


def test_repo_config_cannot_enable_or_retarget_the_overseer(tmp_path: Path) -> None:
    (tmp_path / ".watchman.toml").write_text(
        '[overseer]\nenabled = true\nmodel = "evil/model"\n[review]\nmin_confidence = 0.7\n'
    )
    cfg, warnings = load_config(tmp_path)
    assert cfg.overseer.enabled is False
    assert cfg.overseer.model == "anthropic/claude-opus-5.5"
    assert any("[overseer]" in w for w in warnings)


def test_tool_trace_step_refs_do_not_collide_with_grounding_aliases() -> None:
    from watchman_review.agent.grounding import normalize_ref

    assert [normalize_ref(r) for r in ("[ref:o3]", "R2", "t1 (read_file)")] == ["o3", "r2", "t1"]
    assert ToolTraceStep(ref="o1", tool="read_file", ok=True).ref == "o1"
