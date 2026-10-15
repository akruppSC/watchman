from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import commit_files
from watchman_review.config import Config, LivePolicy
from watchman_review.live import controller as live
from watchman_review.live import state as st
from watchman_review.live.snapshot import _git, head_sha
from watchman_review.review import ReviewError, ReviewOptions
from watchman_review.schema import (
    Category,
    Finding,
    ReviewMode,
    ReviewResult,
    Severity,
    Source,
    Usage,
    compute_verdict,
)

MARKERS = {"BAD_BLOCKER": Severity.BLOCKER, "BAD_MAJOR": Severity.MAJOR, "BAD_NIT": Severity.NIT}


class FakeReviewer:
    """Flags every line containing BAD_* in the snapshot it is given, like a stable reviewer."""

    def __init__(self) -> None:
        self.calls: list[ReviewOptions] = []
        self.incomplete = False
        self.fail = False

    def __call__(self, repo: Path, opts: ReviewOptions, cfg: Config | None) -> ReviewResult:
        self.calls.append(opts)
        if self.fail:
            raise ReviewError("boom")
        findings: list[Finding] = []
        for path in _git(repo, "ls-tree", "-r", "--name-only", opts.head).splitlines():
            for n, line in enumerate(_git(repo, "show", f"{opts.head}:{path}").splitlines(), 1):
                for marker, sev in MARKERS.items():
                    if marker in line:
                        findings.append(
                            Finding(
                                category=Category.CORRECTNESS,
                                severity=sev,
                                confidence=0.9,
                                file=path,
                                line_start=n,
                                line_end=n,
                                summary=f"{marker} here",
                                evidence="e",
                                source=Source.AGENT,
                                evidence_refs=["t1"],
                                suggested_fix="remove it",
                            )
                        )
        return ReviewResult(
            mode=opts.mode or ReviewMode.AGENT,
            base=opts.base,
            head=opts.head,
            verdict=compute_verdict(findings),
            findings=findings,
            usage=Usage(cost_usd=0.01),
            incomplete=self.incomplete,
            incomplete_reasons=["model timed out"] if self.incomplete else [],
        )


@pytest.fixture
def repo(git_repo: Path) -> Path:
    commit_files(git_repo, {"a.py": "ok = 1\n", "b.py": "fine = 2\n"}, "base")
    return git_repo


def write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)


def run(repo: Path, rev: FakeReviewer, kind: str = "done", **kw: object):
    return live.check(repo, kind=kind, cfg=kw.pop("cfg", Config()), review_fn=rev, **kw)  # type: ignore[arg-type]


def test_nothing_to_review_when_the_tree_matches_the_base(repo: Path) -> None:
    rev = FakeReviewer()
    fb = run(repo, rev)
    assert fb.status == "nothing" and rev.calls == []
    assert "nothing to review" in fb.render()


def test_clean_review_passes_and_ends_the_episode(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "ok = 1\nmore = 2\n")
    fb = run(repo, rev)
    assert (fb.status, fb.gate, fb.round) == ("clean", "pass", 0)
    assert fb.render().startswith("WATCHMAN done: clean")


def test_findings_open_a_round_and_advisory_never_blocks(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "ok = 1\nx = BAD_BLOCKER\n")
    fb = run(repo, rev)
    assert (fb.status, fb.round, fb.verdict, fb.gate) == ("open", 1, "block", "pass")
    assert [f.location for f in fb.new] == ["a.py:2"] and fb.new[0].fix == "remove it"
    assert fb.render().splitlines()[1].startswith("+ blocker correctness a.py:2")


def test_blocking_policy_blocks_on_a_block_verdict_but_not_on_a_warning(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "ok = 1\nx = BAD_MAJOR\n")
    warn = run(repo, rev, policy=LivePolicy.BLOCKING)
    assert (warn.verdict, warn.gate) == ("warn", "pass")
    write(repo, "a.py", "ok = 1\nx = BAD_MAJOR\ny = BAD_BLOCKER\n")
    block = run(repo, rev, policy=LivePolicy.BLOCKING)
    assert block.gate == "block" and "BLOCKED" in block.render()


def test_unchanged_snapshot_is_not_reviewed_again_and_keeps_the_gate(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "x = BAD_BLOCKER\n")
    first = run(repo, rev, policy=LivePolicy.BLOCKING)
    again = run(repo, rev, policy=LivePolicy.BLOCKING)
    assert len(rev.calls) == 1
    assert (again.status, again.gate, again.round, again.cost_usd) == ("unchanged", "block", 1, 0.0)
    assert first.gate == again.gate


def test_a_finding_keeps_its_identity_when_lines_shift_and_counts_fixed_when_rewritten(
    repo: Path,
) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "ok = 1\nx = BAD_MAJOR\n")
    r1 = run(repo, rev)
    assert [f.state for f in r1.new] == ["new"]
    # The worker inserts two lines above the problem: same finding, new line number.
    write(repo, "a.py", "ok = 1\nnew1 = 1\nnew2 = 2\nx = BAD_MAJOR\n")
    r2 = run(repo, rev)
    assert r2.new == [] and [f.location for f in r2.open] == ["a.py:4"]
    assert r2.open[0].since_round == 1 and r2.round == 2
    assert "(open since r1)" in r2.render()
    write(repo, "a.py", "ok = 1\nnew1 = 1\nnew2 = 2\nx = fixed\n")
    r3 = run(repo, rev)
    assert (r3.status, r3.fixed) == ("clean", 1)


def test_new_problems_are_marked_new_alongside_known_ones(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "x = BAD_MAJOR\n")
    run(repo, rev)
    write(repo, "a.py", "x = BAD_MAJOR\ny = BAD_BLOCKER\n")
    fb = run(repo, rev)
    assert [f.location for f in fb.new] == ["a.py:2"] and [f.location for f in fb.open] == [
        "a.py:1"
    ]


def test_round_cap_escalates_to_the_human_and_releases_the_gate(repo: Path) -> None:
    rev = FakeReviewer()
    texts = ["x = BAD_BLOCKER\n", "x = BAD_BLOCKER  # try 2\n", "x = BAD_BLOCKER  # try 3\n"]
    fbs = []
    for text in texts:
        write(repo, "a.py", text)
        fbs.append(run(repo, rev, policy=LivePolicy.BLOCKING))
    assert [f.round for f in fbs] == [1, 2, 3]
    assert [f.gate for f in fbs] == ["block", "block", "released"]
    assert fbs[-1].status == "escalated" and "ESCALATED after 3 rounds" in fbs[-1].render()
    esc = repo / ".watchman/live/ESCALATION-default-done.md"
    assert esc.exists() and "x = BAD_BLOCKER" not in esc.read_text()  # findings, not code
    assert "a.py:1" in esc.read_text()
    # A fourth attempt costs nothing and stays released: the worker can't be trapped.
    write(repo, "a.py", "x = BAD_BLOCKER  # try 4\n")
    later = run(repo, rev, policy=LivePolicy.BLOCKING)
    assert later.status == "escalated" and later.gate == "released" and len(rev.calls) == 3


def test_a_clean_round_resets_the_episode(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "x = BAD_MAJOR\n")
    run(repo, rev)
    write(repo, "a.py", "x = 1\n")
    assert run(repo, rev).status == "clean"
    write(repo, "a.py", "x = BAD_MAJOR  # again\n")
    assert run(repo, rev).round == 1  # a fresh episode, not round 3


def test_checkpoint_kinds_have_independent_episodes(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "x = BAD_MAJOR\n")
    assert run(repo, rev, kind="commit").round == 1
    write(repo, "a.py", "x = BAD_MAJOR  # edit\n")
    assert run(repo, rev, kind="commit").round == 2
    assert run(repo, rev, kind="done").round == 1


def test_an_incomplete_review_never_costs_a_round_or_blocks(repo: Path) -> None:
    rev = FakeReviewer()
    rev.incomplete = True
    write(repo, "a.py", "x = BAD_BLOCKER\n")
    fb = run(repo, rev, policy=LivePolicy.BLOCKING)
    assert (fb.status, fb.gate) == ("error", "pass") and "No action needed" in fb.render()
    rev.incomplete = False
    assert run(repo, rev, policy=LivePolicy.BLOCKING).round == 1  # the failed attempt didn't count
    rev.fail = True
    write(repo, "a.py", "x = BAD_BLOCKER  # edit\n")
    assert run(repo, rev).status == "error"


def test_nits_are_counted_but_not_shown(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "x = BAD_NIT\ny = BAD_MAJOR\n")
    fb = run(repo, rev)
    assert fb.nits == 1 and [f.severity for f in fb.new] == ["major"]
    write(repo, "a.py", "x = BAD_NIT\ny = 1\n")
    assert "1 nit(s) not shown" in run(repo, rev).render()


def test_the_base_is_pinned_at_session_start_so_commits_do_not_shrink_the_review(
    repo: Path,
) -> None:
    rev = FakeReviewer()
    start_sha = head_sha(repo)
    live.start(repo, "s1", task="Add a loader")
    commit_files(repo, {"a.py": "x = BAD_MAJOR\n"}, "worker commit")
    fb = run(repo, rev, kind="commit", session="s1")
    assert rev.calls[0].base == start_sha and rev.calls[0].task == "Add a loader"
    assert fb.round == 1 and [f.location for f in fb.new] == ["a.py:1"]


def test_test_run_checkpoints_use_the_cheap_checks_and_mode(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "x = 1\n")
    run(repo, rev, kind="test_run")
    opts = rev.calls[0]
    assert opts.mode is ReviewMode.DETERMINISTIC
    assert opts.checks == Config().live.test_run_checks and "pytest" not in opts.checks
    write(repo, "a.py", "x = 2\n")
    run(repo, rev, kind="done")
    assert rev.calls[1].mode is ReviewMode.AGENT and rev.calls[1].checks is None


def test_feedback_stays_terse_even_with_many_findings(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "".join(f"x{i} = BAD_MAJOR\n" for i in range(20)))
    fb = run(repo, rev)
    text = fb.render()
    assert len(fb.new) == 6 and fb.hidden == 14
    assert "(+14 more; `watchman live status` lists them)" in text
    assert len(text) < 1400


def test_unknown_kind_and_state_survive_corruption(repo: Path) -> None:
    with pytest.raises(live.LiveError):
        live.check(repo, kind="lunch", cfg=Config())
    write(repo, "a.py", "x = BAD_MAJOR\n")
    rev = FakeReviewer()
    run(repo, rev)
    st.state_path(repo, "default").write_text("{not json")
    assert run(repo, rev).round == 1  # corrupt state restarts rather than crashing


def test_a_finding_shown_at_the_commit_is_not_new_again_at_done(repo: Path) -> None:
    rev = FakeReviewer()
    write(repo, "a.py", "x = BAD_MAJOR\n")
    first = run(repo, rev, kind="commit")
    assert [f.state for f in first.new] == ["new"]
    done = run(repo, rev, kind="done")
    assert done.new == [] and [f.state for f in done.open] == ["open"]
    assert done.open[0].since_round == 1  # remembers when the worker first saw it
    assert done.round == 1  # but the done episode still counts its own rounds


def test_a_cheap_checkpoint_does_not_mark_the_other_scopes_findings_fixed(repo: Path) -> None:
    class ScopedReviewer(FakeReviewer):
        def __call__(self, repo: Path, opts: ReviewOptions, cfg: Config | None) -> ReviewResult:
            res = super().__call__(repo, opts, cfg)
            if opts.mode is ReviewMode.DETERMINISTIC:  # the cheap review can't see LLM findings
                res = res.model_copy(update={"findings": []})
            return res

    rev = ScopedReviewer()
    write(repo, "a.py", "x = BAD_MAJOR\n")
    run(repo, rev, kind="commit")
    cheap = run(repo, rev, kind="test_run")
    assert cheap.fixed == 0  # it found nothing, but that doesn't mean the commit finding is fixed
    assert run(repo, rev, kind="commit").status == "unchanged"
